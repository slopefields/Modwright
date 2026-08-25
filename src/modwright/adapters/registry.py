"""Adapter lookup: install root -> the adapter that handles it."""

from __future__ import annotations

from pathlib import Path

from modwright.adapters.base import ModFrameworkAdapter
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.errors import InvalidInstallRootError, UnsupportedGameError
from modwright.models import GameContext

#: Ordered: the first adapter whose `detect` claims the install wins. Generic
#: multi-game loaders come before per-game ones, since a game carrying an
#: installed loader should be driven through that loader.
ADAPTERS: tuple[ModFrameworkAdapter, ...] = (BepInEx5Adapter(),)


def get_adapter(framework_id: str) -> ModFrameworkAdapter:
    for adapter in ADAPTERS:
        if adapter.framework_id == framework_id:
            return adapter
    raise UnsupportedGameError(
        f"No adapter registered for framework {framework_id!r}.",
        hints=[f"Known frameworks: {', '.join(a.framework_id for a in ADAPTERS)}"],
    )


def detect_framework(install_root: Path | str) -> GameContext:
    """Identify the modding framework for a game install.

    Raises `UnsupportedGameError` when nothing claims it, rather than guessing
    -- a clear refusal is more useful to an agent than a half-working adapter.
    """
    root = Path(install_root)
    if not root.is_dir():
        raise InvalidInstallRootError(f"Not a directory: {root}")

    for adapter in ADAPTERS:
        context = adapter.detect(root)
        if context is not None:
            return context

    # Nothing in the game folder identifies a framework -- but a mod manager
    # keeps each profile as a standalone loader tree elsewhere on disk, and
    # the game folder of someone who has only ever used a manager can be
    # completely untouched. A profile for this game is proof enough of which
    # framework it uses.
    #
    # Imported here rather than at module scope: profile discovery asks the
    # adapters what a loader tree looks like, so importing it above would
    # close a cycle back onto this module.
    from modwright.profiles import discover_profiles

    for profile in discover_profiles(root.name):
        adapter = get_adapter(profile.framework_id)
        context = adapter.inspect_game(root)
        if context is not None:
            # Proof of framework, NOT a choice of destination. Several
            # profiles may exist and picking one is the user's call -- see
            # `_require_chosen_target`, which asks.
            return context

    raise UnsupportedGameError(
        f"No supported modding framework detected at {root}.",
        hints=[
            "ModWright v1 supports BepInEx 5 (Mono Unity games).",
            "Confirm the game is installed and its mod loader has been set up.",
            "If mods are run through a manager (r2modman, Thunderstore Mod "
            "Manager, Gale), make sure a profile for this game exists in it.",
        ],
    )
