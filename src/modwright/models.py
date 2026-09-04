"""Shared data types passed between the server tools and framework adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class GameContext:
    """A game install that a known framework adapter has claimed.

    Produced by `ModFrameworkAdapter.detect`. Everything downstream (scaffold,
    build, deploy, log resolution) takes this rather than a bare path, so an
    adapter only has to do its install-root sniffing once.
    """

    install_root: Path
    game_name: str
    framework_id: str

    #: Folder holding the game's managed assemblies, when the framework has a
    #: meaningful one (Unity's `<name>_Data/Managed`). Used to resolve
    #: `<Reference>` paths during scaffolding, and as the assembly path handed
    #: to DecompilerServer. None for frameworks where it does not apply.
    managed_dir: Path | None = None

    #: Where built mods are installed for this game, when the framework has a
    #: single such folder (`BepInEx/plugins`, `Mods/`). None for frameworks
    #: whose build step places the artifact itself (see `BuildOutcome`).
    mods_dir: Path | None = None

    #: Directory holding the *loader* tree, when it lives outside the game
    #: install. Normally None, meaning loader and game share `install_root`.
    #:
    #: Mod managers (r2modman, Thunderstore Mod Manager, Gale) break that
    #: assumption: each profile is a complete standalone loader tree elsewhere
    #: on disk, and the game is launched pointed at it. The game install still
    #: supplies the assemblies to compile and decompile against, so the two
    #: roots have to be tracked separately -- `managed_dir` follows the game,
    #: `mods_dir` and the log follow the loader.
    loader_root: Path | None = None

    @property
    def effective_loader_root(self) -> Path:
        """Where the loader actually lives -- the profile, or the install."""
        return self.loader_root or self.install_root


@dataclass(frozen=True)
class LoaderInfo:
    """What an adapter can tell from a loader tree alone, with no game.

    Mod-manager profiles are loader trees sitting on their own -- no game
    install anywhere near them -- so they have to be understood without the
    usual detection path.
    """

    mods_dir: Path
    #: The loader's log, when it has been written. None means this tree has
    #: never actually run.
    log_path: Path | None = None


@dataclass(frozen=True)
class LoggingStatus:
    """Whether a loader tree is configured to write a log at all.

    Every loader can be told not to, and one that has been is
    indistinguishable from one that never ran -- so an empty log means
    nothing until this has been checked.

    `disabled` is deliberately a plain bool rather than a tri-state: it is
    True only when the setting was positively read as off. A missing or
    unreadable config leaves it False, because loaders default to logging on,
    so "no config" behaves like "on" and must never be reported as off.
    `config_path` being None is what carries "nothing has written a config
    here yet" -- useful as evidence, too weak to conclude from.
    """

    disabled: bool
    #: The file the setting was read from, or None if there is no config yet.
    config_path: Path | None = None
    #: The adapter's own wording for how to turn logging back on. Framework-
    #: specific -- naming this file is the adapter's job, never the server's.
    hint: str | None = None


@dataclass(frozen=True)
class LoadRecording:
    """Whether a loader tree records WHICH file it loaded each plugin from.

    A separate question from `LoggingStatus`, which asks whether a log is
    written at all. A loader can write a perfectly healthy log and still say
    nothing about where the assemblies in it came from -- BepInEx does exactly
    that out of the box, because the lines carrying that detail belong to
    Harmony's `Info` channel and the shipped default listens only to warnings
    and errors.

    That default is why `read_session` comes back empty on most installs: the
    evidence it looks for is real, and simply not printed. This type is what
    lets a caller say so, and offer to turn it on, instead of reporting an
    unexplained "unknown".

    `enabled` is a plain bool rather than a tri-state because both unreadable
    and absent configs mean the same thing here: the detail is not known to be
    on, and the remedy is identical either way. That is the opposite of
    `LoggingStatus.disabled`, where the default is ON and only a positive
    reading may be trusted -- here the default is OFF.
    """

    enabled: bool
    #: The file the setting lives in, or None when there is no config yet.
    config_path: Path | None = None
    #: The setting as it currently reads, for a caller to quote rather than
    #: describe. None when it could not be read.
    setting: str | None = None
    #: The adapter's own wording for what this costs and how to change it.
    #: Framework-specific, so the adapter owns it and the server never
    #: paraphrases it.
    hint: str | None = None


@dataclass(frozen=True)
class LoadRecordingChange:
    """The result of turning plugin-load recording on or off.

    `changed` is False for a no-op -- the setting already read the way it was
    asked to -- which is not a failure and must not be reported as one. An
    agent that calls this defensively before every deploy should see a quiet
    success, not a warning.
    """

    #: What the setting reads now.
    enabled: bool
    #: Whether this call actually rewrote the file.
    changed: bool
    config_path: Path
    #: The value before and after, verbatim, so the caller can show the edit
    #: rather than assert it happened.
    previous: str | None = None
    current: str | None = None


@dataclass(frozen=True)
class BuildOutcome:
    """Result of an adapter's build step.

    `artifact` is None exactly when `deployed_by_build` is True: some
    frameworks (tModLoader, SMAPI) have MSBuild targets that place the final
    artifact themselves, leaving nothing for a separate deploy step to move.
    Callers must branch on `deployed_by_build` rather than assuming a copy is
    always required.
    """

    artifact: Path | None
    deployed_by_build: bool = False
    log: str = ""

    def __post_init__(self) -> None:
        if (self.artifact is None) != self.deployed_by_build:
            raise ValueError(
                "BuildOutcome.artifact must be None if and only if "
                "deployed_by_build is True"
            )


@dataclass(frozen=True)
class DeployOutcome:
    """Where a built mod ended up, and whether this run had to move it."""

    destination: Path
    #: False when the build step had already placed the artifact and deploy
    #: was therefore a no-op.
    copied: bool


@dataclass(frozen=True)
class PatchTarget:
    """A patch target extracted from the MOD's own source.

    Deliberately stringly-typed: this is what the mod *claims* to patch, read
    syntactically out of an attribute. Whether it actually exists in the game
    is decided later by looking it up through DecompilerServer.
    """

    type_name: str | None
    member_name: str | None
    source_file: Path
    line: int
    #: Set when the attribute was found but its arguments could not be read as
    #: literals -- a `const` reference or computed string. Such targets are
    #: reported as unchecked rather than guessed at or silently passed.
    unresolved_reason: str | None = None

    @property
    def display(self) -> str:
        if self.type_name and self.member_name:
            return f"{self.type_name}.{self.member_name}"
        if self.type_name:
            return self.type_name
        if self.member_name:
            return f"?.{self.member_name}"
        return "?"


@dataclass(frozen=True)
class ValidatedTarget:
    """A `PatchTarget` after checking it against the real game assembly."""

    target: PatchTarget
    exists: bool
    #: Populated on a miss, so the agent gets near-matches instead of a dead
    #: end. Mirrors DecompilerServer's own "structured error hints" contract.
    candidates: list[str] = field(default_factory=list)
    member_id: str | None = None


@dataclass(frozen=True)
class LoaderSession:
    """What the log's own header says the running process loaded.

    The answer to "is the game running the build I just deployed?" that does
    not go through a cursor. A cursor is a byte offset with no timestamp on
    it, so it can only ever say "something changed since last time"; this
    says when THIS process started and which file it loaded the mod from,
    which is the question that was being asked all along.

    Both fields are optional because a loader may print one and not the
    other, and because absence has to stay distinguishable from "no". A
    plugin with no patches leaves no timestamp behind, and reporting that as
    "did not restart" is the exact failure this replaces.
    """

    #: When the loader loaded the mod, from the log's own text. None when the
    #: log carries no such stamp -- unknown, never "it did not happen".
    started_at: datetime | None = None
    #: The OTHER time `started_at` could mean, when the loader wrote a stamp
    #: that does not identify itself uniquely. BepInEx's comes from Harmony,
    #: which formats it on a 12-hour clock with no AM/PM marker, so `01.39.17`
    #: is either 01:39 or 13:39 and nothing in the line says which.
    #:
    #: `started_at` holds the reading that best fits when the log was last
    #: written; this holds the one that was rejected. It is not a footnote:
    #: the two are 12 hours apart, so a caller comparing `started_at` against
    #: a build time must check whether the alternative would flip its answer
    #: before presenting that answer as certain. None means the stamp was
    #: unambiguous, or that there was no stamp at all.
    started_at_alternative: datetime | None = None
    #: The file the loader says it loaded the mod from. Compared against the
    #: deploy destination, this catches a stale copy loading out of another
    #: tree, which a startup banner alone cannot see.
    plugin_path: Path | None = None
    #: The mod's name as the loader announced it, for the response to quote.
    plugin_name: str | None = None


@dataclass(frozen=True)
class LogRead:
    """A chunk of log content plus the cursor to resume from."""

    content: str
    cursor: int
    path: Path
    #: The log was shorter than the cursor it was read from, so it was
    #: truncated between the two reads. Loaders truncate their log when they
    #: start, which makes this positive evidence that a NEW process began --
    #: the one thing a timestamp cannot establish, since a process that was
    #: already running keeps appending to the same file after a redeploy.
    #:
    #: False means only "not seen": a log that has already regrown past the
    #: old cursor was truncated without leaving a shorter file behind.
    restarted: bool = False

    #: Loader startups counted in everything that was read, BEFORE `content`
    #: was trimmed to the caller's line budget. Counted there deliberately:
    #: the startup banner sits at the top of a freshly truncated log, which is
    #: the first thing a tail drops -- so trimming the text the agent sees
    #: must not also blind the check for whether a new process began.
    loader_starts: int = 0

    #: Lines read but not returned, because `content` is capped. Reported
    #: rather than dropped quietly: an agent that cannot tell a complete read
    #: from a trimmed one will read a gap as evidence that nothing happened.
    omitted_lines: int = 0

    #: What the log's header says the running process loaded, read from the
    #: top of the file rather than from anything after the cursor. None when
    #: the loader wrote nothing recognisable there.
    session: LoaderSession | None = None
