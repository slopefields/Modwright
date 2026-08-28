"""Knowing which ModWright is answering before trusting what it says.

ModWright is normally installed editable, so the server imports out of a
working tree and a process started before an edit goes on serving the code it
loaded. Nothing in any other response says so, which makes a stale server look
exactly like a bug in whatever is being debugged -- and establishing otherwise
used to mean reading `.pth` files and comparing timestamps by hand.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

from modwright import server


@pytest.fixture()
def info():
    return server.server_info()


class TestWhatIsRunning:
    def test_it_names_the_source_it_imported(self, info):
        """The path, not just a version. An editable install can point
        anywhere, and which tree it points at is the actual question."""
        assert Path(info["source_root"]) == Path(server.__file__).resolve().parent

    def test_it_reports_when_this_process_started(self, info):
        assert info["started_at"]

    def test_a_missing_distribution_is_not_a_failure(self, monkeypatch):
        """A checkout that was never installed still has a source root, which
        is the answer being asked for. Reporting no version beats refusing."""

        def _absent(_name):
            raise metadata.PackageNotFoundError

        monkeypatch.setattr(metadata, "version", _absent)
        result = server.server_info()

        assert result["success"]
        assert result["version"] is None
        assert result["source_root"]


class TestStaleness:
    def test_a_freshly_started_server_is_not_stale(self, info):
        assert info["source_stale"] is False

    def test_an_edit_after_startup_is_stale(self, monkeypatch):
        """The case worth catching. The process keeps serving what it loaded,
        so every tool answers correctly for a version that no longer exists."""
        monkeypatch.setattr(server, "_STARTED_AT", 0.0)

        assert server.server_info()["source_stale"] is True

    def test_staleness_ignores_files_outside_the_package(self, tmp_path, info):
        """A moved README is not a stale server. Widening this to the repo
        would report one on every commit and train the agent to ignore it."""
        (tmp_path / "README.md").write_text("newer than anything", encoding="utf-8")

        assert info["source_stale"] is False
