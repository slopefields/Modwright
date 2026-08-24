"""Per-project config: how a mod project remembers what it targets.

ModWright keeps no global database. Each mod project is self-describing via a
`.modwright.json` file in its own directory, so projects stay portable, survive
being moved or cloned, and never desynchronise from a central index.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from modwright.errors import ProjectNotFoundError

CONFIG_FILENAME = ".modwright.json"


@dataclass
class ProjectConfig:
    mod_name: str
    framework_id: str
    install_root: str
    game_name: str

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
        return cls(**data)
