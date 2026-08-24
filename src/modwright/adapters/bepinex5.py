"""BepInEx 5 -- the v1 adapter, covering Mono Unity games.

Deploy shape: a single plugin DLL copied into `BepInEx/plugins`. Log: the
single fixed `BepInEx/LogOutput.log`. This is the simplest shape on the
roadmap, which is why it goes first: MelonLoader, Hollow Knight's Modding API
and Beat Saber's BSIPA all reuse it, differing only in where they look.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from modwright.errors import (
    ArtifactLockedError,
    BuildFailedError,
    Il2CppUnsupportedError,
    InvalidModNameError,
    ProjectExistsError,
)
from modwright.models import BuildOutcome, DeployOutcome, GameContext, PatchTarget
from modwright.patches import extract_harmony_targets

#: Mod names become both an assembly name and a C# namespace, so they are held
#: to C# identifier rules rather than silently mangled into something that
#: compiles to a different name than the user asked for.
_VALID_MOD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CSPROJ_TEMPLATE = """<Project Sdk="Microsoft.NET.Sdk">

  <PropertyGroup>
    <TargetFramework>__TFM__</TargetFramework>
    <AssemblyName>__MOD_NAME__</AssemblyName>
    <RootNamespace>__MOD_NAME__</RootNamespace>
    <Version>1.0.0</Version>
    <LangVersion>latest</LangVersion>
  </PropertyGroup>

  <!--
    Assemblies are referenced straight out of the game install, so the mod
    compiles against exactly the versions it will run against, with no NuGet
    feed required. GameDir is overridable so this project still builds on a
    machine where the game lives somewhere else:

        dotnet build -c Release -p:GameDir="D:\\Games\\__GAME_FOLDER__"
  -->
  <PropertyGroup>
    <GameDir Condition="'$(GameDir)' == ''">__GAME_DIR__</GameDir>
    <GameManagedDir>$(GameDir)\\__DATA_FOLDER__\\Managed</GameManagedDir>
    <BepInExCoreDir>$(GameDir)\\BepInEx\\core</BepInExCoreDir>
  </PropertyGroup>

  <!-- Private="false" keeps game and loader assemblies out of the build
       output; only the mod's own DLL should ever be deployed. -->
  <ItemGroup>
    <Reference Include="$(BepInExCoreDir)\\BepInEx.dll" Private="false" />
    <Reference Include="$(BepInExCoreDir)\\0Harmony.dll" Private="false" />
    <Reference Include="$(GameManagedDir)\\Assembly-CSharp*.dll" Private="false" />
    <Reference Include="$(GameManagedDir)\\UnityEngine*.dll" Private="false" />
    <Reference Include="$(GameManagedDir)\\Unity.*.dll" Private="false" />
  </ItemGroup>

</Project>
"""

_PLUGIN_TEMPLATE = """using BepInEx;
using BepInEx.Logging;
using HarmonyLib;

namespace __MOD_NAME__;

[BepInPlugin(PluginGuid, PluginName, PluginVersion)]
public class Plugin : BaseUnityPlugin
{
    // Must be unique across every mod the user has installed. Change the
    // "com.example" part to something you own.
    public const string PluginGuid = "__PLUGIN_GUID__";
    public const string PluginName = "__MOD_NAME__";
    public const string PluginVersion = "1.0.0";

    internal static new ManualLogSource Logger;

    private readonly Harmony _harmony = new Harmony(PluginGuid);

    private void Awake()
    {
        Logger = base.Logger;

        // Applies every [HarmonyPatch]-annotated class in this assembly.
        _harmony.PatchAll();

        Logger.LogInfo($"{PluginName} {PluginVersion} loaded.");
    }
}
"""

_MOD_GITIGNORE = "bin/\nobj/\n"

#: Compiler diagnostics look like `Plugin.cs(12,9): error CS0103: ...`.
_COMPILER_ERROR = re.compile(r"^.*?: (?:error|warning) [A-Z]+\d+: .*$", re.MULTILINE)


def _compiler_errors(build_output: str, limit: int = 15) -> list[str]:
    """Pull the actual diagnostics out of MSBuild's verbose output.

    A failed build otherwise returns hundreds of lines of restore chatter, and
    the two lines that say what is wrong get lost in it.
    """
    seen: list[str] = []
    for match in _COMPILER_ERROR.findall(build_output):
        line = match.strip()
        if "error" in line and line not in seen:
            seen.append(line)
    return seen[:limit]


class BepInEx5Adapter:
    framework_id = "bepinex5"
    display_name = "BepInEx 5"

    def detect(self, install_root: Path) -> GameContext | None:
        """Structural sniff of a Unity install root.

        Returns None (rather than raising) whenever the install simply isn't
        ours, so the registry can keep trying. The one exception is IL2CPP: the
        user pointed at a real Unity game we positively cannot support, and
        saying so beats silently falling through to "unsupported game".
        """
        data_dirs = sorted(install_root.glob("*_Data"))
        if not data_dirs:
            return None  # Not a Unity install; some other adapter may claim it.

        # IL2CPP compiles game logic to native code, leaving Assembly-CSharp.dll
        # as a metadata-only stub. No IL decompiler can read it, so refuse
        # loudly instead of scaffolding a mod against a shell.
        if (install_root / "GameAssembly.dll").exists():
            raise Il2CppUnsupportedError(
                f"{install_root.name} is an IL2CPP game, which ModWright cannot "
                "inspect: its real logic is compiled to native code, not IL.",
                hints=[
                    "IL2CPP support needs a separate toolchain (Cpp2IL / "
                    "Il2CppInterop) and is out of scope.",
                ],
            )

        managed_dir = data_dirs[0] / "Managed"
        if not (managed_dir / "Assembly-CSharp.dll").exists():
            return None

        bepinex_dir = install_root / "BepInEx"
        if not bepinex_dir.is_dir():
            # A Mono Unity game without BepInEx installed. Could belong to a
            # native-loading or single-game adapter, so defer rather than claim.
            return None

        return GameContext(
            install_root=install_root,
            game_name=install_root.name,
            framework_id=self.framework_id,
            managed_dir=managed_dir,
            mods_dir=bepinex_dir / "plugins",
        )

    def scaffold(
        self, project_path: Path, game_context: GameContext, mod_name: str
    ) -> list[Path]:
        """Write a mod project wired to this specific game install.

        The .csproj is generated rather than produced by `dotnet new
        bepinex5plugin`: BepInEx's template needs a custom NuGet feed installed
        first, pins its own dependency versions rather than the ones this game
        actually ships, and deliberately leaves game references for the
        developer to add -- so it would need post-processing anyway.
        """
        if not _VALID_MOD_NAME.match(mod_name):
            raise InvalidModNameError(
                f"{mod_name!r} is not usable as an assembly name and namespace.",
                hints=[
                    "Use letters, digits and underscores, starting with a "
                    "letter or underscore. Example: MyFirstMod",
                ],
            )

        assert game_context.managed_dir is not None  # guaranteed by detect()
        project_path.mkdir(parents=True, exist_ok=True)

        existing = list(project_path.glob("*.csproj"))
        if existing:
            raise ProjectExistsError(
                f"{project_path} already contains {existing[0].name}.",
                hints=["Choose an empty directory, or delete the existing project."],
            )

        # The Managed folder is expressed relative to GameDir rather than
        # hardcoded, so overriding GameDir on another machine redirects every
        # reference at once.
        data_folder = game_context.managed_dir.parent.name

        csproj_text = (
            _CSPROJ_TEMPLATE.replace("__TFM__", self._target_framework(game_context))
            .replace("__MOD_NAME__", mod_name)
            .replace("__GAME_DIR__", xml_escape(str(game_context.install_root)))
            .replace("__DATA_FOLDER__", xml_escape(data_folder))
            .replace("__GAME_FOLDER__", xml_escape(game_context.install_root.name))
        )
        plugin_text = _PLUGIN_TEMPLATE.replace("__MOD_NAME__", mod_name).replace(
            "__PLUGIN_GUID__", f"com.example.{mod_name.lower()}"
        )

        written: list[Path] = []
        for relative, content in (
            (f"{mod_name}.csproj", csproj_text),
            ("Plugin.cs", plugin_text),
            (".gitignore", _MOD_GITIGNORE),
        ):
            path = project_path / relative
            path.write_text(content, encoding="utf-8")
            written.append(path)

        return written

    @staticmethod
    def _target_framework(game_context: GameContext) -> str:
        """Match the target to the BCL the game actually ships.

        This is about which reference assemblies exist, not about what Mono can
        load: Unity ships a netstandard 2.1 facade under either Api
        Compatibility Level, so `netstandard.dll` alone does not discriminate
        and a netstandard2.1 plugin will generally load either way.

        What differs is the BCL. Under the .NET Framework profile the game
        ships the full set (System.Data, System.Xml.Linq, System.Net.Http);
        targeting net472 makes those available to the mod, and matches what
        BepInEx 5 and the overwhelming majority of Mono-Unity mods target.
        Under the .NET Standard profile those assemblies are absent, so net472
        would promise a BCL that is not there and netstandard2.1 is correct.
        """
        assert game_context.managed_dir is not None
        framework_markers = ("System.Data.dll", "System.Xml.Linq.dll")
        if all((game_context.managed_dir / m).exists() for m in framework_markers):
            return "net472"
        if (game_context.managed_dir / "netstandard.dll").exists():
            return "netstandard2.1"
        # Neither profile is recognisable; net472 is the safer guess, since
        # BepInEx 5 itself targets .NET Framework.
        return "net472"

    def build(self, project_path: Path) -> BuildOutcome:
        """Compile the mod with `dotnet build`.

        The produced assembly is located by asking MSBuild for `TargetPath`
        rather than guessing `bin/Release/<tfm>/<name>.dll` -- mod projects
        target unusual frameworks and the folder name varies with them.
        """
        project_file = self._project_file(project_path)

        result = self._run_dotnet(["build", str(project_file), "-c", "Release"])
        if result.returncode != 0:
            raise BuildFailedError(
                "dotnet build failed.",
                hints=_compiler_errors(result.stdout) or [result.stdout[-1500:]],
            )

        # Evaluated, not rebuilt: this reads the property off the project that
        # was just built.
        query = self._run_dotnet(
            ["msbuild", str(project_file), "-getProperty:TargetPath", "-p:Configuration=Release"]
        )
        target_path = query.stdout.strip()
        if query.returncode != 0 or not target_path:
            raise BuildFailedError(
                "Build succeeded but the output assembly could not be located.",
                hints=[query.stdout[-1000:] or query.stderr[-1000:]],
            )

        artifact = Path(target_path)
        if not artifact.exists():
            raise BuildFailedError(
                f"MSBuild reported an output at {artifact}, but it does not exist."
            )

        return BuildOutcome(artifact=artifact, log=result.stdout[-2000:])

    @staticmethod
    def _project_file(project_path: Path) -> Path:
        projects = sorted(project_path.glob("*.csproj"))
        if not projects:
            raise BuildFailedError(
                f"No .csproj found in {project_path}.",
                hints=["Run scaffold_mod_project first."],
            )
        if len(projects) > 1:
            raise BuildFailedError(
                f"{project_path} contains {len(projects)} .csproj files.",
                hints=["ModWright expects one project per mod directory."],
            )
        return projects[0]

    @staticmethod
    def _run_dotnet(args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["dotnet", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise BuildFailedError(
                "The `dotnet` command was not found.",
                hints=["Install the .NET SDK and ensure `dotnet` is on PATH."],
            ) from exc

    def deploy(
        self, outcome: BuildOutcome, game_context: GameContext
    ) -> DeployOutcome:
        """Copy the plugin DLL into `BepInEx/plugins`."""
        if outcome.deployed_by_build or outcome.artifact is None:
            raise ValueError(
                "BepInEx 5 builds always produce a separate artifact to deploy."
            )

        plugins_dir = game_context.mods_dir
        assert plugins_dir is not None  # guaranteed by detect()
        plugins_dir.mkdir(parents=True, exist_ok=True)
        destination = plugins_dir / outcome.artifact.name

        try:
            shutil.copy2(outcome.artifact, destination)
        except (PermissionError, OSError) as exc:
            # Windows raises a sharing violation (WinError 32) when the game
            # holds the DLL open. That is the overwhelmingly common cause, and
            # a plain stack trace hides it.
            raise ArtifactLockedError(
                f"Could not write {destination.name}: the file is locked.",
                hints=[
                    f"{game_context.game_name} is probably running -- close it "
                    "and deploy again.",
                    f"Underlying error: {exc}",
                ],
            ) from exc

        return DeployOutcome(destination=destination, copied=True)

    def extract_patch_targets(self, mod_source_dir: Path) -> list[PatchTarget]:
        """BepInEx mods patch via Harmony, so the shared extractor applies."""
        return extract_harmony_targets(mod_source_dir)

    def resolve_log(self, game_context: GameContext) -> Path | None:
        log_path = game_context.install_root / "BepInEx" / "LogOutput.log"
        return log_path if log_path.exists() else None
