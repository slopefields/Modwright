"""Finding mod-manager profiles.

Mod managers (r2modman, Thunderstore Mod Manager, Gale) do not mod the game
folder. Each *profile* is a complete standalone loader tree kept in the
manager's own data directory, and the game is launched pointed at it:

    <profile>/  winhttp.dll  doorstop_config.ini
                BepInEx/  core  config  cache  patchers  plugins
                          LogOutput.log

So the game install holds the assemblies while the profile holds the loader,
the installed mods, and the log that actually gets written.

This module is deliberately *convenience only*. Everything it finds can be
passed in by hand instead, and a manager it has never heard of still works
that way -- so a wrong or outdated root here degrades to "nothing listed",
never to a broken deploy.

Detection is shape-driven rather than name-driven for the same reason: a
candidate counts as a profile because it contains a loader tree, not because
it sits under a path we recognise. Which shapes count is asked of the
adapters, so this module never learns what any one framework looks like --
MelonLoader profiles hold `Mods/` where BepInEx holds `BepInEx/plugins`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from modwright.adapters.registry import ADAPTERS

#: Manager data roots, each expected to hold `<game>/profiles/<name>`.
#:
#: VERIFIED on a real install: r2modman only (checked against two games,
#: Lethal Company and PEAK, identical layout). The others are recorded from
#: documentation and are NOT confirmed -- treat a miss as expected, and do not
#: build anything load-bearing on these paths without checking them first.
_MANAGER_ROOTS: tuple[tuple[str, str], ...] = (
    ("r2modman", "r2modmanPlus-local"),  # verified
    ("Thunderstore Mod Manager", "Thunderstore Mod Manager/DataFolder"),  # unverified
    ("Gale", "com.kesomannen.gale"),  # unverified
)


@dataclass(frozen=True)
class ModProfile:
    """One mod-manager profile that could be deployed into."""

    manager: str
    game_folder: str
    name: str
    path: Path
    #: Which adapter recognised this tree.
    framework_id: str
    #: The loader log inside this profile, when it has been written at least
    #: once. Its absence just means the profile has never been launched.
    log_path: Path | None
    #: Number of installed mods, as a rough "is this a busy profile" signal.
    #: A thin profile makes log output readable and failures attributable.
    mod_count: int


def manager_data_dirs() -> list[Path]:
    """Candidate manager data directories that exist on this machine."""
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases.append(Path(appdata))
    # Linux/macOS installs keep the same folder names under the XDG config
    # dir; harmless to probe, and skipped silently when absent.
    bases.append(Path.home() / ".config")

    found: list[Path] = []
    for base in bases:
        for _, relative in _MANAGER_ROOTS:
            candidate = base / relative
            if candidate.is_dir():
                found.append(candidate)
    return found


def _manager_name(root: Path) -> str:
    for name, relative in _MANAGER_ROOTS:
        if root.as_posix().endswith(relative):
            return name
    return root.name


def _normalise(name: str) -> str:
    """Fold a game name for comparison: 'Lethal Company' -> 'lethalcompany'."""
    return "".join(c for c in name.lower() if c.isalnum())


def _read_profile(manager: str, game_folder: str, path: Path) -> ModProfile | None:
    for adapter in ADAPTERS:
        info = adapter.inspect_loader_root(path)
        if info is None:
            continue  # Not this adapter's shape; try the next.
        mods = info.mods_dir
        return ModProfile(
            manager=manager,
            game_folder=game_folder,
            name=path.name,
            path=path,
            framework_id=adapter.framework_id,
            log_path=info.log_path,
            mod_count=len(list(mods.iterdir())) if mods.is_dir() else 0,
        )
    return None  # No adapter can deploy here, so it is not worth offering.


def discover_profiles(game_name: str | None = None) -> list[ModProfile]:
    """List mod-manager profiles, optionally narrowed to one game.

    `game_name` is matched loosely against the manager's own game folder,
    since managers strip spacing ('Lethal Company' -> 'LethalCompany').
    """
    wanted = _normalise(game_name) if game_name else None
    profiles: list[ModProfile] = []

    for root in manager_data_dirs():
        manager = _manager_name(root)
        for game_dir in sorted(root.iterdir()):
            if not game_dir.is_dir():
                continue
            if wanted and _normalise(game_dir.name) != wanted:
                continue
            profiles_dir = game_dir / "profiles"
            if not profiles_dir.is_dir():
                continue
            for profile_dir in sorted(profiles_dir.iterdir()):
                if not profile_dir.is_dir():
                    continue
                profile = _read_profile(manager, game_dir.name, profile_dir)
                if profile is not None:
                    profiles.append(profile)

    return profiles
