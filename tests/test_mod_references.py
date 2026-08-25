"""Compiling a mod against another installed mod.

The dependency is referenced where the mod manager already installed it, so
the build matches the version the game will load. The awkward part is that a
package is a folder whose contents vary: the assembly may be nested, named
nothing like the package, and sitting next to native libraries and framework
assemblies that must not be referenced.
"""

from __future__ import annotations

import json
import shutil

import pytest

from modwright import server
from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import REFERENCES_FILENAME, BepInEx5Adapter
from modwright.mods import find_installed_mod, list_installed_mods, read_installed_mod
from modwright.project_config import ModReference, ProjectConfig
from modwright.server import _reference_drift


@pytest.fixture()
def project(fake_game, tmp_path):
    """A scaffolded project whose mods directory can be populated."""
    game = fake_game("Game")
    context = detect_framework(game)
    path = tmp_path / "proj"
    BepInEx5Adapter().scaffold(path, context, "MyMod")
    ProjectConfig("MyMod", "bepinex5", str(game), "Game").save(path)
    return path, context


class TestFindingAssemblies:
    def test_finds_a_nested_assembly(self, installed_mod, tmp_path):
        """Layout varies: some packages put the DLL a folder deep."""
        package = installed_mod(
            tmp_path / "plugins", "author-Lib", {"Lib/Some.Name.dll": "managed"}
        )
        mod = read_installed_mod(package)
        assert [a.name for a in mod.assemblies] == ["Some.Name.dll"]

    def test_assembly_name_need_not_resemble_the_package(self, installed_mod, tmp_path):
        """DawnLib ships com.github.teamxiaolan.dawnlib.dll, which is why the
        'main' assembly cannot be picked out by name."""
        package = installed_mod(
            tmp_path / "plugins",
            "TeamXiaolan-DawnLib",
            {"DawnLib/com.github.teamxiaolan.dawnlib.dll": "managed"},
        )
        assert read_installed_mod(package).referenceable

    def test_native_libraries_are_excluded(self, installed_mod, tmp_path):
        """Referencing a native DLL fails the build outright. Mods ship them
        right beside their managed code: opus.dll next to OpusDotNet.dll."""
        package = installed_mod(
            tmp_path / "plugins",
            "qwbarch-OpusDotNet",
            {"OpusDotNet.dll": "managed", "opus.dll": "native"},
        )
        mod = read_installed_mod(package)

        assert [a.name for a in mod.assemblies] == ["OpusDotNet.dll"]
        assert [s.path.name for s in mod.skipped] == ["opus.dll"]
        assert "native" in mod.skipped[0].reason

    def test_framework_assemblies_are_excluded(self, installed_mod, tmp_path):
        """Mods bundle System.Memory and friends because Unity's Mono profile
        does not ship them. A mod project needing those should take the NuGet
        package rather than borrow a neighbour's copy."""
        package = installed_mod(
            tmp_path / "plugins",
            "qwbarch-Concentus",
            {
                "Concentus.dll": "managed",
                "System.Memory.dll": "managed",
                "System.Buffers.dll": "managed",
            },
        )
        mod = read_installed_mod(package)

        assert [a.name for a in mod.assemblies] == ["Concentus.dll"]
        assert {s.path.name for s in mod.skipped} == {
            "System.Memory.dll",
            "System.Buffers.dll",
        }

    def test_package_with_nothing_referenceable(self, installed_mod, tmp_path):
        package = installed_mod(
            tmp_path / "plugins", "someone-NativeOnly", {"thing.dll": "native"}
        )
        assert not read_installed_mod(package).referenceable

    def test_version_comes_from_the_package_manifest(self, installed_mod, tmp_path):
        package = installed_mod(
            tmp_path / "plugins", "a-B", {"B.dll": "managed"}, version="3.4.2"
        )
        assert read_installed_mod(package).version == "3.4.2"

    def test_package_without_a_manifest_still_works(self, installed_mod, tmp_path):
        package = installed_mod(
            tmp_path / "plugins", "MMHOOK", {"MMHOOK_X.dll": "managed"}, version=None
        )
        mod = read_installed_mod(package)
        assert mod.version is None
        assert mod.referenceable


class TestLookup:
    @pytest.fixture()
    def mods_dir(self, installed_mod, tmp_path):
        plugins = tmp_path / "plugins"
        installed_mod(plugins, "xilophor-StaticNetcodeLib", {"X.S.dll": "managed"})
        return plugins

    @pytest.mark.parametrize(
        "query",
        [
            "xilophor-StaticNetcodeLib",
            "XILOPHOR-STATICNETCODELIB",
            "StaticNetcodeLib",
        ],
    )
    def test_accepts_the_forms_people_actually_use(self, mods_dir, query):
        """Nobody says the author prefix out loud."""
        assert find_installed_mod(mods_dir, query) is not None

    def test_unknown_package_is_none(self, mods_dir):
        assert find_installed_mod(mods_dir, "NotInstalled") is None

    def test_listing_skips_loose_files(self, mods_dir):
        (mods_dir / "SomeMod.dll").write_bytes(b"")
        assert [m.package for m in list_installed_mods(mods_dir)] == [
            "xilophor-StaticNetcodeLib"
        ]


class TestGeneratedReferenceFile:
    def test_written_beside_the_project_and_imported(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib/Lib.dll": "managed"})

        BepInEx5Adapter().write_mod_references(path, context, ["a-Lib"])

        assert (path / REFERENCES_FILENAME).exists()
        csproj = (path / "MyMod.csproj").read_text(encoding="utf-8")
        assert REFERENCES_FILENAME in csproj

    def test_references_are_relative_to_an_overridable_property(
        self, project, installed_mod
    ):
        """Same treatment as GameDir: the machine-specific path appears once."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib/Lib.dll": "managed"})

        BepInEx5Adapter().write_mod_references(path, context, ["a-Lib"])
        props = (path / REFERENCES_FILENAME).read_text(encoding="utf-8")

        assert "<ModsDir Condition=" in props
        assert "$(ModsDir)" in props
        assert "a-Lib" in props

    def test_dependencies_are_not_copied_to_output(self, project, installed_mod):
        """BepInEx loads each mod itself; a copy beside ours would be a second
        instance of the same assembly."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})

        BepInEx5Adapter().write_mod_references(path, context, ["a-Lib"])
        props = (path / REFERENCES_FILENAME).read_text(encoding="utf-8")

        for line in props.splitlines():
            if "<Reference Include=" in line:
                assert 'Private="false"' in line

    def test_rewritten_wholesale_rather_than_appended(self, project, installed_mod):
        """Generated from the config alone, so the file cannot drift out of
        step with what the project says it references."""
        path, context = project
        installed_mod(context.mods_dir, "a-One", {"One.dll": "managed"})
        installed_mod(context.mods_dir, "a-Two", {"Two.dll": "managed"})
        adapter = BepInEx5Adapter()

        adapter.write_mod_references(path, context, ["a-One"])
        adapter.write_mod_references(path, context, ["a-Two"])
        props = (path / REFERENCES_FILENAME).read_text(encoding="utf-8")

        assert "Two.dll" in props
        assert "One.dll" not in props

    def test_removing_the_last_reference_deletes_the_file(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        adapter = BepInEx5Adapter()

        adapter.write_mod_references(path, context, ["a-Lib"])
        adapter.write_mod_references(path, context, [])

        assert not (path / REFERENCES_FILENAME).exists()

    def test_import_is_not_duplicated(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        adapter = BepInEx5Adapter()

        adapter.write_mod_references(path, context, ["a-Lib"])
        adapter.write_mod_references(path, context, ["a-Lib"])

        csproj = (path / "MyMod.csproj").read_text(encoding="utf-8")
        assert csproj.count("<Import Project=") == 1


class TestTools:
    def test_adding_records_the_version_at_that_moment(self, project, installed_mod):
        path, context = project
        installed_mod(
            context.mods_dir, "a-Lib", {"Lib.dll": "managed"}, version="1.1.1"
        )

        result = server.add_mod_reference(str(path), "Lib")

        assert result["success"]
        assert ProjectConfig.load(path).references == [
            ModReference(package="a-Lib", version_when_added="1.1.1")
        ]

    def test_adding_twice_does_not_duplicate(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})

        server.add_mod_reference(str(path), "a-Lib")
        server.add_mod_reference(str(path), "a-Lib")

        assert len(ProjectConfig.load(path).references) == 1

    def test_adding_reports_what_was_skipped(self, project, installed_mod):
        path, context = project
        installed_mod(
            context.mods_dir, "a-Lib", {"Lib.dll": "managed", "native.dll": "native"}
        )

        result = server.add_mod_reference(str(path), "a-Lib")

        assert [s["file"] for s in result["skipped"]] == ["native.dll"]

    def test_adding_an_uninstalled_mod_is_a_typed_refusal(self, project):
        path, _ = project
        result = server.add_mod_reference(str(path), "NotInstalled")
        assert result["code"] == "mod_reference_not_found"
        assert result["hints"]

    def test_adding_a_package_with_no_usable_assembly_refuses(
        self, project, installed_mod
    ):
        path, context = project
        installed_mod(context.mods_dir, "a-Native", {"thing.dll": "native"})

        result = server.add_mod_reference(str(path), "a-Native")
        assert result["code"] == "mod_reference_not_found"

    def test_removing_updates_config_and_file(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        server.add_mod_reference(str(path), "a-Lib")

        result = server.remove_mod_reference(str(path), "a-Lib")

        assert result["references"] == []
        assert ProjectConfig.load(path).references == []
        assert not (path / REFERENCES_FILENAME).exists()

    def test_removing_something_not_referenced_refuses(self, project):
        path, _ = project
        result = server.remove_mod_reference(str(path), "a-Lib")
        assert result["code"] == "mod_reference_not_found"

    def test_listing_shows_what_can_be_referenced(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        installed_mod(context.mods_dir, "a-Native", {"x.dll": "native"})

        mods = server.list_available_mods(str(path))["mods"]

        by_package = {m["package"]: m for m in mods}
        assert by_package["a-Lib"]["referenceable"]
        assert not by_package["a-Native"]["referenceable"]


class TestVersionDrift:
    """A dependency updating underneath you should be reported, not inferred
    later from a puzzling compile error."""

    def test_no_warning_while_the_version_is_unchanged(self, project, installed_mod):
        path, context = project
        installed_mod(
            context.mods_dir, "a-Lib", {"Lib.dll": "managed"}, version="1.0.0"
        )
        server.add_mod_reference(str(path), "a-Lib")

        assert _reference_drift(context, ProjectConfig.load(path)) == []

    def test_warns_when_the_installed_version_moved(self, project, installed_mod):
        path, context = project
        installed_mod(
            context.mods_dir, "a-Lib", {"Lib.dll": "managed"}, version="1.0.0"
        )
        server.add_mod_reference(str(path), "a-Lib")
        installed_mod(
            context.mods_dir, "a-Lib", {"Lib.dll": "managed"}, version="2.0.0"
        )

        warnings = _reference_drift(context, ProjectConfig.load(path))

        assert len(warnings) == 1
        assert "1.0.0" in warnings[0] and "2.0.0" in warnings[0]

    def test_warns_when_a_dependency_was_uninstalled(self, project, installed_mod):
        path, context = project
        package = installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        server.add_mod_reference(str(path), "a-Lib")
        shutil.rmtree(package)

        warnings = _reference_drift(context, ProjectConfig.load(path))
        assert "no longer installed" in warnings[0]

    def test_the_current_version_is_never_cached(self, project, installed_mod):
        """Storing it would give two answers to one question, and the stored
        one goes stale the moment the user updates the mod."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        server.add_mod_reference(str(path), "a-Lib")

        stored = json.loads((path / ".modwright.json").read_text(encoding="utf-8"))
        assert set(stored["references"][0]) == {"package", "version_when_added"}
