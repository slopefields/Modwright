"""Framework detection and the adapter registry.

Uses synthetic install trees so every branch -- IL2CPP, no loader, non-Unity --
is covered without those games being installed.
"""

from __future__ import annotations

import errno

import pytest

from modwright.adapters import detect_framework
from modwright.adapters import bepinex5
from modwright.adapters.base import ModFrameworkAdapter
from modwright.adapters.registry import ADAPTERS, _protocol_members, _verify_adapters
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.errors import (
    ArtifactLockedError,
    DeployFailedError,
    Il2CppUnsupportedError,
    InvalidInstallRootError,
    UnsupportedGameError,
)
from modwright.models import BuildOutcome


class TestBepInEx5Detection:
    def test_claims_a_mono_unity_install_with_bepinex(self, fake_game):
        root = fake_game("Lethal Company")
        context = detect_framework(root)

        assert context.framework_id == "bepinex5"
        assert context.game_name == "Lethal Company"
        assert context.managed_dir == root / "Lethal Company_Data" / "Managed"
        assert context.mods_dir == root / "BepInEx" / "plugins"

    def test_refuses_il2cpp_explicitly(self, fake_game):
        """IL2CPP compiles to native code, so no IL decompiler can read it.

        This must be a clear refusal rather than a generic "unsupported", since
        the install otherwise looks exactly like a supported one.
        """
        root = fake_game("Phasmophobia", il2cpp=True)
        with pytest.raises(Il2CppUnsupportedError) as excinfo:
            detect_framework(root)
        assert "IL2CPP" in str(excinfo.value)

    def test_declines_unity_game_without_bepinex(self, fake_game):
        """Not ours -- another adapter may claim it, so this is not an error
        from the adapter's point of view."""
        root = fake_game("SomeGame", bepinex=False)
        assert BepInEx5Adapter().detect(root) is None

    def test_declines_non_unity_directory(self, fake_game):
        root = fake_game("NotUnity", unity=False)
        assert BepInEx5Adapter().detect(root) is None

    def test_declines_when_assembly_csharp_missing(self, tmp_path):
        root = tmp_path / "Hollow"
        (root / "Hollow_Data" / "Managed").mkdir(parents=True)
        (root / "BepInEx").mkdir()
        assert BepInEx5Adapter().detect(root) is None


class TestRegistry:
    def test_unrecognised_install_is_refused_with_hints(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(UnsupportedGameError) as excinfo:
            detect_framework(empty)
        assert excinfo.value.hints

    def test_missing_directory_is_distinguished_from_unsupported(self, tmp_path):
        with pytest.raises(InvalidInstallRootError):
            detect_framework(tmp_path / "does-not-exist")


class TestTargetFramework:
    """Which profile a mod should target depends on the BCL the game ships."""

    def test_net_framework_profile_targets_net472(self, fake_game):
        root = fake_game("G", net_framework_profile=True)
        context = BepInEx5Adapter().detect(root)
        assert BepInEx5Adapter()._target_framework(context) == "net472"

    def test_netstandard_only_profile_targets_netstandard(self, fake_game):
        """Without the full BCL, net472 would promise assemblies that are not
        there, so netstandard2.1 is correct."""
        root = fake_game("G", net_framework_profile=False)
        context = BepInEx5Adapter().detect(root)
        assert BepInEx5Adapter()._target_framework(context) == "netstandard2.1"


class TestLoaderOutsideTheGameFolder:
    """A mod manager keeps the loader in a profile and leaves the game install
    untouched, so requiring BepInEx beside the game refused those users before
    they could say where their loader actually is."""

    def _profile(self, tmp_path, game_folder="CoolGame", name="Default"):
        root = tmp_path / "r2modmanPlus-local" / game_folder / "profiles" / name
        for sub in ("core", "plugins"):
            (root / "BepInEx" / sub).mkdir(parents=True, exist_ok=True)
        return root

    def _use(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"],
        )

    def test_a_profile_elsewhere_is_enough_to_claim_the_game(
        self, fake_game, tmp_path, monkeypatch
    ):
        game = fake_game("CoolGame", bepinex=False)
        self._profile(tmp_path)
        self._use(monkeypatch, tmp_path)

        context = detect_framework(game)
        assert context.framework_id == "bepinex5"
        # Proof of framework, not a choice of destination: several profiles
        # may exist and picking one is the user's call.
        assert context.mods_dir is None

    def test_doorstop_files_alone_claim_the_game(self, fake_game, monkeypatch):
        """What a manager leaves in the game folder: the injector and its
        config, with everything else in a profile we may not find."""
        game = fake_game("CoolGame", bepinex=False)
        (game / "winhttp.dll").write_bytes(b"")
        (game / "doorstop_config.ini").write_text("enabled=true", encoding="utf-8")
        monkeypatch.setattr("modwright.profiles.manager_data_dirs", lambda: [])

        context = BepInEx5Adapter().detect(game)
        assert context is not None
        assert context.mods_dir is None

    def test_one_doorstop_file_is_not_evidence(self, fake_game, monkeypatch):
        """winhttp.dll is a common enough filename to mean nothing alone."""
        game = fake_game("CoolGame", bepinex=False)
        (game / "winhttp.dll").write_bytes(b"")
        monkeypatch.setattr("modwright.profiles.manager_data_dirs", lambda: [])

        assert BepInEx5Adapter().detect(game) is None

    def test_a_mono_unity_game_with_no_loader_anywhere_is_still_declined(
        self, fake_game, monkeypatch
    ):
        """Being Unity+Mono is not enough: RimWorld is both and loads mods
        natively, with no BepInEx involved."""
        game = fake_game("CoolGame", bepinex=False)
        monkeypatch.setattr("modwright.profiles.manager_data_dirs", lambda: [])

        assert BepInEx5Adapter().detect(game) is None
        with pytest.raises(UnsupportedGameError):
            detect_framework(game)

    def test_a_loader_in_the_game_folder_still_sets_the_target(self, fake_game):
        """The hand-installed case keeps working, and needs no question asked:
        there is only one place the mod can go."""
        game = fake_game("CoolGame")
        context = BepInEx5Adapter().detect(game)
        assert context.mods_dir == game / "BepInEx" / "plugins"


class TestTheContractIsEnforced:
    """`ModFrameworkAdapter` is a Protocol, which Python checks only under a
    type checker -- and this project runs none. Adapters do not even inherit
    from it. So adding a member to the contract would silently invalidate
    every adapter written before it, and the first sign would be an
    AttributeError inside whichever tool called the missing member.
    """

    def test_every_registered_adapter_satisfies_it(self):
        for adapter in ADAPTERS:
            assert isinstance(adapter, ModFrameworkAdapter), (
                f"{type(adapter).__name__} does not implement the contract"
            )

    def test_an_incomplete_adapter_is_refused_by_name(self):
        """What a future adapter looks like when the contract has moved on
        without it: everything common is present, one newer member is not."""

        class StaleAdapter(BepInEx5Adapter):
            framework_id = "stale"

            def __getattribute__(self, name):
                if name == "inspect_logging":
                    raise AttributeError(name)
                return object.__getattribute__(self, name)

        with pytest.raises(TypeError) as excinfo:
            _verify_adapters((StaleAdapter(),))

        message = str(excinfo.value)
        assert "StaleAdapter" in message
        assert "inspect_logging" in message

    def test_the_member_list_is_read_from_the_protocol(self):
        """Hardcoding it here would be one more list to remember to update --
        the very problem this check exists to remove."""
        members = _protocol_members(ModFrameworkAdapter)

        assert "inspect_logging" in members  # a method
        assert "framework_id" in members  # an annotated attribute
        assert not any(name.startswith("_") for name in members)


class TestLoaderStartCounting:
    """Counting BepInEx's startup banner, and why it only works on a slice.

    This is the adapter half of "is the running game running the build I just
    deployed". The framework detail that makes it work is also the one that
    makes the obvious use of it wrong.
    """

    def test_counts_the_startup_banner(self):
        adapter = BepInEx5Adapter()
        text = (
            "[Message:   BepInEx] Preloader started\n"
            "[Message:   BepInEx] Chainloader started\n"
            "[Info   :   BepInEx] Loading [MyMod 1.0.0]\n"
        )

        assert adapter.count_loader_starts(text) == 1

    def test_ordinary_output_counts_nothing(self):
        assert BepInEx5Adapter().count_loader_starts("[Info: MyMod] hello\n") == 0

    def test_empty_text_counts_nothing(self):
        assert BepInEx5Adapter().count_loader_starts("") == 0

    def test_the_version_banner_is_not_used_as_the_marker(self):
        """BepInEx's first line looks like a launch timestamp and is not one --
        it carries the game executable's date, so it reads identically in every
        profile on a machine, months apart. Counting it would report a restart
        on every single read."""
        first_line = (
            "[Message:   BepInEx] BepInEx 5.4.23.5 - Lethal Company "
            "(8/22/2026 4:40:30 PM)\n"
        )

        assert BepInEx5Adapter().count_loader_starts(first_line) == 0


class TestDeployFailuresAreNamedForWhatHappened:
    """Every OSError from the copy used to be reported as `artifact_locked`,
    whose hint says the game is probably running. That is right for the
    common case and actively misleading for the rest: telling someone to
    close a game they have already closed sends the whole investigation the
    wrong way, and a full disk does not fix itself while they try."""

    class _SharingViolation(OSError):
        """What Windows raises when another process holds the file open."""

        winerror = 32

    @pytest.fixture()
    def ready_to_deploy(self, fake_game, tmp_path):
        context = detect_framework(fake_game("Game"))
        artifact = tmp_path / "MyMod.dll"
        artifact.write_bytes(b"")
        return BepInEx5Adapter(), BuildOutcome(artifact=artifact), context

    def test_a_locked_file_still_names_the_running_game(
        self, ready_to_deploy, monkeypatch
    ):
        adapter, outcome, context = ready_to_deploy
        monkeypatch.setattr(
            bepinex5.shutil,
            "copy2",
            lambda *a, **k: (_ for _ in ()).throw(self._SharingViolation("locked")),
        )

        with pytest.raises(ArtifactLockedError) as caught:
            adapter.deploy(outcome, context)

        assert any("running" in hint for hint in caught.value.hints)

    def test_a_full_disk_is_not_blamed_on_the_game(
        self, ready_to_deploy, monkeypatch
    ):
        adapter, outcome, context = ready_to_deploy
        monkeypatch.setattr(
            bepinex5.shutil,
            "copy2",
            lambda *a, **k: (_ for _ in ()).throw(
                OSError(errno.ENOSPC, "No space left on device")
            ),
        )

        with pytest.raises(DeployFailedError) as caught:
            adapter.deploy(outcome, context)

        assert not any("running" in hint for hint in caught.value.hints)
