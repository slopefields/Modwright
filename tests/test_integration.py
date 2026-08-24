"""End-to-end tests against a real game install.

These need Lethal Company (or another supported game), the .NET SDK, and a
DecompilerServer build, so they skip themselves when those are missing. They
are the only tests that prove the pieces work against real data rather than
fixtures.

Deploy is exercised against a synthetic install rather than the real
`BepInEx/plugins`, so running the suite never writes into a real game.
"""

from __future__ import annotations

import json

import pytest

from modwright import server
from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.project_config import ProjectConfig

pytestmark = pytest.mark.game


@pytest.fixture()
def real_project(game_install, tmp_path):
    """A scaffolded project targeting the real installed game."""
    project = tmp_path / "TestMod"
    result = server.scaffold_mod_project(str(game_install), str(project), "TestMod")
    assert result["success"], result
    return project


class TestDetection:
    def test_identifies_the_installed_game(self, game_install):
        result = server.detect_game(str(game_install))
        assert result["success"]
        assert result["framework_id"] == "bepinex5"
        assert result["mods_dir"].endswith("plugins")


@pytest.mark.dotnet
class TestBuild:
    def test_scaffolded_project_compiles(self, real_project, requires_dotnet):
        result = server.build_mod(str(real_project))
        assert result["success"], result
        assert result["artifact"].endswith("TestMod.dll")
        assert result["deploy_required"] is True

    def test_build_reports_compiler_errors_rather_than_msbuild_noise(
        self, real_project, requires_dotnet
    ):
        (real_project / "Broken.cs").write_text(
            "class Broken { void M() { return undefined_symbol; } }",
            encoding="utf-8",
        )
        result = server.build_mod(str(real_project))

        assert not result["success"]
        assert result["code"] == "build_failed"
        assert any("error CS" in hint for hint in result["hints"])

    def test_deploy_places_the_dll_without_the_game_assemblies(
        self, real_project, fake_game, requires_dotnet
    ):
        outcome = BepInEx5Adapter().build(real_project)
        destination = fake_game("Target")
        context = detect_framework(destination)

        deployed = BepInEx5Adapter().deploy(outcome, context)

        assert deployed.copied
        assert deployed.destination.name == "TestMod.dll"
        plugins = list(context.mods_dir.iterdir())
        assert [p.name for p in plugins] == ["TestMod.dll"]


@pytest.mark.decompiler
class TestValidation:
    async def test_real_targets_resolve(
        self, game_install, tmp_path, requires_decompiler
    ):
        project = _project_targeting(game_install, tmp_path, """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch("KillEnemy")]
    static void Postfix() { }
}
""")
        result = await server.validate_mod_patches(str(project))

        assert result["success"]
        assert result["valid"]
        assert result["checked"] == 1

    async def test_misspelled_method_is_caught_with_a_suggestion(
        self, game_install, tmp_path, requires_decompiler
    ):
        """The failure this tool exists for: Harmony takes the method name as a
        string, so the compiler accepts a name that does not exist."""
        project = _project_targeting(game_install, tmp_path, """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch("KilEnemy")]
    static void Postfix() { }
}
""")
        result = await server.validate_mod_patches(str(project))

        assert not result["valid"]
        assert result["missing"][0]["target"] == "EnemyAI.KilEnemy"
        assert "KillEnemy" in result["missing"][0]["did_you_mean"]

    async def test_unrelated_name_suggests_nothing_rather_than_noise(
        self, game_install, tmp_path, requires_decompiler
    ):
        """Offering alphabetically-first members as "did you mean" would be
        worse than offering nothing."""
        project = _project_targeting(game_install, tmp_path, """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch("CompletelyUnrelatedName")]
    static void Postfix() { }
}
""")
        result = await server.validate_mod_patches(str(project))

        assert not result["valid"]
        assert result["missing"][0]["did_you_mean"] == []

    async def test_non_literal_target_is_reported_as_unchecked(
        self, game_install, tmp_path, requires_decompiler
    ):
        project = _project_targeting(game_install, tmp_path, """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch(Constants.Name)]
    static void Postfix() { }
}
""")
        result = await server.validate_mod_patches(str(project))

        assert result["unchecked"]
        assert result["checked"] == 0


class TestLogs:
    def test_reads_the_loader_log_and_returns_a_cursor(
        self, game_install, real_project
    ):
        result = server.watch_mod_logs(str(real_project), lines=5)

        if not result["success"]:
            # The game has never been run with BepInEx installed.
            assert result["code"] == "log_not_found"
            pytest.skip("no LogOutput.log yet")

        assert result["log_path"].endswith("LogOutput.log")
        assert result["cursor"] > 0


def _project_targeting(game_install, tmp_path, code: str):
    """A minimal project directory with config and one source file."""
    project = tmp_path / "validate"
    project.mkdir(exist_ok=True)
    (project / "Patches.cs").write_text(code, encoding="utf-8")
    ProjectConfig(
        mod_name="V",
        framework_id="bepinex5",
        install_root=str(game_install),
        game_name=game_install.name,
    ).save(project)
    return project
