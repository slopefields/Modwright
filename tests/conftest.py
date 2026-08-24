"""Shared fixtures.

Most tests here are pure: they build fake game trees and fake mod sources on
disk, so they run anywhere. The handful that need a real game install, a
DecompilerServer build, or the .NET SDK are marked and skip themselves when
those are absent, so a fresh checkout still gets a meaningful test run.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

#: Games the integration tests can run against, tried in order. Extend this
#: rather than hardcoding a path into a test.
_CANDIDATE_GAMES = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Lethal Company"),
)


def _installed_game() -> Path | None:
    for candidate in _CANDIDATE_GAMES:
        if (candidate / "BepInEx").is_dir():
            return candidate
    return None


@pytest.fixture(scope="session")
def game_install() -> Path:
    """A real supported game install, or skip."""
    game = _installed_game()
    if game is None:
        pytest.skip("no supported game install found")
    return game


@pytest.fixture(scope="session", autouse=True)
def _decompiler_path() -> None:
    """Point at a sibling DecompilerServer build if the env var is unset.

    Keeps `pytest` working out of the box in the layout this repo is developed
    in, without hardcoding that layout into the code under test.
    """
    if os.environ.get("MODWRIGHT_DECOMPILER_PATH"):
        return
    sibling = (
        Path(__file__).resolve().parents[2]
        / "DecompilerServer"
        / "bin"
        / "Release"
        / "net10.0"
        / "DecompilerServer.exe"
    )
    if sibling.exists():
        os.environ["MODWRIGHT_DECOMPILER_PATH"] = str(sibling)


@pytest.fixture()
def requires_decompiler() -> None:
    path = os.environ.get("MODWRIGHT_DECOMPILER_PATH")
    if not path or not Path(path).exists():
        pytest.skip("DecompilerServer build not available")


@pytest.fixture()
def requires_dotnet() -> None:
    if shutil.which("dotnet") is None:
        pytest.skip("dotnet SDK not on PATH")


@pytest.fixture()
def fake_game(tmp_path: Path):
    """Build a synthetic game install tree.

    Lets adapter detection be tested exhaustively -- IL2CPP, missing loader,
    non-Unity -- without needing those games actually installed.
    """

    def _build(
        name: str = "FakeGame",
        *,
        unity: bool = True,
        il2cpp: bool = False,
        bepinex: bool = True,
        net_framework_profile: bool = True,
    ) -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)

        if unity:
            managed = root / f"{name}_Data" / "Managed"
            managed.mkdir(parents=True, exist_ok=True)
            (managed / "Assembly-CSharp.dll").write_bytes(b"")
            (managed / "netstandard.dll").write_bytes(b"")
            if net_framework_profile:
                # The assemblies that mark Unity's .NET Framework API level.
                (managed / "System.Data.dll").write_bytes(b"")
                (managed / "System.Xml.Linq.dll").write_bytes(b"")

        if il2cpp:
            (root / "GameAssembly.dll").write_bytes(b"")

        if bepinex:
            (root / "BepInEx" / "core").mkdir(parents=True, exist_ok=True)
            (root / "BepInEx" / "plugins").mkdir(parents=True, exist_ok=True)

        return root

    return _build


@pytest.fixture()
def fake_profile(tmp_path: Path):
    """Build a synthetic mod-manager profile tree.

    Mirrors the layout r2modman/Thunderstore Mod Manager/Gale produce: a
    standalone loader tree with no game assemblies anywhere near it.
    """

    def _build(
        name: str = "dev",
        *,
        manager: str = "r2modmanPlus-local",
        game_folder: str = "FakeGame",
        core: bool = True,
        plugins: bool = True,
        log: bool = False,
    ) -> Path:
        root = tmp_path / manager / game_folder / "profiles" / name
        loader = root / "BepInEx"
        loader.mkdir(parents=True, exist_ok=True)
        if core:
            (loader / "core").mkdir(exist_ok=True)
        if plugins:
            (loader / "plugins").mkdir(exist_ok=True)
        if log:
            (loader / "LogOutput.log").write_text("start\n", encoding="utf-8")
        (root / "doorstop_config.ini").write_text("enabled = true\n", encoding="utf-8")
        return root

    return _build


@pytest.fixture()
def mod_source(tmp_path: Path):
    """Write C# into a throwaway mod directory and return the directory."""

    def _write(code: str, filename: str = "Patches.cs") -> Path:
        directory = tmp_path / "mod"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        return directory

    return _write
