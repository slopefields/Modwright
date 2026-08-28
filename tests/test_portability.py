"""Keeping one person's disk out of a shared mod repo.

A mod repo pushed anywhere public must not publish where its author keeps
their games. The deploy path alone would give away the username, the mod
manager in use, and the profile name. So the project splits by what is true
of the PROJECT (committed) versus what is true of THIS MACHINE (never
committed, regenerated on demand).
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from modwright import server
from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import PROPS_FILENAME, BepInEx5Adapter
from modwright.project_config import (
    CONFIG_FILENAME,
    LOCAL_CONFIG_FILENAME,
    ProjectConfig,
)


@pytest.fixture()
def project(fake_game, tmp_path):
    """A scaffolded project, as the machine that created it sees it."""
    game = fake_game("Game")
    context = detect_framework(game)
    path = tmp_path / "proj"
    BepInEx5Adapter().scaffold(path, context, "MyMod")
    ProjectConfig("MyMod", "bepinex5", str(game), "Game").save(path)
    return path, game


@pytest.fixture()
def cloned(project, tmp_path):
    """The same project as somebody else receives it.

    Built by deleting exactly what the generated .gitignore excludes, so this
    stays honest if that list ever changes.
    """
    path, game = project
    clone = tmp_path / "clone"
    shutil.copytree(path, clone)
    for line in (path / ".gitignore").read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            (clone / entry.rstrip("/")).unlink(missing_ok=True)
    return clone, game


class TestWhatGetsCommitted:
    def test_no_absolute_path_is_committed(self, project):
        path, game = project
        committed = (path / CONFIG_FILENAME).read_text(encoding="utf-8")

        assert str(game) not in committed
        assert "install_root" not in committed
        assert "deploy_root" not in committed
        assert "deployed_artifact" not in committed

    def test_what_the_mod_needs_is_committed(self, project):
        """A clone has to know what it is looking at. The game, framework and
        dependency list say that without naming a single directory."""
        path, _ = project
        data = json.loads((path / CONFIG_FILENAME).read_text(encoding="utf-8"))

        assert data["mod_name"] == "MyMod"
        assert data["game_name"] == "Game"
        assert data["framework_id"] == "bepinex5"
        assert "references" in data

    def test_the_paths_go_in_the_local_file(self, project):
        path, game = project
        local = json.loads((path / LOCAL_CONFIG_FILENAME).read_text(encoding="utf-8"))

        assert local["install_root"] == str(game)
        assert set(local) == {"install_root", "deploy_root", "deployed_artifact"}

    def test_both_local_files_are_gitignored(self, project):
        path, _ = project
        ignored = (path / ".gitignore").read_text(encoding="utf-8")

        assert LOCAL_CONFIG_FILENAME in ignored
        assert PROPS_FILENAME in ignored

    def test_reloading_sees_the_two_halves_as_one(self, project):
        path, game = project
        assert ProjectConfig.load(path).install_root == str(game)


class TestACloneWithoutPaths:
    def test_tools_refuse_with_an_actionable_reason(self, cloned):
        """Not an error to retry: nobody could have known these paths, so the
        answer is to ask rather than to guess."""
        clone, _ = cloned
        result = asyncio.run(server.build_mod(str(clone)))

        assert result["code"] == "project_not_configured"
        assert any("set_game_install" in h for h in result["hints"])

    def test_pointing_it_at_a_game_makes_it_work(self, cloned):
        clone, game = cloned
        result = server.set_game_install(str(clone), str(game))

        assert result["success"]
        assert result["configured"]
        assert (clone / PROPS_FILENAME).exists()
        assert ProjectConfig.load(clone).install_root == str(game)

    def test_the_regenerated_paths_are_the_new_machines(self, cloned, fake_game):
        """The point of not committing them: a second person's game is
        somewhere else entirely."""
        clone, original = cloned
        # Not "Game2": the original path would be a prefix of it, and the
        # "old path is gone" check below would pass for the wrong reason.
        elsewhere = fake_game("Elsewhere")

        server.set_game_install(str(clone), str(elsewhere))

        props = (clone / PROPS_FILENAME).read_text(encoding="utf-8")
        assert str(elsewhere) in props
        assert str(original) not in props

    def test_a_missing_props_file_fails_loudly_in_the_build_itself(self, cloned):
        """Even outside ModWright. Without this the paths resolve to nothing
        and MSBuild reports missing types, which says nothing about the cause."""
        clone, _ = cloned
        csproj = next(clone.glob("*.csproj")).read_text(encoding="utf-8")

        assert "ModwrightRequireProps" in csproj
        assert "<Error" in csproj


class TestPathsStayInStep:
    def test_switching_target_moves_what_the_build_compiles_against(
        self, project, fake_profile
    ):
        """The loader moving means BepInEx moves with it. Leaving the old path
        behind is the silent version mismatch this split exists to prevent."""
        path, _ = project
        other = fake_profile("other", game_folder="Game")

        server.set_deploy_target(str(path), str(other))

        props = (path / PROPS_FILENAME).read_text(encoding="utf-8")
        assert f"<LoaderDir Condition=\"'$(LoaderDir)' == ''\">{other}" in props

    def test_a_refused_switch_changes_nothing(
        self, project, fake_profile, installed_mod
    ):
        """Switching to a target missing a referenced mod is refused. It must
        leave the config and the generated file agreeing, not half-applied."""
        path, _ = project
        config = ProjectConfig.load(path)
        installed_mod(
            detect_framework(config.install_root).mods_dir, "a-Lib", {"L.dll": "managed"}
        )
        server.add_mod_reference(str(path), "a-Lib")
        before = (path / PROPS_FILENAME).read_text(encoding="utf-8")

        empty = fake_profile("empty", game_folder="Game")
        result = server.set_deploy_target(str(path), str(empty))

        assert result["success"] is False
        assert (path / PROPS_FILENAME).read_text(encoding="utf-8") == before
        assert ProjectConfig.load(path).deploy_root is None
