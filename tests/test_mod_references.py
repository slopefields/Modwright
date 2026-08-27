"""Compiling a mod against another installed mod.

The dependency is referenced where the mod manager already installed it, so
the build matches the version the game will load. The awkward part is that a
package is a folder whose contents vary: the assembly may be nested, named
nothing like the package, and sitting next to native libraries and framework
assemblies that must not be referenced.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest

from modwright import server
from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import PROPS_FILENAME, BepInEx5Adapter
from modwright.errors import BuildFailedError, DecompilerUnavailableError
from modwright.models import BuildOutcome
from modwright.mods import find_installed_mod, list_installed_mods, read_installed_mod
from modwright.project_config import ModReference, ProjectConfig
from modwright.server import _reference_drift

from conftest import _pe_bytes
from modwright.mods import _HEADER_BYTES, is_managed_assembly


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

    def test_listing_includes_loose_assemblies(self, mods_dir):
        """A dependency need not be a folder. A library taken from a release
        page, or a locally built mod, is a bare file -- and keeping only
        directories made those invisible to add_mod_reference entirely."""
        (mods_dir / "SomeMod.dll").write_bytes(_pe_bytes(managed=True))

        assert [m.package for m in list_installed_mods(mods_dir)] == [
            "SomeMod",
            "xilophor-StaticNetcodeLib",
        ]

    def test_a_loose_assembly_is_referenceable_and_has_no_version(self, mods_dir):
        """No manifest sits beside a bare file, so there is no version to
        read. None is recorded rather than one invented from the assembly's
        own metadata, which routinely disagrees with the released version."""
        (mods_dir / "SomeMod.dll").write_bytes(_pe_bytes(managed=True))

        loose = next(m for m in list_installed_mods(mods_dir) if m.package == "SomeMod")
        assert loose.referenceable
        assert loose.version is None
        assert [a.name for a in loose.assemblies] == ["SomeMod.dll"]

    def test_a_loose_native_library_is_not_referenceable(self, mods_dir):
        """The same PE check that protects packages protects loose files:
        referencing a native DLL fails the build outright."""
        (mods_dir / "opus.dll").write_bytes(_pe_bytes(managed=False))

        loose = next(m for m in list_installed_mods(mods_dir) if m.package == "opus")
        assert not loose.referenceable
        assert "native" in loose.skipped[0].reason

    def test_loose_files_that_are_not_assemblies_are_ignored(self, mods_dir):
        (mods_dir / "readme.txt").write_text("not a mod", encoding="utf-8")
        assert "readme" not in [m.package for m in list_installed_mods(mods_dir)]


class TestGeneratedReferenceFile:
    def test_written_beside_the_project_and_imported(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib/Lib.dll": "managed"})

        BepInEx5Adapter().write_project_props(path, context, ["a-Lib"])

        assert (path / PROPS_FILENAME).exists()
        csproj = (path / "MyMod.csproj").read_text(encoding="utf-8")
        assert PROPS_FILENAME in csproj

    def test_references_are_relative_to_an_overridable_property(
        self, project, installed_mod
    ):
        """Same treatment as GameDir: the machine-specific path appears once."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib/Lib.dll": "managed"})

        BepInEx5Adapter().write_project_props(path, context, ["a-Lib"])
        props = (path / PROPS_FILENAME).read_text(encoding="utf-8")

        assert "<ModsDir Condition=" in props
        assert "$(ModsDir)" in props
        assert "a-Lib" in props

    def test_dependencies_are_not_copied_to_output(self, project, installed_mod):
        """BepInEx loads each mod itself; a copy beside ours would be a second
        instance of the same assembly."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})

        BepInEx5Adapter().write_project_props(path, context, ["a-Lib"])
        props = (path / PROPS_FILENAME).read_text(encoding="utf-8")

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

        adapter.write_project_props(path, context, ["a-One"])
        adapter.write_project_props(path, context, ["a-Two"])
        props = (path / PROPS_FILENAME).read_text(encoding="utf-8")

        assert "Two.dll" in props
        assert "One.dll" not in props

    def test_removing_the_last_reference_keeps_the_file(self, project, installed_mod):
        """It also carries the build paths, so deleting it when the last
        reference goes would break the build rather than merely drop a
        dependency."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        adapter = BepInEx5Adapter()

        adapter.write_project_props(path, context, ["a-Lib"])
        adapter.write_project_props(path, context, [])

        props = (path / PROPS_FILENAME).read_text(encoding="utf-8")
        assert "a-Lib" not in props
        assert "<GameDir" in props

    def test_import_is_not_duplicated(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        adapter = BepInEx5Adapter()

        adapter.write_project_props(path, context, ["a-Lib"])
        adapter.write_project_props(path, context, ["a-Lib"])

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
        stored = ProjectConfig.load(path).references
        assert [(r.package, r.version_when_added) for r in stored] == [
            ("a-Lib", "1.1.1")
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
        # The file stays: it holds the build paths as well as the references.
        assert "a-Lib" not in (path / PROPS_FILENAME).read_text(encoding="utf-8")

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

    def test_the_listing_gives_openable_paths_not_bare_filenames(
        self, project, installed_mod
    ):
        """Explaining why an installed mod fights the one being written means
        reading its code, and a decompiler needs a path. This function already
        holds them, so reporting the filename alone only sends the caller off
        to search the disk for a file it could have handed over."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})

        listed = server.list_available_mods(str(path))["mods"][0]

        assert listed["assemblies"] == [str(context.mods_dir / "a-Lib" / "Lib.dll")]

    def test_the_two_tools_report_assemblies_the_same_way(
        self, project, installed_mod
    ):
        """`add_mod_reference` always reported paths. The listing disagreeing
        with it is the kind of split that teaches a caller to distrust both."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})

        listed = server.list_available_mods(str(path))["mods"][0]["assemblies"]
        referenced = server.add_mod_reference(str(path), "a-Lib")["referenced"]

        assert listed == referenced


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
        # Everything stored is an "at the time it was added" fact. Nothing
        # here describes what is installed *now* -- that is read from disk.
        assert set(stored["references"][0]) == {
            "package",
            "version_when_added",
            "assembly_mtime_when_added",
        }


class TestOwnOutputIsNotADependency:
    """A project deploys into the same folder it reads dependencies from, so
    once bare files are visible its own artifact is sitting among them."""

    def _deploy_own_output(self, context, mod_name="MyMod"):
        artifact = context.mods_dir / f"{mod_name}.dll"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(_pe_bytes(managed=True))
        return artifact

    def test_not_offered_in_the_listing(self, project, installed_mod):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        self._deploy_own_output(context)

        listed = server.list_available_mods(str(path))["mods"]
        assert [m["package"] for m in listed] == ["a-Lib"]

    def test_referencing_itself_is_refused_with_a_reason(self, project):
        path, context = project
        self._deploy_own_output(context)

        result = server.add_mod_reference(str(path), "MyMod")

        assert result["success"] is False
        assert "own build output" in result["error"]

    def test_a_real_dependency_is_still_reachable(self, project, installed_mod):
        """The exclusion must not swallow anything but the project itself."""
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        self._deploy_own_output(context)

        assert server.add_mod_reference(str(path), "a-Lib")["success"]


class TestChangedDependencyHint:
    """A bare file has no version, so 'has it moved' is the only question that
    can be answered about it. Reported only when a build fails."""

    def _touch(self, path, *, seconds: int = 60):
        when = os.stat(path).st_mtime + seconds
        os.utime(path, (when, when))

    def test_a_changed_dependency_is_reported_when_the_build_fails(
        self, project, installed_mod, monkeypatch
    ):
        path, context = project
        package = installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        server.add_mod_reference(str(path), "a-Lib")
        self._touch(package / "Lib.dll")

        monkeypatch.setattr(
            BepInEx5Adapter, "build",
            lambda self, p: (_ for _ in ()).throw(BuildFailedError("nope")),
        )
        result = asyncio.run(server.build_mod(str(path)))

        assert result["success"] is False
        assert any(
            "changed on disk" in w for w in result["reference_warnings"]
        )

    def test_an_unchanged_dependency_says_nothing(
        self, project, installed_mod, monkeypatch
    ):
        path, context = project
        installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        server.add_mod_reference(str(path), "a-Lib")

        monkeypatch.setattr(
            BepInEx5Adapter, "build",
            lambda self, p: (_ for _ in ()).throw(BuildFailedError("nope")),
        )
        result = asyncio.run(server.build_mod(str(path)))

        assert result["success"] is False
        assert "reference_warnings" not in result

    def test_a_passing_build_never_carries_the_hint(
        self, project, installed_mod, monkeypatch
    ):
        """The timestamp moves every time a dependency is rebuilt and
        redeployed, so on a passing build this would be constant noise."""
        path, context = project
        package = installed_mod(context.mods_dir, "a-Lib", {"Lib.dll": "managed"})
        server.add_mod_reference(str(path), "a-Lib")
        self._touch(package / "Lib.dll")

        monkeypatch.setattr(
            BepInEx5Adapter, "build",
            lambda self, p: BuildOutcome(artifact=p / "out.dll"),
        )
        result = asyncio.run(server.build_mod(str(path)))

        assert result["success"] is True
        assert "reference_warnings" not in result


def _stub_build(monkeypatch):
    """Skip the real compile: these tests are about what happens around it."""
    artifact = None

    def _build(self, project):
        nonlocal artifact
        artifact = project / "bin" / f"{project.name}.dll"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"dll")
        return BuildOutcome(artifact=artifact)

    monkeypatch.setattr("modwright.adapters.bepinex5.BepInEx5Adapter.build", _build)

class TestPatchNamesCheckedOnTheWayThrough:
    """Harmony resolves its target by string at launch, so the compiler never
    sees a typo and the build passes. The check for that existed as its own
    tool and was simply never called: the rehearsal agent deployed four times
    and called `build_mod` zero times, so a check reachable only through a
    tool nothing reaches for was no check at all. Both paths to a DLL now run
    it.
    """

    @pytest.fixture()
    def report(self, monkeypatch):
        """Stand in for DecompilerServer, which these tests must not spawn."""

        def _use(missing=(), found=(), unchecked=(), unavailable=None):
            async def _fake(project, context, adapter):
                if unavailable:
                    raise DecompilerUnavailableError(unavailable)
                return {
                    "valid": not missing,
                    "checked": len(missing) + len(found),
                    "missing": [
                        {"target": t, "file": "Plugin.cs", "line": 1,
                         "did_you_mean": []} for t in missing
                    ],
                    "found": [{"target": t, "line": 1} for t in found],
                    "unchecked": [
                        {"target": t, "file": "Plugin.cs", "line": 1,
                         "reason": "not a literal"} for t in unchecked
                    ],
                }

            monkeypatch.setattr(server, "_validate_patches", _fake)

        return _use

    def test_a_clean_build_says_the_check_ran(self, project, report, monkeypatch):
        report(found=("A.Start", "A.Update"))
        _stub_build(monkeypatch)
        path, _ = project

        result = asyncio.run(server.build_mod(str(path)))

        assert result["patches_checked"] == 2
        assert "patch_warnings" not in result

    def test_a_bad_patch_name_is_reported_without_failing_the_build(
        self, project, report, monkeypatch
    ):
        """It still compiles and is still worth deploying -- the mod just will
        not load until the name is fixed, which only launching would reveal."""
        report(missing=("A.LateUpdte",))
        _stub_build(monkeypatch)
        path, _ = project

        result = asyncio.run(server.build_mod(str(path)))

        assert result["success"] is True
        assert result["patches_missing"][0]["target"] == "A.LateUpdte"
        assert result["patch_warnings"]

    def test_deploy_runs_the_same_check(self, project, report, monkeypatch):
        """The path the agent actually takes. Putting this in `build_mod`
        alone would have missed every deploy in the rehearsal."""
        report(missing=("A.LateUpdte",))
        _stub_build(monkeypatch)
        path, _ = project

        result = asyncio.run(server.deploy_mod(str(path)))

        assert result["success"] is True
        assert result["patches_missing"][0]["target"] == "A.LateUpdte"

    def test_an_unreachable_decompiler_says_so_rather_than_passing_quietly(
        self, project, report, monkeypatch
    ):
        """A check that silently does not run reads exactly like one that
        passed. That confusion is what made an empty log unreadable, and it
        must not be reintroduced here."""
        report(unavailable="DecompilerServer executable not configured.")
        _stub_build(monkeypatch)
        path, _ = project

        result = asyncio.run(server.build_mod(str(path)))

        assert result["success"] is True
        assert "not configured" in result["patches_unchecked"]
        assert "patches_checked" not in result

    def test_targets_that_cannot_be_read_are_listed_separately(
        self, project, report, monkeypatch
    ):
        """A target built from a variable is unverifiable without compiling.
        Neither passed nor failed -- reported as what it is."""
        report(found=("A.Start",), unchecked=("A.<computed>",))
        _stub_build(monkeypatch)
        path, _ = project

        result = asyncio.run(server.build_mod(str(path)))

        assert result["patches_not_literal"][0]["target"] == "A.<computed>"

    def test_a_failed_build_does_not_pay_for_the_check(
        self, project, monkeypatch
    ):
        """The compiler error is the more urgent answer and arrives in a
        fraction of the time."""
        def _never(*args, **kwargs):
            raise AssertionError("validated a build that failed")

        monkeypatch.setattr(server, "_validate_patches", _never)
        monkeypatch.setattr(
            "modwright.adapters.bepinex5.BepInEx5Adapter.build",
            lambda self, p: (_ for _ in ()).throw(BuildFailedError("nope")),
        )
        path, _ = project

        result = asyncio.run(server.build_mod(str(path)))

        assert result["success"] is False
        assert result["code"] == "build_failed"


class TestDeployHandsBackALogCursor:
    """Deploy is the only moment the tool knows for certain, so it is the
    only place a useful baseline can be taken.

    Everything downstream -- did the game restart, is the running process
    holding this build -- is answered relative to a cursor. Left to take its
    own baseline on the first poll, the agent takes it after the relaunch it
    is trying to detect, and there is nothing left to compare.
    """

    @pytest.fixture()
    def deployable(self, project, monkeypatch):
        async def _no_patches(project_path, context, adapter):
            return {"valid": True, "checked": 0, "missing": [],
                    "found": [], "unchecked": []}

        monkeypatch.setattr(server, "_validate_patches", _no_patches)
        _stub_build(monkeypatch)
        path, context = project
        return path, context.install_root / "BepInEx" / "LogOutput.log"

    def test_the_cursor_is_the_logs_length_at_deploy_time(self, deployable):
        path, log = deployable
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("previous session\n", encoding="utf-8")

        result = asyncio.run(server.deploy_mod(str(path)))

        assert result["log_cursor"] == log.stat().st_size

    def test_a_profile_never_launched_reports_no_cursor(self, deployable):
        """None rather than 0: there is no log yet, and reading from byte 0 of
        the one first launch writes covers the startup anyway."""
        path, log = deployable
        assert not log.exists()

        assert asyncio.run(server.deploy_mod(str(path)))["log_cursor"] is None

    def test_polling_from_that_cursor_sees_the_next_startup(self, deployable):
        """The whole point, end to end: the bytes after the deploy cursor are
        exactly the ones written since the file was placed."""
        path, log = deployable
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("previous session\n", encoding="utf-8")
        cursor = asyncio.run(server.deploy_mod(str(path)))["log_cursor"]

        with log.open("a", encoding="utf-8") as handle:
            handle.write("[Message:   BepInEx] Chainloader started\n")

        polled = server.watch_mod_logs(str(path), since_cursor=cursor)
        assert polled["loader_restarted_since_cursor"] is True


class TestAssemblyClassificationReadsOnlyTheHeader:
    """Every entry in a mods folder passes through `is_managed_assembly`, and
    reading each one whole cost 12.6 seconds on a real 91-mod profile -- 60 MB
    of assembly bodies to look at a few hundred bytes of header. That is on
    the path of `list_available_mods` and of every reference change, and
    `write_project_props` walks it once per referenced package."""

    def test_a_body_larger_than_the_header_window_is_still_classified(
        self, tmp_path
    ):
        dll = tmp_path / "Big.dll"
        dll.write_bytes(_pe_bytes(managed=True) + b"\0" * (_HEADER_BYTES * 4))
        assert is_managed_assembly(dll) is True

    def test_a_native_library_of_the_same_size_is_still_excluded(self, tmp_path):
        dll = tmp_path / "opus.dll"
        dll.write_bytes(_pe_bytes(managed=False) + b"\0" * (_HEADER_BYTES * 4))
        assert is_managed_assembly(dll) is False

    def test_a_file_too_short_to_hold_a_header_is_refused_not_raised(
        self, tmp_path
    ):
        """A partly-downloaded or zero-byte file in a mods folder must be
        classified, not turned into a traceback out of a listing tool."""
        dll = tmp_path / "Truncated.dll"
        dll.write_bytes(_pe_bytes(managed=True)[:64])
        assert is_managed_assembly(dll) is False

    def test_an_empty_file_is_refused(self, tmp_path):
        dll = tmp_path / "Empty.dll"
        dll.write_bytes(b"")
        assert is_managed_assembly(dll) is False
