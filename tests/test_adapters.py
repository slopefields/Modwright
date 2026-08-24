"""Framework detection and the adapter registry.

Uses synthetic install trees so every branch -- IL2CPP, no loader, non-Unity --
is covered without those games being installed.
"""

from __future__ import annotations

import pytest

from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.errors import (
    Il2CppUnsupportedError,
    InvalidInstallRootError,
    UnsupportedGameError,
)


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
