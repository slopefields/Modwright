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
    LoaderSession,
    LoggingStatus,
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

    def inspect_game(self, install_root: Path) -> GameContext | None:
        """Return a context if this is a game this framework could mod.

        Answers only "is this the right kind of game", never "where do mods
        go" -- so `mods_dir` comes back unset. Split out from `detect` because
        the loader is frequently NOT in the game folder: a mod manager keeps
        it in a profile elsewhere, leaving the install untouched. Detection
        that demanded a loader beside the game refused those users outright.
        """
        ...

    def detect(self, install_root: Path) -> GameContext | None:
        """Return a context if this adapter handles the install, else None.

        The right kind of game is not sufficient on its own -- RimWorld is
        Unity+Mono and loads mods natively -- so this also requires evidence
        of the framework itself, whether that is a loader tree in the game
        folder or a sign the game is launched against one elsewhere.

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

    def explain_unusable_loader_root(self, loader_root: Path) -> str | None:
        """Say why a tree that was MEANT to be a loader is not one yet.

        Called only for directories the caller already knows were meant to
        hold a loader -- a mod manager's own profile folder -- so it does not
        have to establish intent, only report what is missing. Returns None
        when this framework has nothing to say about the tree, either because
        it is usable or because it is another framework's shape entirely.

        Exists because dropping such a profile from a listing silently is
        worse than refusing it out loud: the one profile a user just created
        and named is the one missing from the list, which reads as the tool
        being wrong rather than the profile being empty. The wording lives
        here because naming what to install -- a package, in the manager's own
        vocabulary -- is framework knowledge the listing must not carry.
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

    def write_project_props(
        self,
        project_path: Path,
        game_context: GameContext,
        packages: list[str],
    ) -> Path:
        """Rewrite the generated file holding this project's build inputs.

        Covers everything ModWright owns and must keep correct: where the game
        is, where the LOADER is, and the references to other installed mods.
        All of those change -- switching deploy target moves the loader -- so
        none may live in the project file, which is written once at scaffold
        time and never rewritten.

        Takes the full set of packages rather than one to add, so the output
        is a function of the config alone -- no accumulated drift between what
        the config says and what the project file happens to contain.

        Callers must resolve the deploy target first: `game_context.mods_dir`
        being None means the loader location is still unknown, and guessing it
        is what silently produced projects that compiled against the wrong
        BepInEx.
        """
        ...

    def extract_patch_targets(self, mod_source_dir: Path) -> list[PatchTarget]:
        """Parse the mod's own source for the targets it claims to patch."""
        ...

    def resolve_log(self, game_context: GameContext) -> Path | None:
        """Return the log file to read now, or None if none exists yet."""
        ...

    def count_loader_starts(self, log_text: str) -> int:
        """Count the loader's own startup banner in a chunk of log text.

        Answers "did a new game process begin" for a caller that only has a
        slice of the log. A mod assembly is loaded once when the process
        starts, so after a redeploy the question is never "was the log
        written" -- a game left running answers that with the OLD build still
        in memory -- but "did it start again since".

        Takes text rather than a path deliberately. The count is only
        meaningful over content read since a known point: BepInEx truncates
        its log on startup, so counting the whole file returns 1 on every
        profile on the machine whether it restarted or not. Passing a slice
        makes that misuse awkward to write.

        Must not raise, and 0 must mean "none in this text", never "cannot
        tell" -- an adapter whose loader prints no such banner should say so
        by returning 0 for everything, since the caller treats absence as
        "not observed" rather than as proof of anything.
        """
        ...

    def read_session(self, log_text: str, plugin_file: str) -> LoaderSession | None:
        """What the log's header says about loading `plugin_file`.

        The absolute answer to "is the running game running the build I just
        deployed", and the reason `count_loader_starts` is now only a
        fallback. A cursor is a byte offset with no timestamp on it, so it can
        say at best that something changed since the last read -- and it
        cannot even do that reliably, because a loader that truncates its log
        and then writes past the old offset leaves neither a shorter file nor
        a banner in the region being read. A timestamp the loader wrote itself
        has no such blind spot.

        Given the TOP of the log rather than a slice since a cursor, for the
        same reason: that is where a loader records what it loaded, and it is
        exactly the region a cursor from a previous session skips past.

        Must not raise, and None must mean "this log does not show it" and
        never "it did not load" -- callers report the difference. An adapter
        for a loader that records nothing usable should return None always,
        which leaves the caller on the fallback rather than on a wrong answer.
        """
        ...

    def inspect_logging(self, loader_root: Path) -> LoggingStatus:
        """Report whether this loader tree would write a log if it ran.

        Reading an empty log as "the game never ran here" is only sound once
        this has been checked: a loader told not to log looks exactly the
        same. The adapter supplies both the check and the wording, because
        every framework keeps this setting in its own file under its own name
        -- putting that prose in the server would leak one framework's
        vocabulary into the layer that is supposed to have none.

        Must not raise. This runs while diagnosing a failure, so an exception
        here replaces a confusing answer with no answer at all; an unreadable
        config is reported as "not known to be off".
        """
        ...
