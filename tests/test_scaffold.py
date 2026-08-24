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
        assert names == {"MyMod.csproj", "Plugin.cs", ".gitignore"}
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
        csproj = (project / "MyMod.csproj").read_text(encoding="utf-8")
        assert "<GameDir Condition=\"'$(GameDir)' == ''\">" in csproj

    def test_paths_derive_from_game_dir_rather_than_repeating_it(self, scaffolded):
        project, _ = scaffolded()
        csproj = (project / "MyMod.csproj").read_text(encoding="utf-8")
        assert "<GameManagedDir>$(GameDir)\\" in csproj
        assert "<BepInExCoreDir>$(GameDir)\\BepInEx\\core</BepInExCoreDir>" in csproj

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
