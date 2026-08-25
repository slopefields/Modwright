"""Per-project config: how a mod project remembers what it targets.

ModWright keeps no global database. Each mod project is self-describing via a
`.modwright.json` file in its own directory, so projects stay portable, survive
being moved or cloned, and never desynchronise from a central index.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from modwright.errors import ProjectNotConfiguredError, ProjectNotFoundError

#: Committed with the mod. Holds what the project IS -- which game, which
#: framework, which dependencies -- so a clone knows what it is looking at and
#: what it needs, with no path in it anywhere.
CONFIG_FILENAME = ".modwright.json"

#: NOT committed. Holds the two fields that describe one person's disk. A mod
#: repo pushed to a public host must not publish where its author keeps their
#: games: `deploy_root` alone would give away the username, the mod manager in
#: use, and the profile name.
LOCAL_CONFIG_FILENAME = ".modwright.local.json"

#: Which fields go in the local file. Deliberately the paths and nothing else.
_LOCAL_FIELDS = ("install_root", "deploy_root")


@dataclass
class ModReference:
    """A mod this project compiles against.

    Only the package name and what was true *at the time it was added* are
    stored. What is installed now is deliberately NOT cached: it is a
    millisecond away in the profile, and a stored copy would go stale the
    moment the user updates the mod, leaving two disagreeing answers to the
    same question. What cannot be recovered later is what things looked like
    when the code was written -- so that is what gets recorded.

    Both fields are kept, each filled whenever it is knowable, because they
    answer different questions: the version is "which release did I write
    against" (read by the drift warning, and later by a packaging manifest's
    dependency line), while the timestamp is "has this file moved underneath
    me" (read only when a build fails). A dependency installed as a bare
    `.dll` has no manifest and so no version; that absence means "not known"
    and must not be repurposed to signal which shape the dependency came in.
    """

    package: str
    version_when_added: str | None = None
    #: Modification time of the newest assembly this reference points at, as
    #: of when it was added. Catches what a version cannot: a locally rebuilt
    #: DLL swapped into an installed package leaves the manifest reading the
    #: same version while the assembly is entirely different.
    assembly_mtime_when_added: float | None = None


@dataclass
class ProjectConfig:
    mod_name: str
    framework_id: str
    install_root: str
    game_name: str
    #: Loader tree to deploy into, when it is not the game install -- a mod
    #: manager profile. Optional and defaulted so configs written before this
    #: existed still load.
    deploy_root: str | None = None
    #: Other mods this project compiles against.
    references: list[ModReference] = field(default_factory=list)

    def save(self, project_path: Path) -> Path:
        """Write both halves: the shared config and this machine's paths."""
        shared = {
            key: value
            for key, value in asdict(self).items()
            if key not in _LOCAL_FIELDS
        }
        _write_json(project_path / CONFIG_FILENAME, shared)
        _write_json(
            project_path / LOCAL_CONFIG_FILENAME,
            {key: getattr(self, key) for key in _LOCAL_FIELDS},
        )
        return project_path / CONFIG_FILENAME

    @classmethod
    def load_shared(cls, project_path: Path) -> ProjectConfig:
        """Load without requiring this machine's paths to be set yet.

        For the one operation that exists to SUPPLY those paths. Everything
        else must use `load`, which refuses a project it cannot locate on
        disk rather than proceeding with an empty install root.
        """
        return cls(**cls._read(project_path))

    @classmethod
    def load(cls, project_path: Path) -> ProjectConfig:
        kwargs = cls._read(project_path)
        if not kwargs.get("install_root"):
            # The project is real but has never been pointed at anything here.
            # This is the ordinary state of a freshly cloned mod repo, not a
            # corrupt one: the paths live only on the machine that set them.
            raise ProjectNotConfiguredError(
                f"{project_path.name} has no {LOCAL_CONFIG_FILENAME}, so it has "
                "not been set up on this machine yet.",
                hints=[
                    "Ask the user where this game is installed, then pass it "
                    "to set_game_install.",
                    "If they run mods through a manager, ask which profile too "
                    "and pass it to set_deploy_target.",
                    f"{LOCAL_CONFIG_FILENAME} holds only local paths and is "
                    "deliberately not committed, so every clone sets its own.",
                ],
            )
        return cls(**kwargs)

    @classmethod
    def _read(cls, project_path: Path) -> dict:
        """Merge the committed config with this machine's local paths."""
        path = project_path / CONFIG_FILENAME
        if not path.exists():
            raise ProjectNotFoundError(
                f"No {CONFIG_FILENAME} in {project_path}.",
                hints=["Run scaffold_mod_project first, or pass the project root."],
            )
        data = json.loads(path.read_text(encoding="utf-8"))

        local_path = project_path / LOCAL_CONFIG_FILENAME
        if local_path.exists():
            data.update(json.loads(local_path.read_text(encoding="utf-8")))

        # Ignore unknown keys rather than crashing: a project written by a
        # newer ModWright should still open in an older one.
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("install_root", "")
        kwargs["references"] = [
            ModReference(**{
                k: v for k, v in entry.items()
                if k in {f.name for f in fields(ModReference)}
            })
            for entry in kwargs.get("references") or []
        ]
        return kwargs


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
