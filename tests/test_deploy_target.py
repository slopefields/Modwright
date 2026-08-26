"""Deploying somewhere other than the game folder.

Mod managers keep each profile as a standalone loader tree, so for most
players the game install is not where mods actually load from. These cover
the split: assemblies stay with the game, deploy and logs follow the loader.
"""

from __future__ import annotations

import pytest

from modwright import server
from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.errors import InvalidDeployRootError
from modwright.models import BuildOutcome
from modwright.profiles import discover_profiles
from modwright.project_config import ProjectConfig


@pytest.fixture()
def adapter():
    return BepInEx5Adapter()


class TestAdoptLoaderRoot:
    def test_deploy_follows_the_profile(self, adapter, fake_game, fake_profile):
        context = detect_framework(fake_game("Game"))
        profile = fake_profile()

        redirected = adapter.adopt_loader_root(context, profile)

        assert redirected.mods_dir == profile / "BepInEx" / "plugins"
        assert redirected.loader_root == profile

    def test_assemblies_stay_with_the_game(self, adapter, fake_game, fake_profile):
        """The profile has no assemblies -- compiling against it is impossible,
        so managed_dir must keep pointing at the install."""
        game = fake_game("Game")
        context = detect_framework(game)

        redirected = adapter.adopt_loader_root(context, fake_profile())

        assert redirected.managed_dir == game / "Game_Data" / "Managed"
        assert redirected.install_root == game

    def test_log_follows_the_profile(self, adapter, fake_game, fake_profile):
        """The game folder's log may be stale or never written; the profile's
        is the one the running loader appends to."""
        context = detect_framework(fake_game("Game"))
        profile = fake_profile(log=True)

        redirected = adapter.adopt_loader_root(context, profile)

        assert adapter.resolve_log(redirected) == profile / "BepInEx" / "LogOutput.log"

    def test_log_is_none_until_the_profile_has_been_launched(
        self, adapter, fake_game, fake_profile
    ):
        context = detect_framework(fake_game("Game"))
        redirected = adapter.adopt_loader_root(context, fake_profile(log=False))
        assert adapter.resolve_log(redirected) is None

    def test_unchanged_context_still_uses_the_install(self, adapter, fake_game):
        game = fake_game("Game")
        context = detect_framework(game)
        assert context.effective_loader_root == game


class TestRefusals:
    def test_directory_without_a_loader_tree(self, adapter, fake_game, tmp_path):
        """A mistyped path must fail loudly. Creating the tree instead would
        report success while deploying somewhere nothing ever loads from."""
        context = detect_framework(fake_game("Game"))
        empty = tmp_path / "not-a-profile"
        empty.mkdir()

        with pytest.raises(InvalidDeployRootError):
            adapter.adopt_loader_root(context, empty)

        assert not (empty / "BepInEx").exists()

    def test_incomplete_loader_tree(self, adapter, fake_game, fake_profile):
        """The exact state that silently loaded nothing on a real machine:
        a BepInEx folder with plugins but no loader in core/."""
        context = detect_framework(fake_game("Game"))
        broken = fake_profile(core=False)

        with pytest.raises(InvalidDeployRootError) as excinfo:
            adapter.adopt_loader_root(context, broken)
        assert "core" in str(excinfo.value)

    def test_deploy_does_not_create_a_missing_tree(
        self, adapter, fake_game, tmp_path
    ):
        context = detect_framework(fake_game("Game"))
        artifact = tmp_path / "Mod.dll"
        artifact.write_bytes(b"")
        vanished = context.mods_dir.parent
        import shutil as _shutil

        _shutil.rmtree(vanished)

        with pytest.raises(InvalidDeployRootError):
            adapter.deploy(BuildOutcome(artifact=artifact), context)


class TestDiscovery:
    def test_finds_profiles_by_shape(self, fake_profile, monkeypatch, tmp_path):
        fake_profile("dev", game_folder="LethalCompany")
        fake_profile("busy", game_folder="LethalCompany")
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )

        found = {p.name for p in discover_profiles()}
        assert found == {"dev", "busy"}

    def test_skips_directories_that_are_not_loader_trees(
        self, fake_profile, monkeypatch, tmp_path
    ):
        """Managers keep a profile entry before BepInEx is installed into it;
        deploying there would go nowhere."""
        fake_profile("real", game_folder="LethalCompany")
        bare = tmp_path / "r2modmanPlus-local" / "LethalCompany" / "profiles" / "Default"
        bare.mkdir(parents=True)
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )

        assert [p.name for p in discover_profiles()] == ["real"]

    def test_game_filter_ignores_spacing_and_case(
        self, fake_profile, monkeypatch, tmp_path
    ):
        """Managers strip spaces: 'Lethal Company' -> 'LethalCompany'."""
        fake_profile("dev", game_folder="LethalCompany")
        fake_profile("other", game_folder="PEAK")
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )

        found = discover_profiles("Lethal Company")
        assert [p.name for p in found] == ["dev"]

    def test_reports_mod_count_and_whether_it_was_ever_launched(
        self, fake_profile, monkeypatch, tmp_path
    ):
        profile = fake_profile("dev", game_folder="G", log=True)
        (profile / "BepInEx" / "plugins" / "Some.dll").write_bytes(b"")
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )

        found = discover_profiles()[0]
        assert found.mod_count == 1
        assert found.log_path is not None

    def test_no_managers_installed_is_empty_not_an_error(self, monkeypatch):
        monkeypatch.setattr("modwright.profiles.manager_data_dirs", lambda: [])
        assert discover_profiles() == []


class TestProjectConfig:
    def test_deploy_root_round_trips(self, tmp_path):
        config = ProjectConfig("M", "bepinex5", "root", "Game", deploy_root="p")
        config.save(tmp_path)
        assert ProjectConfig.load(tmp_path).deploy_root == "p"

    def test_config_written_before_deploy_root_existed_still_loads(self, tmp_path):
        (tmp_path / ".modwright.json").write_text(
            '{"mod_name": "M", "framework_id": "bepinex5", '
            '"install_root": "r", "game_name": "G"}',
            encoding="utf-8",
        )
        assert ProjectConfig.load(tmp_path).deploy_root is None

    def test_unknown_keys_are_ignored(self, tmp_path):
        """A project written by a newer ModWright should still open here."""
        (tmp_path / ".modwright.json").write_text(
            '{"mod_name": "M", "framework_id": "bepinex5", "install_root": "r", '
            '"game_name": "G", "future_field": 1}',
            encoding="utf-8",
        )
        assert ProjectConfig.load(tmp_path).mod_name == "M"


class TestChoosingBeforeValidating:
    """Whether the destination *works* is a different question from whether it
    is the one the user meant. Ambiguity has to be resolved first."""

    def test_deploy_refuses_to_guess_when_profiles_exist(
        self, fake_game, fake_profile, monkeypatch, tmp_path
    ):
        game = fake_game("Game")
        project = tmp_path / "proj"
        project.mkdir()
        ProjectConfig("M", "bepinex5", str(game), "Game").save(project)
        fake_profile("dev", game_folder="Game")
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )

        from modwright import server

        result = server.deploy_mod(str(project))

        assert result["code"] == "deploy_target_unset"
        assert [p["name"] for p in result["profiles"]] == ["dev"]

    def test_no_profiles_means_no_question(
        self, fake_game, monkeypatch, tmp_path
    ):
        """With no manager installed the game folder is the only option, so
        asking would be noise."""
        game = fake_game("Game")
        project = tmp_path / "proj"
        project.mkdir()
        ProjectConfig("M", "bepinex5", str(game), "Game").save(project)
        monkeypatch.setattr("modwright.profiles.manager_data_dirs", lambda: [])

        from modwright import server

        result = server.deploy_mod(str(project))
        # Fails on the build (no real project), never on an unset target.
        assert result.get("code") != "deploy_target_unset"


class TestTargetBecomingInvalidLater:
    """A target valid when chosen can be deleted or emptied afterwards. It is
    re-checked on every deploy, not trusted because it was stored."""

    def test_deleted_profile_is_reported_as_gone(
        self, adapter, fake_game, fake_profile
    ):
        context = detect_framework(fake_game("Game"))
        profile = fake_profile()
        import shutil as _shutil

        _shutil.rmtree(profile)

        with pytest.raises(InvalidDeployRootError) as excinfo:
            adapter.adopt_loader_root(context, profile)
        assert "no longer exists" in str(excinfo.value)

    def test_fresh_profile_says_how_to_make_it_usable(
        self, adapter, fake_game, tmp_path
    ):
        """A newly created profile has no loader until the first mod is
        installed, so the fix is to install one -- not to pick another."""
        context = detect_framework(fake_game("Game"))
        fresh = tmp_path / "profiles" / "brand-new"
        fresh.mkdir(parents=True)

        with pytest.raises(InvalidDeployRootError) as excinfo:
            adapter.adopt_loader_root(context, fresh)
        assert any("install" in hint.lower() for hint in excinfo.value.hints)


class TestFrameworksWithoutADestination:
    def test_bepinex_supports_choosing_a_target(self, adapter):
        assert adapter.supports_deploy_target

    def test_capability_gates_the_question(self, fake_game, monkeypatch, tmp_path):
        """Frameworks whose build places the artifact itself (tModLoader,
        SMAPI) have no destination to choose, so deploy must not ask."""
        game = fake_game("Game")
        project = tmp_path / "proj"
        project.mkdir()
        ProjectConfig("M", "bepinex5", str(game), "Game").save(project)
        fake_profile_root = tmp_path / "r2modmanPlus-local"
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs", lambda: [fake_profile_root]
        )
        from modwright import server
        from modwright.adapters.bepinex5 import BepInEx5Adapter

        monkeypatch.setattr(BepInEx5Adapter, "supports_deploy_target", False)
        result = server.deploy_mod(str(project))
        assert result.get("code") != "deploy_target_unset"


class TestDiscoveryDefersToAdapters:
    """What a loader tree looks like is each adapter's knowledge, so discovery
    stays usable when a second framework arrives."""

    def test_profile_is_tagged_with_the_framework_that_claimed_it(
        self, fake_profile, monkeypatch, tmp_path
    ):
        fake_profile("dev", game_folder="G")
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )
        assert discover_profiles()[0].framework_id == "bepinex5"

    def test_tree_missing_its_loader_is_not_offered(
        self, fake_profile, monkeypatch, tmp_path
    ):
        """Recognition means deployable. A BepInEx folder with no core/ takes
        the file and loads nothing, so offering it would be a trap."""
        fake_profile("broken", game_folder="G", core=False)
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )
        assert discover_profiles() == []


class TestProfilesWithNoLoaderYet:
    """Accounting for the profiles `discover_profiles` deliberately drops.

    A manager creates the profile entry immediately but does not install the
    loader until something is installed into it, so a profile the user just
    made and named holds nothing. Refusing it as a deploy target is right --
    a file put there would load nothing. Omitting it from the listing without
    a word is not: the one name the user is looking for is the one missing,
    which reads as the tool being out of date rather than the profile being
    empty, and that is what it cost in the rehearsal.
    """

    @pytest.fixture()
    def listing(self, fake_profile, monkeypatch, tmp_path):
        def _build(*bare_names, **kwargs):
            fake_profile("ready", game_folder="LethalCompany", **kwargs)
            root = tmp_path / "r2modmanPlus-local"
            for name in bare_names:
                (root / "LethalCompany" / "profiles" / name).mkdir(parents=True)
            monkeypatch.setattr(
                "modwright.profiles.manager_data_dirs", lambda: [root]
            )

        return _build

    def test_an_empty_profile_is_reported_rather_than_dropped(self, listing):
        listing("brand new")
        result = server.list_mod_profiles("Lethal Company")

        assert [p["name"] for p in result["profiles"]] == ["ready"]
        assert [p["name"] for p in result["unavailable"]] == ["brand new"]

    def test_the_reason_names_the_package_to_install(self, listing):
        """The listing itself must not know the word: it comes from whichever
        adapter recognised the shape."""
        listing("brand new")
        entry = server.list_mod_profiles("Lethal Company")["unavailable"][0]

        assert "BepInExPack" in entry["reason"]

    def test_a_shell_with_no_loader_in_it_is_reported_too(
        self, listing, fake_profile
    ):
        """Not the same as empty: this one has the folder but not the loader,
        and would accept a deployed file and load nothing."""
        fake_profile("hollow", game_folder="LethalCompany", core=False)
        listing()
        entry = next(
            p
            for p in server.list_mod_profiles("Lethal Company")["unavailable"]
            if p["name"] == "hollow"
        )

        assert "core" in entry["reason"]

    def test_the_two_listings_together_cover_every_directory(self, listing):
        """Neither list may quietly swallow a profile."""
        listing("one", "two")
        result = server.list_mod_profiles("Lethal Company")

        names = {p["name"] for p in result["profiles"]}
        names |= {p["name"] for p in result["unavailable"]}
        assert names == {"ready", "one", "two"}

    def test_usable_profiles_are_never_listed_as_unavailable(self, listing):
        listing()
        result = server.list_mod_profiles("Lethal Company")

        assert result["unavailable"] == []

    def test_only_empty_profiles_is_not_reported_as_no_profiles(
        self, monkeypatch, tmp_path
    ):
        """The old empty-list hint said no manager was installed, which sends
        a user whose only profile is a new one after the wrong problem."""
        root = tmp_path / "r2modmanPlus-local"
        (root / "LethalCompany" / "profiles" / "brand new").mkdir(parents=True)
        monkeypatch.setattr("modwright.profiles.manager_data_dirs", lambda: [root])

        result = server.list_mod_profiles("Lethal Company")

        assert result["profiles"] == []
        assert [p["name"] for p in result["unavailable"]] == ["brand new"]
        assert not any("No mod-manager profiles" in h for h in result["hints"])

    def test_no_manager_at_all_still_says_so(self, monkeypatch):
        monkeypatch.setattr("modwright.profiles.manager_data_dirs", lambda: [])
        result = server.list_mod_profiles("Lethal Company")

        assert result["unavailable"] == []
        assert any("No mod-manager profiles" in h for h in result["hints"])
