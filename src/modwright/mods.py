"""Reading the mods already installed alongside a project's deploy target.

A mod that builds on another mod -- a networking library, a shared API --
needs that library's assembly at compile time. The copy the player will
actually run is already on disk in the profile, so ModWright references that
rather than downloading anything or asking for a copy to be vendored into the
repo. Nothing is fetched from the network here, by design: the packages under
a profile are whatever the user's own mod manager installed and already runs.

The awkward part is that a mod package is a folder, not an assembly, and what
is in that folder varies a lot. Observed in a single real profile:

    giosuel-Imperium/giosuel.Imperium.dll                 one DLL, top level
    xilophor-LethalNetworkAPI/LethalNetworkAPI/*.dll      one DLL, nested
    TeamXiaolan-DawnLib/DawnLib/com.github...*.dll        four, named nothing
                                                          like the package
    qwbarch-Concentus/  Concentus.dll + System.Memory.dll + System.Buffers.dll
    qwbarch-MirageCore/ managed DLLs + onnxruntime.dll (native C++)

So the package name cannot be used to guess the assembly name, and not every
file in the folder is safe to reference. Two kinds are filtered out; see
`_EXCLUDE_*` below.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path

#: Assemblies that belong to the framework, not to the mod. Mods bundle these
#: because Unity's Mono profile does not ship them (`Span<T>` and friends), but
#: a mod project that needs them should get them from NuGet with the matching
#: binding redirects -- not borrow whichever version a neighbouring mod
#: happens to have installed. Under a netstandard2.1 target they would also
#: collide with `netstandard.dll`'s own definitions.
_EXCLUDE_PREFIXES = ("System.", "Microsoft.", "netstandard", "mscorlib")

CONFIG_MANIFEST = "manifest.json"


@dataclass(frozen=True)
class SkippedAssembly:
    """A DLL deliberately not referenced, and why -- reported, not hidden."""

    path: Path
    reason: str


@dataclass(frozen=True)
class InstalledMod:
    """One mod package installed in a loader's mods directory."""

    #: Folder name, which is the stable Thunderstore-style `author-Package`
    #: identifier. Stable across versions, unlike the assembly names inside.
    package: str
    path: Path
    #: Assemblies safe to compile against.
    assemblies: tuple[Path, ...]
    skipped: tuple[SkippedAssembly, ...] = ()
    #: From the package's own manifest, when it has one.
    display_name: str | None = None
    version: str | None = None

    @property
    def referenceable(self) -> bool:
        return bool(self.assemblies)

    @property
    def last_changed(self) -> float | None:
        """When the newest referenced assembly was written, or None.

        The question this answers is "did anything in here change", so a
        package shipping several assemblies (DawnLib ships four) reports the
        newest of them rather than trying to nominate a main one.
        """
        stamps = []
        for assembly in self.assemblies:
            try:
                stamps.append(assembly.stat().st_mtime)
            except OSError:
                continue
        return max(stamps, default=None)


def is_managed_assembly(path: Path) -> bool:
    """True if this DLL is .NET, false if it is native code.

    Read out of the file rather than guessed from the name: mods ship native
    dependencies right beside their managed ones (`opus.dll` next to
    `OpusDotNet.dll`), and referencing a native DLL fails the build outright.

    A .NET assembly carries a CLI header in the PE optional header's 15th data
    directory; a native DLL leaves that entry zero.
    """
    try:
        data = path.read_bytes()
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_offset : pe_offset + 4] != b"PE\0\0":
            return False
        magic = struct.unpack_from("<H", data, pe_offset + 24)[0]
        if magic == 0x10B:  # PE32
            directories = pe_offset + 24 + 96
        elif magic == 0x20B:  # PE32+
            directories = pe_offset + 24 + 112
        else:
            return False
        cli_header_rva = struct.unpack_from("<I", data, directories + 14 * 8)[0]
        return cli_header_rva != 0
    except (OSError, struct.error, IndexError):
        return False


def _is_framework_assembly(path: Path) -> bool:
    return path.stem.startswith(_EXCLUDE_PREFIXES)


def _read_manifest(package_dir: Path) -> tuple[str | None, str | None]:
    manifest = package_dir / CONFIG_MANIFEST
    if not manifest.is_file():
        return None, None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None, None
    name = data.get("name")
    version = data.get("version_number")
    return (name if isinstance(name, str) else None,
            version if isinstance(version, str) else None)


def read_installed_mod(package_dir: Path) -> InstalledMod:
    """Describe one installed package: what can be referenced, and what cannot."""
    assemblies: list[Path] = []
    skipped: list[SkippedAssembly] = []

    for dll in sorted(package_dir.rglob("*.dll")):
        if not is_managed_assembly(dll):
            skipped.append(SkippedAssembly(dll, "native library, not a .NET assembly"))
        elif _is_framework_assembly(dll):
            skipped.append(
                SkippedAssembly(dll, "framework assembly; use a NuGet package instead")
            )
        else:
            assemblies.append(dll)

    display_name, version = _read_manifest(package_dir)
    return InstalledMod(
        package=package_dir.name,
        path=package_dir,
        assemblies=tuple(assemblies),
        skipped=tuple(skipped),
        display_name=display_name,
        version=version,
    )


def read_loose_assembly(dll: Path) -> InstalledMod:
    """Describe a dependency installed as a bare file rather than a folder.

    Named for the file itself, since there is no package folder to take a name
    from. It carries no manifest and therefore no version -- see
    `list_installed_mods` for why none is invented.
    """
    if not is_managed_assembly(dll):
        reason = "native library, not a .NET assembly"
    elif _is_framework_assembly(dll):
        reason = "framework assembly; use a NuGet package instead"
    else:
        return InstalledMod(package=dll.stem, path=dll, assemblies=(dll,))
    return InstalledMod(
        package=dll.stem,
        path=dll,
        assemblies=(),
        skipped=(SkippedAssembly(dll, reason),),
    )


def list_installed_mods(mods_dir: Path) -> list[InstalledMod]:
    """Every dependency in a mods directory that could be referenced.

    Two shapes count, because both really occur side by side in one directory:
    a package FOLDER, which a mod manager creates, and a LOOSE `.dll`, which
    is what a library downloaded from a release page or a locally-built mod
    looks like. Keeping only folders made the second kind invisible to
    `add_mod_reference` entirely.

    A loose file has no `manifest.json`, so it has no version. None is
    recorded rather than read out of the assembly's own metadata: that
    metadata is whatever the dependency's build happened to stamp, which is
    routinely unrelated to the version the mod is actually released under, and
    a wrong version presented as authoritative is worse than an absent one.
    """
    if not mods_dir.is_dir():
        return []

    found = []
    for entry in sorted(mods_dir.iterdir()):
        if entry.is_dir():
            found.append(read_installed_mod(entry))
        elif entry.suffix.lower() == ".dll":
            found.append(read_loose_assembly(entry))
    return found


def find_installed_mod(mods_dir: Path, package: str) -> InstalledMod | None:
    """Look a package up by folder name, then by its manifest name.

    Matching is case-insensitive and accepts the bare package name as well as
    the `author-Package` folder, since that is how mods are referred to in
    conversation ("StaticNetcodeLib", not "xilophor-StaticNetcodeLib").
    """
    wanted = package.strip().lower()
    candidates = list_installed_mods(mods_dir)

    for mod in candidates:
        if mod.package.lower() == wanted:
            return mod
    for mod in candidates:
        if (mod.display_name or "").lower() == wanted:
            return mod
    # `author-Package` -> `Package`, the half people actually say.
    for mod in candidates:
        _, _, tail = mod.package.partition("-")
        if tail.lower() == wanted:
            return mod
    return None
