"""Shared data types passed between the server tools and framework adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
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

    type_name: str
    member_name: str | None
    source_file: Path
    line: int

    @property
    def display(self) -> str:
        if self.member_name is None:
            return self.type_name
        return f"{self.type_name}.{self.member_name}"


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
class LogRead:
    """A chunk of log content plus the cursor to resume from."""

    content: str
    cursor: int
    path: Path
