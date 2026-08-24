"""BepInEx 5 -- the v1 adapter, covering Mono Unity games.

Deploy shape: a single plugin DLL copied into `BepInEx/plugins`. Log: the
single fixed `BepInEx/LogOutput.log`. This is the simplest shape on the
roadmap, which is why it goes first: MelonLoader, Hollow Knight's Modding API
and Beat Saber's BSIPA all reuse it, differing only in where they look.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from modwright.errors import (
    AdapterStepNotImplementedError,
    ArtifactLockedError,
    Il2CppUnsupportedError,
)
from modwright.models import BuildOutcome, DeployOutcome, GameContext, PatchTarget


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
        """Not implemented yet.

        Intended approach, per the prior-art review: shell out to BepInEx's own
        maintained templates (`dotnet new bepinex5plugin`) rather than emitting
        a .csproj from scratch, then post-process the generated project to add
        `<Reference>` entries resolved from `game_context.managed_dir`. That
        auto-resolution is the part BepInEx.Templates deliberately leaves to the
        developer, and is where ModWright adds value.
        """
        raise AdapterStepNotImplementedError(
            "BepInEx 5 scaffolding is not implemented yet."
        )

    def build(self, project_path: Path) -> BuildOutcome:
        """Not implemented yet.

        Intended approach: `dotnet build -c Release`, then resolve the produced
        assembly via MSBuild's own `--getProperty:TargetPath` rather than
        guessing at `bin/Release/<tfm>/` -- mod projects target unusual
        frameworks (net35, net46, netstandard2.1) and the folder name varies.
        """
        raise AdapterStepNotImplementedError(
            "BepInEx 5 build is not implemented yet."
        )

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
        """Not implemented yet -- see `modwright.patches`.

        BepInEx mods patch via Harmony attributes, so this will delegate to the
        shared Harmony extractor rather than implementing its own parsing.
        """
        raise AdapterStepNotImplementedError(
            "Harmony patch-target extraction is not implemented yet."
        )

    def resolve_log(self, game_context: GameContext) -> Path | None:
        log_path = game_context.install_root / "BepInEx" / "LogOutput.log"
        return log_path if log_path.exists() else None
