"""Explaining an empty mod log.

The failure being diagnosed here is the quietest one in the tool: a mod
deployed into a loader the player never launches. Every step reports success
and the log stays empty, so the only evidence is in what *other* loader trees
have been writing.

The rule these tests enforce hardest is that the diagnosis reports an
OBSERVATION and never a cause. "Another loader ran later" has two explanations
with opposite fixes -- wrong profile launched, or the right one launched and
died before writing -- so nothing here may quietly pick one.
"""

from __future__ import annotations

import os

import pytest

from modwright import server
from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.diagnosis import (
    NOTHING_RAN_SINCE_DEPLOY,
    LOADER_WROTE_NOTHING,
    LOGGING_DISABLED,
    OTHER_LOADER_RAN_LATER,
    diagnose_silence,
)
from modwright.project_config import ProjectConfig

from conftest import write_bepinex_config


def _age(path, *, seconds: int) -> None:
    """Backdate a file, so "which ran last" is unambiguous in a fast test."""
    when = os.stat(path).st_mtime - seconds
    os.utime(path, (when, when))


def _deploy(loader_root, *, age_days: int = 0):
    """Put a built plugin in place the way `deploy` really does.

    `shutil.copy2` carries the build's timestamp across, so the file's mtime
    is when the code was built, not when it was copied.
    """
    artifact = loader_root / "BepInEx" / "plugins" / "MyMod.dll"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"plugin")
    if age_days:
        when = os.stat(artifact).st_mtime - age_days * 86400
        os.utime(artifact, (when, when))
    return artifact


@pytest.fixture()
def adapter() -> BepInEx5Adapter:
    return BepInEx5Adapter()


@pytest.fixture()
def profile(fake_profile):
    """`fake_profile` filed under the same game the fake install is named.

    Discovery matches profiles to a game by the manager's folder name, so a
    profile under any other one is correctly invisible.
    """

    def _build(name: str, **kwargs):
        return fake_profile(name, game_folder="Game", **kwargs)

    return _build


@pytest.fixture()
def only_these_profiles(monkeypatch):
    """Restrict discovery to the profiles a test built itself.

    Without this, a machine with r2modman installed would have its real
    profiles show up in the candidate set.
    """

    def _use(*profile_dirs):
        roots = {p.parent.parent.parent for p in profile_dirs}
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs", lambda: sorted(roots)
        )

    return _use


class TestLoggingConfig:
    """Whether the loader would write a log at all, read from its own config.

    This has to come first: an empty log means nothing until it is ruled out,
    because a loader told not to log is indistinguishable from one that never
    ran.
    """

    def test_reads_logging_turned_off(self, adapter, fake_profile):
        profile = fake_profile(disk_logging=False)
        status = adapter.inspect_logging(profile)

        assert status.disabled is True
        assert status.config_path == profile / "BepInEx" / "config" / "BepInEx.cfg"

    def test_reads_logging_turned_on(self, adapter, fake_profile):
        assert adapter.inspect_logging(fake_profile(disk_logging=True)).disabled is False

    def test_no_config_at_all_is_not_reported_as_off(self, adapter, fake_profile):
        """A profile that has never run has no config, and BepInEx defaults to
        logging on -- so absence must never be read as "turned off"."""
        status = adapter.inspect_logging(fake_profile())

        assert status.disabled is False
        assert status.config_path is None

    def test_the_hint_names_the_file_and_setting(self, adapter, fake_profile):
        """The adapter owns this wording. Naming BepInEx.cfg in the server is
        the layering break that got the first attempt reverted."""
        hint = adapter.inspect_logging(fake_profile(disk_logging=False)).hint

        assert "BepInEx.cfg" in hint
        assert "[Logging.Disk]" in hint

    def test_ignores_the_same_key_in_another_section(self, adapter, fake_profile):
        """`[Logging.Console]` has its own `Enabled`, set to false in a stock
        config. Reading that one would report every install as silenced."""
        profile = fake_profile()
        write_bepinex_config(profile, disk_logging=True)

        assert adapter.inspect_logging(profile).disabled is False

    def test_missing_key_claims_nothing(self, adapter, fake_profile):
        profile = fake_profile()
        config = profile / "BepInEx" / "config" / "BepInEx.cfg"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[Logging.Disk]\nWriteUnityLog = true\n", encoding="utf-8")

        assert adapter.inspect_logging(profile).disabled is False

    def test_unreadable_value_claims_nothing(self, adapter, fake_profile):
        """Better to explain nothing than to announce logging is off because a
        value could not be parsed."""
        profile = fake_profile()
        config = profile / "BepInEx" / "config" / "BepInEx.cfg"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[Logging.Disk]\nEnabled = perhaps\n", encoding="utf-8")

        assert adapter.inspect_logging(profile).disabled is False

    def test_a_byte_order_mark_does_not_hide_the_section(self, adapter, fake_profile):
        profile = fake_profile()
        config = profile / "BepInEx" / "config" / "BepInEx.cfg"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "[Logging.Disk]\nEnabled = false\n", encoding="utf-8-sig"
        )

        assert adapter.inspect_logging(profile).disabled is True


class TestDiagnosis:
    def test_logging_off_explains_it_and_claims_nothing_else(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """A silenced loader accounts for the empty log on its own. Guessing
        at which profile ran on top of that would be noise."""
        game = fake_game("Game")
        target = profile("target", disk_logging=False)
        other = profile("other", log=True)
        only_these_profiles(target, other)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(context, adapter, None)

        assert result["reason"] == LOGGING_DISABLED
        assert "more_recent_loader" not in result

    def test_names_a_loader_that_wrote_more_recently(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        game = fake_game("Game")
        target = profile("target", log=True, disk_logging=True)
        other = profile("other", log=True, disk_logging=True)
        only_these_profiles(target, other)
        target_log = target / "BepInEx" / "LogOutput.log"
        _age(target_log, seconds=3600)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(context, adapter, target_log)

        assert result["reason"] == OTHER_LOADER_RAN_LATER
        assert result["more_recent_loader"]["path"] == str(other)
        assert "other" in result["more_recent_loader"]["label"]

    def test_no_log_at_all_still_compares_against_the_others(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """The most common shape of this bug: deploy into a fresh profile,
        never launch it. There is no log to read, not merely an empty one."""
        game = fake_game("Game")
        target = profile("target", disk_logging=True)
        other = profile("other", log=True, disk_logging=True)
        only_these_profiles(target, other)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(context, adapter, None)

        assert result["reason"] == OTHER_LOADER_RAN_LATER
        assert result["more_recent_loader"]["path"] == str(other)

    def test_the_target_is_not_offered_as_its_own_explanation(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """The target is discovered by the same scan as its rivals, so it has
        to be filtered out -- otherwise it always "ran later" than itself."""
        game = fake_game("Game")
        target = profile("target", log=True, disk_logging=True)
        only_these_profiles(target)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(
            context, adapter, target / "BepInEx" / "LogOutput.log"
        )

        assert result["reason"] == LOADER_WROTE_NOTHING

    def test_freshest_target_points_at_the_loader_not_the_target_choice(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        game = fake_game("Game")
        target = profile("target", log=True, disk_logging=True)
        other = profile("other", log=True, disk_logging=True)
        only_these_profiles(target, other)
        _age(other / "BepInEx" / "LogOutput.log", seconds=3600)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(
            context, adapter, target / "BepInEx" / "LogOutput.log"
        )

        assert result["reason"] == LOADER_WROTE_NOTHING

    def test_a_never_configured_loader_says_so_as_evidence(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """Offered as a hint rather than its own reason: an imported profile
        arrives carrying configs it never wrote, so this cannot be concluded
        from."""
        game = fake_game("Game")
        target = profile("target")
        only_these_profiles(target)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(context, adapter, None)

        assert result["reason"] == LOADER_WROTE_NOTHING
        assert any("never have been launched" in hint for hint in result["hints"])

    def test_the_game_folder_counts_as_a_rival_loader(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """The original form of this bug was a stale loader in the game folder
        winning over the profile the player actually uses."""
        game = fake_game("Game")
        (game / "BepInEx" / "LogOutput.log").write_text("ran\n", encoding="utf-8")
        target = profile("target", disk_logging=True)
        only_these_profiles(target)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(context, adapter, None)

        assert result["reason"] == OTHER_LOADER_RAN_LATER
        assert result["more_recent_loader"]["path"] == str(game)

    def test_never_tells_the_agent_to_switch_on_its_own(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """The whole point. An agent acting on this signal unprompted is right
        about half the time and destructive the other half."""
        game = fake_game("Game")
        target = profile("target", disk_logging=True)
        other = profile("other", log=True, disk_logging=True)
        only_these_profiles(target, other)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        hints = " ".join(diagnose_silence(context, adapter, None)["hints"])

        assert "Ask the user" in hints
        assert "do not switch" in hints


class TestWatchModLogs:
    @pytest.fixture()
    def project(self, fake_game, profile, tmp_path, only_these_profiles):
        def _build(**profile_kwargs):
            game = fake_game("Game")
            target = profile("target", **profile_kwargs)
            only_these_profiles(target)
            path = tmp_path / "proj"
            path.mkdir(exist_ok=True)
            ProjectConfig(
                "MyMod", "bepinex5", str(game), "Game", deploy_root=str(target)
            ).save(path)
            return path, target

        return _build

    def test_content_means_no_diagnosis_is_needed(self, project):
        """Content proves this loader ran, which settles the question outright."""
        path, target = project(log=True, disk_logging=True)
        result = server.watch_mod_logs(str(path))

        assert result["content"]
        assert "diagnosis" not in result

    def test_a_normal_poll_does_not_scan_for_profiles(self, project, monkeypatch):
        """Pins the gate as behaviour rather than a benchmark. This is the one
        tool an agent polls in a loop, and discovery is the expensive half."""
        path, _ = project(log=True, disk_logging=True)

        def _fail():
            raise AssertionError("discovery ran on a poll that returned content")

        monkeypatch.setattr("modwright.diagnosis.discover_profiles", _fail)
        assert server.watch_mod_logs(str(path))["content"]

    def test_an_empty_read_is_explained(self, project):
        path, target = project(log=True, disk_logging=False)
        first = server.watch_mod_logs(str(path))
        result = server.watch_mod_logs(str(path), since_cursor=first["cursor"])

        assert result["content"] == ""
        assert result["diagnosis"]["reason"] == LOGGING_DISABLED

    def test_a_missing_log_is_explained_rather_than_just_refused(self, project):
        """`log_not_found` used to say "run the game at least once", which is
        wrong and misleading when the user did run it, somewhere else."""
        path, _ = project(disk_logging=True)
        result = server.watch_mod_logs(str(path))

        assert result["success"] is False
        assert result["code"] == "log_not_found"
        assert result["diagnosis"]["reason"] == LOADER_WROTE_NOTHING


class TestNothingRanYet:
    """Ranking loaders against the deploy, before ranking them against
    each other.

    Without this, two logs left over from previous sessions get compared and
    the older one is announced as the wrong profile -- on the very first poll
    after a deploy, before the player has had any chance to launch. That is
    both the most common poll there is and a confidently wrong answer.
    """

    def test_stale_rivals_are_not_reported_as_the_wrong_profile(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        game = fake_game("Game")
        target = profile("target", log=True, disk_logging=True)
        other = profile("other", log=True, disk_logging=True)
        only_these_profiles(target, other)
        # Both logs predate the deploy: nobody has launched anything since.
        _age(target / "BepInEx" / "LogOutput.log", seconds=7 * 86400)
        _age(other / "BepInEx" / "LogOutput.log", seconds=2 * 86400)
        _deploy(target)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(
            context, adapter, target / "BepInEx" / "LogOutput.log"
        )

        assert result["reason"] == NOTHING_RAN_SINCE_DEPLOY
        assert "more_recent_loader" not in result
        assert result["deployed_at"] > result["last_loader_activity"]

    def test_a_rival_that_ran_after_the_deploy_still_wins(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """The real wrong-profile case must survive the new check."""
        game = fake_game("Game")
        target = profile("target", log=True, disk_logging=True)
        other = profile("other", log=True, disk_logging=True)
        only_these_profiles(target, other)
        _age(target / "BepInEx" / "LogOutput.log", seconds=7 * 86400)
        _deploy(target, age_days=1)  # built yesterday, other ran since

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(
            context, adapter, target / "BepInEx" / "LogOutput.log"
        )

        assert result["reason"] == OTHER_LOADER_RAN_LATER
        assert result["more_recent_loader"]["path"] == str(other)

    def test_an_undeployed_project_falls_back_to_comparing_loaders(
        self, adapter, fake_game, profile, only_these_profiles
    ):
        """With nothing deployed there is no moment to rank against, so the
        older comparison is still the best available answer."""
        game = fake_game("Game")
        target = profile("target", disk_logging=True)
        other = profile("other", log=True, disk_logging=True)
        only_these_profiles(target, other)

        context = adapter.adopt_loader_root(detect_framework(game), target)
        result = diagnose_silence(context, adapter, None)

        assert result["reason"] == OTHER_LOADER_RAN_LATER

    def test_every_read_says_when_the_log_was_written(
        self, fake_game, profile, only_these_profiles, tmp_path
    ):
        """A first poll returns the tail of whatever is already there, which
        can be weeks old. Content alone cannot be told apart from a live
        session, so the timestamp rides on every read, not just empty ones."""
        game = fake_game("Game")
        target = profile("target", log=True, disk_logging=True)
        only_these_profiles(target)
        path = tmp_path / "proj"
        path.mkdir(exist_ok=True)
        ProjectConfig(
            "MyMod", "bepinex5", str(game), "Game", deploy_root=str(target)
        ).save(path)

        result = server.watch_mod_logs(str(path))

        assert result["content"]
        assert "diagnosis" not in result
        assert result["log_written_at"]


class TestStaleContent:
    """A log that has not been written since the deploy.

    This is the shape the feature originally missed. Gating the diagnosis on
    an empty read only works while the agent polls with a cursor; after a
    redeploy it reasonably reads afresh, gets the full tail of a log written
    before the build it just made, and sees a wall of plausible text. Nothing
    in the response contradicted it, so the only way out was to leave the tool
    entirely and compare file times by hand.
    """

    @pytest.fixture()
    def project(self, fake_game, profile, tmp_path, only_these_profiles):
        def _build(*others, **profile_kwargs):
            game = fake_game("Game")
            target = profile("target", **profile_kwargs)
            only_these_profiles(target, *others)
            path = tmp_path / "proj"
            path.mkdir(exist_ok=True)
            ProjectConfig(
                "MyMod", "bepinex5", str(game), "Game", deploy_root=str(target)
            ).save(path)
            return path, target

        return _build

    def test_content_older_than_the_deploy_is_still_diagnosed(self, project):
        path, target = project(log=True, disk_logging=True)
        _age(target / "BepInEx" / "LogOutput.log", seconds=86400)
        _deploy(target)

        result = server.watch_mod_logs(str(path))

        # The text is right there and reads like any other session.
        assert result["content"]
        assert result["diagnosis"]["reason"] == NOTHING_RAN_SINCE_DEPLOY

    def test_the_wrong_profile_is_caught_without_a_cursor(
        self, project, profile
    ):
        """The rehearsal's actual failure: relaunched into another profile,
        read the target fresh, and got its stale tail back with no warning."""
        other = profile("other", log=True, disk_logging=True)
        path, target = project(other, log=True, disk_logging=True)
        _age(target / "BepInEx" / "LogOutput.log", seconds=86400)
        _deploy(target, age_days=1)  # built yesterday; `other` ran since

        result = server.watch_mod_logs(str(path))

        assert result["diagnosis"]["reason"] == OTHER_LOADER_RAN_LATER
        assert result["diagnosis"]["more_recent_loader"]["path"] == str(other)

    def test_a_log_written_since_the_deploy_is_left_alone(
        self, project, monkeypatch
    ):
        """The gate still holds with a deploy in place. This is the poll an
        agent runs in a loop, and discovery is the expensive half of it."""
        path, target = project(log=True, disk_logging=True)
        _deploy(target, age_days=1)  # the log is the newer of the two

        def _fail():
            raise AssertionError("discovery ran on a log written since deploy")

        monkeypatch.setattr("modwright.diagnosis.discover_profiles", _fail)
        result = server.watch_mod_logs(str(path))

        assert result["content"]
        assert "diagnosis" not in result

    def test_both_timestamps_ride_on_every_read(self, project):
        """One timestamp answers nothing on its own -- the agent needs
        something to compare it against, in the same payload."""
        path, target = project(log=True, disk_logging=True)
        _deploy(target)

        result = server.watch_mod_logs(str(path))

        assert result["log_written_at"]
        assert result["deployed_at"]

    def test_an_undeployed_project_reports_no_deploy_time(self, project):
        """Absence means "not known", never a second fact encoded as a
        timestamp. With nothing deployed there is no moment to rank against,
        so the staleness check must not fire at all."""
        path, _ = project(log=True, disk_logging=True)

        result = server.watch_mod_logs(str(path))

        assert result["deployed_at"] is None
        assert "diagnosis" not in result
