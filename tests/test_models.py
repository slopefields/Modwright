"""Invariants on the types passed between server tools and adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from modwright.errors import ErrorCode, ModwrightError, UnsupportedGameError
from modwright.models import BuildOutcome, PatchTarget
from modwright.project_config import CONFIG_FILENAME, ProjectConfig
from modwright.errors import ProjectNotFoundError


class TestBuildOutcome:
    """`artifact is None` must mean exactly "the build already deployed it"."""

    def test_flat_copy_build_carries_an_artifact(self):
        outcome = BuildOutcome(artifact=Path("Mod.dll"))
        assert not outcome.deployed_by_build

    def test_build_is_deploy_carries_no_artifact(self):
        outcome = BuildOutcome(artifact=None, deployed_by_build=True)
        assert outcome.artifact is None

    @pytest.mark.parametrize(
        "artifact, deployed",
        [(None, False), (Path("Mod.dll"), True)],
    )
    def test_contradictory_combinations_are_rejected(self, artifact, deployed):
        with pytest.raises(ValueError):
            BuildOutcome(artifact=artifact, deployed_by_build=deployed)


class TestPatchTargetDisplay:
    @pytest.mark.parametrize(
        "type_name, member_name, expected",
        [
            ("EnemyAI", "KillEnemy", "EnemyAI.KillEnemy"),
            ("EnemyAI", None, "EnemyAI"),
            (None, "KillEnemy", "?.KillEnemy"),
            (None, None, "?"),
        ],
    )
    def test_display(self, type_name, member_name, expected):
        target = PatchTarget(
            type_name=type_name,
            member_name=member_name,
            source_file=Path("P.cs"),
            line=1,
        )
        assert target.display == expected


class TestErrorResponses:
    def test_carries_a_stable_machine_checkable_code(self):
        response = UnsupportedGameError("nope").to_response()
        assert response == {
            "success": False,
            "code": ErrorCode.UNSUPPORTED_GAME,
            "error": "nope",
        }

    def test_hints_are_included_when_present(self):
        response = UnsupportedGameError("nope", hints=["try this"]).to_response()
        assert response["hints"] == ["try this"]

    def test_every_error_code_is_unique(self):
        values = [code.value for code in ErrorCode]
        assert len(values) == len(set(values))

    def test_all_errors_derive_from_the_base(self):
        assert issubclass(UnsupportedGameError, ModwrightError)


class TestProjectConfig:
    def test_round_trips(self, tmp_path):
        config = ProjectConfig(
            mod_name="MyMod",
            framework_id="bepinex5",
            install_root=r"C:\Games\Thing",
            game_name="Thing",
        )
        config.save(tmp_path)
        assert ProjectConfig.load(tmp_path) == config

    def test_save_returns_the_written_path(self, tmp_path):
        config = ProjectConfig("M", "bepinex5", "root", "Game")
        assert config.save(tmp_path) == tmp_path / CONFIG_FILENAME

    def test_missing_config_is_a_typed_error_with_a_hint(self, tmp_path):
        with pytest.raises(ProjectNotFoundError) as excinfo:
            ProjectConfig.load(tmp_path)
        assert excinfo.value.hints
