"""Turning on the loader's record of WHICH build it loaded, and reading it.

Two failures meet here, and they are the same failure at different stages.

BepInEx ships listening only to Harmony's `Warn` and `Error` channels, and the
three `### ...` lines naming the file a plugin was loaded from -- and when --
are logged on `Info`. So on a stock install the log says nothing about
provenance, `read_session` finds nothing, and the one question the log-watching
workflow exists to answer comes back unknown. `set_load_recording` is the
remedy: it adds `Info` to that setting.

And once those lines DO appear, they have to be read correctly. Harmony formats
the stamp with `yyyy-MM-dd hh.mm.ss` -- a twelve-hour hour with no AM/PM marker
anywhere in the line -- which was being parsed as twenty-four hour. Every
afternoon load therefore read twelve hours early, which turns "this is your
build" into "this is stale, quit and relaunch", advice a relaunch cannot
satisfy. Turning the setting on without fixing the parse would have replaced an
honest unknown with a confident wrong answer, so both live in one file.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.errors import ErrorCode, LoaderConfigNotFoundError

from conftest import write_bepinex_config


@pytest.fixture()
def adapter() -> BepInEx5Adapter:
    return BepInEx5Adapter()


@pytest.fixture()
def loader(tmp_path):
    """A loader tree with a BepInEx.cfg in it, shipped-default by default."""

    def _build(**kwargs):
        root = tmp_path / "profile"
        root.mkdir(exist_ok=True)
        kwargs.setdefault("disk_logging", True)
        write_bepinex_config(root, **kwargs)
        return root

    return _build


def _config(root):
    return root / "BepInEx" / "config" / "BepInEx.cfg"


class TestInspectLoadRecording:
    def test_the_shipped_default_does_not_record_loads(self, adapter, loader):
        """The point of the whole feature: this is OFF on a normal install.

        Not a misconfiguration to be scolded for -- the value being read here
        is exactly what BepInEx writes on its first run.
        """
        status = adapter.inspect_load_recording(loader())

        assert status.enabled is False
        assert status.setting == "Warn, Error"
        assert "set_load_recording" in status.hint

    def test_the_hint_says_it_is_the_shipped_default(self, adapter, loader):
        """A user reading this must not go hunting for who turned it off."""
        status = adapter.inspect_load_recording(loader())

        assert "ships" in status.hint

    @pytest.mark.parametrize("channels", ["Warn, Error, Info", "All", "info", "ALL"])
    def test_a_listening_config_is_recognised(self, adapter, loader, channels):
        """`All` implies `Info`, and the file's capitalisation is the user's."""
        status = adapter.inspect_load_recording(loader(log_channels=channels))

        assert status.enabled is True
        assert status.hint is None

    def test_no_config_is_reported_without_claiming_it_is_off(
        self, adapter, tmp_path
    ):
        """A loader that has never run has no config, which is not a setting.

        Reported with `config_path=None` so a caller can tell "read it and it
        was off" from "there was nothing to read".
        """
        status = adapter.inspect_load_recording(tmp_path / "never-run")

        assert status.enabled is False
        assert status.config_path is None
        assert "never been launched" in status.hint


class TestSetLoadRecording:
    def test_enabling_adds_info_to_what_is_already_there(self, adapter, loader):
        """The user's other channels are theirs. This adds one."""
        root = loader()
        change = adapter.set_load_recording(root, True)

        assert change.changed is True
        assert change.previous == "Warn, Error"
        assert change.current == "Warn, Error, Info"
        assert adapter.inspect_load_recording(root).enabled is True

    def test_enabling_does_not_reach_for_all(self, adapter, loader):
        """`All` would switch on the IL channel, which dumps whole patch
        bodies -- BepInEx's own comment on the setting says so. Burying the
        log is not an acceptable price for making it readable."""
        change = adapter.set_load_recording(loader(), True)

        assert "IL" not in change.current
        assert "All" not in change.current

    def test_enabling_twice_is_a_quiet_success(self, adapter, loader):
        """An agent may call this defensively before every deploy."""
        root = loader()
        adapter.set_load_recording(root, True)
        again = adapter.set_load_recording(root, True)

        assert again.changed is False
        assert again.enabled is True

    def test_every_other_setting_survives(self, adapter, loader):
        """A read-modify-write of somebody's loader config, so this is the
        test that matters most: one line changes and nothing else does."""
        root = loader()
        before = _config(root).read_text(encoding="utf-8").splitlines()

        adapter.set_load_recording(root, True)
        after = _config(root).read_text(encoding="utf-8").splitlines()

        assert len(before) == len(after)
        differing = [(a, b) for a, b in zip(before, after) if a != b]
        assert differing == [("LogChannels = Warn, Error", "LogChannels = Warn, Error, Info")]

    def test_the_documentation_comments_survive(self, adapter, loader):
        """The `##` lines above a setting are how a user knows what it does."""
        root = loader()
        adapter.set_load_recording(root, True)
        text = _config(root).read_text(encoding="utf-8")

        assert "## NOTE: IL channel dumps the whole patch methods" in text

    def test_crlf_line_endings_are_not_rewritten(self, adapter, loader):
        """Python translates line endings at both ends unless told not to.
        A profile under version control must not come back as an every-line
        diff because one value changed."""
        root = loader(newline="\r\n")
        adapter.set_load_recording(root, True)
        raw = _config(root).read_bytes()

        assert b"\r\n" in raw
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_disabling_puts_the_setting_back(self, adapter, loader):
        root = loader()
        adapter.set_load_recording(root, True)
        change = adapter.set_load_recording(root, False)

        assert change.changed is True
        assert change.current == "Warn, Error"
        assert adapter.inspect_load_recording(root).enabled is False

    def test_disabling_from_all_returns_to_the_shipped_default(
        self, adapter, loader
    ):
        """`All` is one token standing for every channel, so `Info` cannot be
        subtracted from it. Spelling out "everything except Info" would be
        inventing an intention the user never expressed; the default is the
        one reversal that claims nothing."""
        root = loader(log_channels="All")
        change = adapter.set_load_recording(root, False)

        assert change.current == "Warn, Error"

    def test_a_none_channel_is_replaced_rather_than_appended_to(
        self, adapter, loader
    ):
        """`None, Info` is a contradiction, not a configuration."""
        change = adapter.set_load_recording(loader(log_channels="None"), True)

        assert change.current == "Info"

    def test_a_missing_config_is_refused_rather_than_created(
        self, adapter, tmp_path
    ):
        """Writing a stub would be worse than failing: BepInEx regenerates
        this file with every default spelled out, replacing the stub and
        taking the edit with it."""
        with pytest.raises(LoaderConfigNotFoundError) as caught:
            adapter.set_load_recording(tmp_path / "never-run", True)

        assert caught.value.code == ErrorCode.LOADER_CONFIG_NOT_FOUND
        assert "never been launched" in str(caught.value)

    def test_a_config_without_the_setting_is_refused(self, adapter, loader):
        """A hand-trimmed config is not something to guess the shape of."""
        with pytest.raises(LoaderConfigNotFoundError):
            adapter.set_load_recording(loader(log_channels=None), True)


def _log(stamp: str, path: str = r"C:\profile\BepInEx\plugins\MyMod.dll") -> str:
    """The lines a real BepInEx + HarmonyX startup writes for one plugin."""
    return (
        "[Info   :   BepInEx] BepInEx 5.4.23.5 - Game (8/22/2026 4:40:30 PM)\n"
        "[Info   :   BepInEx] Chainloader started\n"
        "[Info   :   BepInEx] Loading [MyMod 1.0.0]\n"
        f"### Started from void MyMod.Plugin::.ctor(), location {path}\n"
        f"### At {stamp}\n"
    )


def _harmony_stamp(when: datetime) -> str:
    """Format a time the way HarmonyX does: `hh` is TWELVE-hour, and there is
    no `tt` anywhere in the format string to say which half of the day."""
    return when.strftime("%Y-%m-%d %I.%M.%S")


class TestTwelveHourStamp:
    def test_an_afternoon_load_is_not_read_as_the_morning(self, adapter):
        """The regression. A mod deployed and launched after noon read twelve
        hours early, so it looked loaded before it was built -- reported as a
        stale build, with a relaunch as the advice. Relaunching reproduced it
        exactly, because the next launch was in the afternoon too."""
        loaded = datetime(2026, 9, 3, 20, 5, 12)
        polled = loaded + timedelta(minutes=5)

        session = adapter.read_session(_log(_harmony_stamp(loaded)), "MyMod.dll", polled)

        assert session.started_at == loaded

    def test_a_morning_load_still_reads_as_the_morning(self, adapter):
        """The other half of the clock, which was never broken and must not
        become broken in the fixing."""
        loaded = datetime(2026, 9, 3, 8, 5, 12)
        polled = loaded + timedelta(minutes=5)

        session = adapter.read_session(_log(_harmony_stamp(loaded)), "MyMod.dll", polled)

        assert session.started_at == loaded

    def test_noon_and_midnight_do_not_collide(self, adapter):
        """`12` is the awkward one: it means 00 in the morning and 12 at
        midday, which is the opposite of how the other eleven hours work."""
        loaded = datetime(2026, 9, 3, 12, 30, 0)
        polled = loaded + timedelta(minutes=1)

        session = adapter.read_session(_log(_harmony_stamp(loaded)), "MyMod.dll", polled)

        assert session.started_at == loaded

    def test_the_reading_ruled_out_by_the_last_write_is_dropped(self, adapter):
        """A session cannot have begun after the log it is writing was last
        touched, which settles most stamps outright."""
        loaded = datetime(2026, 9, 3, 9, 0, 0)
        polled = datetime(2026, 9, 3, 9, 30, 0)

        session = adapter.read_session(_log(_harmony_stamp(loaded)), "MyMod.dll", polled)

        assert session.started_at == loaded
        assert session.started_at_alternative is None

    def test_a_genuinely_ambiguous_stamp_reports_both_readings(self, adapter):
        """A game up for more than twelve hours leaves both readings possible.
        The nearer one is taken and the other handed back, rather than the
        choice being made silently -- the caller often knows which is right."""
        polled = datetime(2026, 9, 3, 21, 0, 0)
        session = adapter.read_session(
            _log(_harmony_stamp(datetime(2026, 9, 3, 8, 5, 0))), "MyMod.dll", polled
        )

        assert session.started_at == datetime(2026, 9, 3, 20, 5, 0)
        assert session.started_at_alternative == datetime(2026, 9, 3, 8, 5, 0)

    def test_a_stamp_from_before_the_log_existed_claims_nothing(self, adapter):
        """Both readings after the last write means the stamp is not this
        session's -- another day, or a clock that moved. Unknown, not a guess."""
        session = adapter.read_session(
            _log(_harmony_stamp(datetime(2026, 9, 3, 23, 0, 0))),
            "MyMod.dll",
            datetime(2026, 9, 3, 6, 0, 0),
        )

        assert session.started_at is None

    def test_no_reference_time_means_no_claim(self, adapter):
        """With nothing to test the two readings against, picking one would be
        the original bug with an extra step."""
        session = adapter.read_session(
            _log(_harmony_stamp(datetime(2026, 9, 3, 20, 5, 0))), "MyMod.dll", None
        )

        assert session.started_at is None
        assert session.plugin_path is not None


class TestTheToolResolvesItsTarget:
    def test_a_loader_inside_the_game_folder_is_found(
        self, fake_game, tmp_path
    ):
        """`GameContext.loader_root` is None whenever BepInEx lives in the game
        folder rather than a manager profile, and the tool has to fall back to
        the install root. Passing the raw field through built a path under a
        `None` root and failed with a TypeError naming nothing useful."""
        from modwright import server
        from modwright.project_config import ProjectConfig
        from conftest import write_bepinex_config

        game = fake_game("Game")
        write_bepinex_config(game, disk_logging=True)
        path = tmp_path / "in-game-proj"
        path.mkdir()
        ProjectConfig("MyMod", "bepinex5", str(game), "Game").save(path)

        result = server.set_load_recording(str(path))

        assert result["success"] is True
        assert result["current"] == "Warn, Error, Info"
