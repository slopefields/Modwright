"""Per-project config: how a mod project remembers what it targets.

ModWright keeps no global database. Each mod project is self-describing via a
`.modwright.json` file in its own directory, so projects stay portable, survive
being moved or cloned, and never desynchronise from a central index.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from modwright.errors import ProjectNotFoundError

CONFIG_FILENAME = ".modwright.json"


@dataclass
class ModReference:
    """A mod this project compiles against.

    Only the package folder name and the version *at the time it was added*
    are stored. The version currently installed is deliberately NOT cached:
    it is a millisecond away in the profile, and a stored copy would go stale
    the moment the user updates the mod, leaving two disagreeing answers to
    the same question. What cannot be recovered later is what the version was
    when the code was written against it -- so that is what gets recorded.
    """

    package: str
    version_when_added: str | None = None


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
        path = project_path / CONFIG_FILENAME
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, project_path: Path) -> ProjectConfig:
        path = project_path / CONFIG_FILENAME
        if not path.exists():
            raise ProjectNotFoundError(
                f"No {CONFIG_FILENAME} in {project_path}.",
                hints=["Run scaffold_mod_project first, or pass the project root."],
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        # Ignore unknown keys rather than crashing: a project written by a
        # newer ModWright should still open in an older one.
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["references"] = [
            ModReference(**{
                k: v for k, v in entry.items()
                if k in {f.name for f in fields(ModReference)}
            })
            for entry in kwargs.get("references") or []
        ]
        return cls(**kwargs)
