"""The contract every modding-framework adapter implements.

One adapter per *modding framework*, not per game. The five server tools are
framework-agnostic and delegate everything framework-specific to whichever
adapter claimed the install.

Three properties of this interface exist because of real differences found
across the frameworks on the roadmap -- do not "simplify" them away:

* `build` returns a `BuildOutcome` rather than a path, because tModLoader and
  SMAPI place the final artifact during the build itself. For those, deploy is
  a genuine no-op, not a copy to a different folder.
* `resolve_log` returns a path *computed at call time* rather than a static
  attribute, because Everest writes rotating `log_*.txt` files -- "the log" is
  whichever is newest, not a fixed name.
* `detect` returns an Optional[GameContext] rather than a bool, so an adapter
  that needs a per-game signature (Hollow Knight, Beat Saber) or a hardcoded
  registry (RimWorld, Cities: Skylines, KSP1) can do that work once and hand
  back everything it learned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from modwright.models import (
    BuildOutcome,
    DeployOutcome,
    GameContext,
    LoaderInfo,
    PatchTarget,
)


@runtime_checkable
class ModFrameworkAdapter(Protocol):
    """A modding framework ModWright knows how to drive end to end."""

    #: Stable identifier persisted into each mod project's config.
    framework_id: str
    #: Human-readable name for tool responses.
    display_name: str
    #: Whether mods for this framework can be installed somewhere other than
    #: the game install -- a mod-manager profile.
    #:
    #: False for frameworks where the question does not arise: tModLoader and
    #: SMAPI place the artifact during the build, so there is no destination
    #: to choose. Adapters with this False must refuse `adopt_loader_root`
    #: rather than half-supporting it, and callers must not ask the user to
    #: pick a target they cannot use.
    supports_deploy_target: bool

    def detect(self, install_root: Path) -> GameContext | None:
        """Return a context if this adapter handles the install, else None.

        Must not raise for a merely-unrecognised install -- returning None lets
        the registry try the next adapter. Raise only for a positively wrong
        situation the user needs told about, such as an IL2CPP game whose real
        code no IL decompiler can read.
        """
        ...

    def inspect_loader_root(self, loader_root: Path) -> LoaderInfo | None:
        """Describe a standalone loader tree, or None if it is not this one's.

        Lets profile discovery stay framework-agnostic: what a loader tree
        looks like -- `BepInEx/core` here, `Mods/` and `MelonLoader/` for
        MelonLoader -- is knowledge that belongs in each adapter, not in a
        shared scanner that would otherwise hardcode whichever framework came
        first.

        Recognition must mean *usable*, not merely present: a tree missing the
        loader itself accepts a deployed file and loads nothing.
        """
        ...

    def adopt_loader_root(
        self, game_context: GameContext, loader_root: Path
    ) -> GameContext:
        """Point deploy and log resolution at a loader tree outside the game.

        Mod managers keep each profile as a standalone loader tree, so the
        install that owns the assemblies is not the install that loads mods.
        The returned context must keep `managed_dir` on the game (that is
        where the assemblies are) while moving `mods_dir` onto `loader_root`.

        Must raise `InvalidDeployRootError` if the path is not a loader tree
        this adapter recognises, rather than creating one: a mistyped path
        would otherwise silently become a directory nothing ever reads.
        """
        ...

    def scaffold(
        self, project_path: Path, game_context: GameContext, mod_name: str
    ) -> list[Path]:
        """Create a buildable mod project. Returns the files written."""
        ...

    def build(self, project_path: Path) -> BuildOutcome:
        """Compile the mod. See `BuildOutcome` for the build-is-deploy case."""
        ...

    def deploy(
        self, outcome: BuildOutcome, game_context: GameContext
    ) -> DeployOutcome:
        """Place a built artifact where the framework loads mods from.

        Must be a no-op returning `copied=False` when `outcome.deployed_by_build`
        is set.
        """
        ...

    def extract_patch_targets(self, mod_source_dir: Path) -> list[PatchTarget]:
        """Parse the mod's own source for the targets it claims to patch."""
        ...

    def resolve_log(self, game_context: GameContext) -> Path | None:
        """Return the log file to read now, or None if none exists yet."""
        ...
