"""Project scaffolding.

Generating the project needs only a plausible install tree, so most of this
runs anywhere. Actually compiling the result is in `test_integration.py`.
"""

from __future__ import annotations

import pytest

from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.errors import InvalidModNameError, ProjectExistsError


@pytest.fixture()
def scaffolded(fake_game, tmp_path):
    def _scaffold(mod_name: str = "MyMod", **game_kwargs):
        context = detect_framework(fake_game("Lethal Company", **game_kwargs))
        project = tmp_path / "project"
        written = BepInEx5Adapter().scaffold(project, context, mod_name)
        return project, written

    return _scaffold


class TestGeneratedFiles:
    def test_writes_a_project_a_plugin_and_a_gitignore(self, scaffolded):
        project, written = scaffolded("MyMod")
        names = {path.name for path in written}
        assert names == {
            "MyMod.csproj",
            "Plugin.cs",
            ".gitignore",
            "Modwright.props",
        }
        assert all(path.exists() for path in written)

    def test_project_is_named_after_the_mod(self, scaffolded):
        project, _ = scaffolded("CoolMod")
        assert (project / "CoolMod.csproj").exists()

    def test_creates_missing_parent_directories(self, fake_game, tmp_path):
        context = detect_framework(fake_game("Lethal Company"))
        nested = tmp_path / "a" / "b" / "c"
        BepInEx5Adapter().scaffold(nested, context, "MyMod")
        assert (nested / "MyMod.csproj").exists()


class TestProjectReferences:
    def test_game_path_is_an_overridable_property(self, scaffolded):
        """Hardcoding the install path would break the project on any other
        machine; GameDir can be overridden with -p:GameDir=..."""
        project, _ = scaffolded()
        props = (project / "Modwright.props").read_text(encoding="utf-8")
        assert "<GameDir Condition=\"'$(GameDir)' == ''\">" in props

    def test_machine_specific_paths_are_not_in_the_project_file(self, scaffolded):
        """The .csproj is written once and never rewritten, so a path baked
        into it goes stale the moment the deploy target changes. Paths belong
        in the generated file, which is rewritten from the context."""
        project, _ = scaffolded()
        csproj = (project / "MyMod.csproj").read_text(encoding="utf-8")

        assert "<GameDir" not in csproj
        assert "<LoaderDir" not in csproj
        assert "Modwright.props" in csproj

    def test_paths_derive_from_their_roots_rather_than_repeating_them(self, scaffolded):
        project, _ = scaffolded()
        props = (project / "Modwright.props").read_text(encoding="utf-8")
        assert "<GameManagedDir>$(GameDir)\\" in props
        assert "<BepInExCoreDir>$(LoaderDir)\\BepInEx\\core</BepInExCoreDir>" in props

    def test_the_loader_is_not_assumed_to_live_inside_the_game(self, scaffolded):
        """BepInEx comes from LoaderDir, never GameDir. With a mod manager the
        two differ, and compiling against the game folder's copy while running
        the profile's is a silent version mismatch."""
        project, _ = scaffolded()
        props = (project / "Modwright.props").read_text(encoding="utf-8")
        assert "$(GameDir)\\BepInEx" not in props

    def test_references_the_loader_and_game_assemblies(self, scaffolded):
        project, _ = scaffolded()
        csproj = (project / "MyMod.csproj").read_text(encoding="utf-8")
        for expected in (
            "$(BepInExCoreDir)\\BepInEx.dll",
            "$(BepInExCoreDir)\\0Harmony.dll",
            "$(GameManagedDir)\\Assembly-CSharp*.dll",
            "$(GameManagedDir)\\UnityEngine*.dll",
        ):
            assert expected in csproj

    def test_game_assemblies_are_not_copied_to_output(self, scaffolded):
        """Without Private="false" the whole game would land in plugins/."""
        project, _ = scaffolded()
        csproj = (project / "MyMod.csproj").read_text(encoding="utf-8")
        for line in csproj.splitlines():
            if "<Reference Include=" in line:
                assert 'Private="false"' in line

    def test_target_framework_follows_the_game_profile(self, scaffolded):
        project, _ = scaffolded("MyMod", net_framework_profile=True)
        assert "<TargetFramework>net472<" in (
            project / "MyMod.csproj"
        ).read_text(encoding="utf-8")


class TestGeneratedPlugin:
    def test_uses_the_mod_name_for_namespace_and_plugin_metadata(self, scaffolded):
        project, _ = scaffolded("CoolMod")
        plugin = (project / "Plugin.cs").read_text(encoding="utf-8")
        assert "namespace CoolMod;" in plugin
        assert 'PluginName = "CoolMod"' in plugin

    def test_wires_up_harmony(self, scaffolded):
        project, _ = scaffolded()
        plugin = (project / "Plugin.cs").read_text(encoding="utf-8")
        assert "using HarmonyLib;" in plugin
        assert "PatchAll()" in plugin

    def test_plugin_guid_is_unique_per_mod(self, scaffolded):
        project, _ = scaffolded("CoolMod")
        plugin = (project / "Plugin.cs").read_text(encoding="utf-8")
        assert "coolmod" in plugin


class TestRefusals:
    @pytest.mark.parametrize("name", ["9Mod", "my-mod", "my mod", "", "Mod!"])
    def test_names_that_are_not_valid_c_sharp_identifiers(self, scaffolded, name):
        with pytest.raises(InvalidModNameError):
            scaffolded(name)

    def test_does_not_overwrite_an_existing_project(self, scaffolded, fake_game):
        project, _ = scaffolded("MyMod")
        context = detect_framework(fake_game("Lethal Company"))
        with pytest.raises(ProjectExistsError):
            BepInEx5Adapter().scaffold(project, context, "MyMod")


class TestTheDeployTargetIsNotAnArgument:
    """Scaffolding cannot be TOLD a deploy target, only answered one.

    It used to take one, which meant an agent could pass a profile carried
    over from whatever project it worked on last -- and that is exactly what
    happened: a new project silently inherited the previous one's profile and
    had to be moved by hand afterwards. The refusal in `deploy_mod` exists to
    force that choice, and an optional argument here walked straight past it.

    The user is now asked directly instead (see `test_confirmation.py`), which
    is the same decision made by the one party entitled to make it. Left
    unanswered -- or asked of a client that cannot answer -- the target stays
    unset and `deploy_mod` still refuses.

    Only one profile is active per launch, so the wrong one builds, deploys
    and then loads nothing.
    """

    def test_scaffolding_takes_no_deploy_target(self, fake_game, tmp_path):
        import inspect

        from modwright import server

        parameters = inspect.signature(server.scaffold_mod_project).parameters
        assert "deploy_root" not in parameters

    def test_a_new_project_starts_with_no_target(self, fake_game, tmp_path):
        import asyncio

        from modwright import server

        result = asyncio.run(
            server.scaffold_mod_project(
                str(fake_game("Game")), str(tmp_path / "proj"), "MyMod"
            )
        )

        assert result["success"] is True
        assert result["deploy_root"] is None

    def test_the_pending_choice_is_stated_at_scaffold_time(
        self, fake_game, tmp_path
    ):
        """Rather than waiting for `deploy_mod` to refuse. The decision is the
        user's, and it is cheapest to make while they are already deciding
        things about this project."""
        import asyncio

        from modwright import server

        result = asyncio.run(
            server.scaffold_mod_project(
                str(fake_game("Game")), str(tmp_path / "proj"), "MyMod"
            )
        )

        assert result["deploy_target_required"] is True
        assert any("set_deploy_target" in hint for hint in result["hints"])
        assert any("per project" in hint for hint in result["hints"])
