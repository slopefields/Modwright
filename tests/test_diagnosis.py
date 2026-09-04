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
from datetime import datetime, timedelta

import pytest

from modwright import server
from modwright.adapters import detect_framework
from modwright.adapters.bepinex5 import BepInEx5Adapter
from modwright.diagnosis import (
    LOAD_NOT_IN_LOG,
    LOAD_TIME_UNUSABLE,
    LOADS_NOT_RECORDED,
    NO_RESTART_SINCE_LAST_READ,
    NOTHING_DEPLOYED,
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


def _stamp(*, hours_ago: int) -> str:
    """A HarmonyX `### At` stamp, positioned against the clock the deploy uses.

    Whole seconds, because that is the resolution HarmonyX writes and the
    reason the comparison carries a second of slack.

    `%I`, not `%H`: HarmonyX formats this with `yyyy-MM-dd hh.mm.ss`, and .NET's
    lowercase `hh` is the TWELVE-hour hour, with no `tt` to say which half of
    the day it is. This fixture used to write a 24-hour stamp, which no loader
    ever produces, and every test over these lines passed against a shape that
    only existed here -- while the parser read real afternoon stamps as
    morning ones and called a fresh build stale.
    """
    when = datetime.now() - timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%d %I.%M.%S")


def _startup_block(artifact, stamp: str) -> str:
    """The three lines BepInEx and HarmonyX write when a plugin loads.

    Verbatim in shape from a real 2.4 MB Lethal Company log, because these
    lines are a format this parser depends on rather than one it defines.
    """
    return (
        "[Info   :   BepInEx] BepInEx 5.4.23.5 - Game (8/22/2026 4:40:30 PM)\n"
        "[Info   :   BepInEx] Chainloader started\n"
        "[Info   :   BepInEx] Loading [MyMod 1.0.0]\n"
        f"### Started from void MyMod.Plugin::.ctor(), location {artifact}\n"
        f"### At {stamp}\n"
    )


def _record_deploy(project_path, artifact) -> None:
    """Note the deployed file the way `deploy_mod` does, without a build."""
    config = ProjectConfig.load(project_path)
    config.deployed_artifact = str(artifact)
    config.save(project_path)


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

    def test_a_neighbouring_mod_does_not_count_as_this_deploy(self, project):
        """The bug this class exists for, pointed the other way.

        `plugins` holds every mod the user has installed. Reading "when was my
        mod deployed" as the newest file in there meant updating a dependency
        moved the deploy forward, and a log written since the real deploy got
        reported as predating it. The recorded artifact settles it: the
        neighbour is newer than everything and changes nothing.
        """
        path, target = project(log=True, disk_logging=True)
        artifact = _deploy(target, age_days=1)
        _age(target / "BepInEx" / "LogOutput.log", seconds=3600)
        _record_deploy(path, artifact)
        # Somebody else's mod, installed after ours and after the log was
        # written -- exactly what the folder scan used to pick up.
        (target / "BepInEx" / "plugins" / "SomeoneElse.dll").write_bytes(b"x")

        result = server.watch_mod_logs(str(path))

        assert result["content"]
        assert "diagnosis" not in result

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
        # Two days against one, not "written a moment earlier". Windows file
        # timestamps land on a coarse timer tick, so two files created
        # back-to-back share an mtime the overwhelming majority of the time --
        # this read `log == deploy`, which is not stale, and the test passed
        # or failed on which side of a tick the fixture happened to land.
        _age(target / "BepInEx" / "LogOutput.log", seconds=86400 * 2)
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


class TestRestartDetection:
    """Whether the running game is running the build that was just deployed.

    The third face of the silent-deploy failure, and the one that survived the
    first two fixes. A mod assembly is loaded once, when the process starts.
    A game left open through a redeploy keeps appending to the same log with
    the PREVIOUS build in memory, so the log is fresh, newer than the deploy,
    full of plausible text -- and every check the tool had said yes while the
    game ran the old code.

    Answered from the loader's own record of what it loaded and when, read
    from the top of the log. The cursor signals that came before could not
    answer it: BepInEx truncates its log on startup, so a session that outgrew
    the old offset before being polled leaves neither a shorter file to catch
    nor a banner at any offset the cursor reaches.
    """

    @pytest.fixture()
    def project(self, fake_game, profile, tmp_path, only_these_profiles):
        def _build(**profile_kwargs):
            game = fake_game("Game")
            target = profile("target", log=True, disk_logging=True, **profile_kwargs)
            only_these_profiles(target)
            path = tmp_path / "restart-proj"
            path.mkdir(exist_ok=True)
            ProjectConfig(
                "MyMod", "bepinex5", str(game), "Game", deploy_root=str(target)
            ).save(path)
            return path, target / "BepInEx" / "LogOutput.log"

        return _build

    def _append(self, log, text: str) -> None:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_load_after_the_build_is_this_build(self, project):
        """The session's headline failure, in the shape that produced it.

        Deploy hands back a cursor. BepInEx then truncates the log on relaunch
        and the new session writes past that offset before the poll, so the
        file is not short and the banner is behind the cursor -- both cursor
        signals blind at once. The loader's own timestamp is not.
        """
        path, log = project()
        artifact = _deploy(log.parent.parent, age_days=1)
        _record_deploy(path, artifact)
        log.write_text(
            _startup_block(artifact, _stamp(hours_ago=1))
            + "".join(f"line {i}\n" for i in range(5_000)),
            encoding="utf-8",
        )

        result = server.watch_mod_logs(str(path), since_cursor=200)

        assert result["running_this_build"] is True
        assert result["plugin_loaded_at"]
        assert "diagnosis" not in result

    def test_a_load_before_the_build_is_the_previous_one(self, project):
        """The failure the check exists for: the game was left running through
        the redeploy, so it holds an assembly read before the build landed."""
        path, log = project()
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        log.write_text(
            _startup_block(artifact, _stamp(hours_ago=6))
            + "[Info: MyMod] still the old build talking\n",
            encoding="utf-8",
        )

        result = server.watch_mod_logs(str(path))

        assert result["content"]  # plenty to read, and none of it proves a thing
        assert result["running_this_build"] is False
        assert result["diagnosis"]["reason"] == NO_RESTART_SINCE_LAST_READ

    def test_the_verdict_does_not_need_a_cursor(self, project):
        """It is two timestamps, so the first poll of a session answers it.

        What it replaced could not: with nothing behind it to compare against,
        a first read had to report "unknown" and the agent had to keep polling
        to find out what was already true.
        """
        path, log = project()
        artifact = _deploy(log.parent.parent, age_days=1)
        _record_deploy(path, artifact)
        log.write_text(_startup_block(artifact, _stamp(hours_ago=1)), encoding="utf-8")

        assert server.watch_mod_logs(str(path))["running_this_build"] is True

    def test_a_load_from_elsewhere_is_called_out(self, project):
        """A stale copy in another tree loads under the same name and reads as
        success everywhere. Only the path the loader recorded can see it."""
        path, log = project()
        artifact = _deploy(log.parent.parent, age_days=1)
        _record_deploy(path, artifact)
        elsewhere = artifact.parent.parent / "MyMod.dll"
        log.write_text(_startup_block(elsewhere, _stamp(hours_ago=1)), encoding="utf-8")

        result = server.watch_mod_logs(str(path))

        assert any("shadowing this build" in hint for hint in result["hints"])

    def test_a_startup_banner_still_settles_it_without_a_stamp(self, project):
        """The fallback, for a plugin that records no load of its own. Weaker,
        and still sound in this direction: a startup after a deploy-time cursor
        did happen."""
        path, log = project()
        cursor = server.watch_mod_logs(str(path))["cursor"]
        _deploy(log.parent.parent)
        self._append(log, "[Message: BepInEx] Chainloader started\n")

        result = server.watch_mod_logs(str(path), since_cursor=cursor)

        assert result["running_this_build"] is True
        assert "diagnosis" not in result

    def test_a_truncated_log_settles_it_too(self, project):
        """The other fallback signal: BepInEx truncates on startup, so a log
        shorter than the cursor is a process that began since."""
        path, log = project()
        _deploy(log.parent.parent)
        self._append(log, "a long previous session\n" * 40)
        cursor = server.watch_mod_logs(str(path))["cursor"]

        log.write_text("fresh, and shorter\n", encoding="utf-8")
        result = server.watch_mod_logs(str(path), since_cursor=cursor)

        assert result["running_this_build"] is True
        assert "diagnosis" not in result

    def test_the_fallback_never_answers_no(self, project):
        """The fix, stated as behaviour. Neither cursor signal firing means
        "not seen", and the log that exposed this showed both going quiet on a
        session that had restarted, was patched, and was working. Unknown says
        as much as was actually known; False said more."""
        path, log = project()
        cursor = server.watch_mod_logs(str(path))["cursor"]
        _deploy(log.parent.parent)
        self._append(log, "[Info: MyMod] no startup record anywhere in here\n")

        result = server.watch_mod_logs(str(path), since_cursor=cursor)

        assert result["running_this_build"] is None
        assert "diagnosis" not in result

    def test_a_read_with_no_cursor_and_no_record_claims_nothing(self, project):
        path, log = project()
        _deploy(log.parent.parent)
        self._append(log, "written since the deploy\n")

        result = server.watch_mod_logs(str(path))

        assert result["running_this_build"] is None
        assert "diagnosis" not in result

    def test_silence_is_diagnosed_ahead_of_the_restart_check(self, project):
        """A log that has not been written since the deploy has a better
        explanation available, and the profile scan can name it."""
        path, log = project()
        cursor = server.watch_mod_logs(str(path))["cursor"]
        _age(log, seconds=86400)
        _deploy(log.parent.parent)

        result = server.watch_mod_logs(str(path), since_cursor=cursor)

        assert result["diagnosis"]["reason"] == NOTHING_RAN_SINCE_DEPLOY

    def test_the_diagnosis_shows_its_working(self, project):
        """It now asserts something decisive, so it has to hand over the two
        timestamps it decided on rather than ask to be taken on trust."""
        path, log = project()
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        log.write_text(_startup_block(artifact, _stamp(hours_ago=6)), encoding="utf-8")

        diagnosis = server.watch_mod_logs(str(path))["diagnosis"]

        assert diagnosis["plugin_loaded_at"]
        assert diagnosis["deployed_at"]
        assert diagnosis["plugin_loaded_from"] == str(artifact)
        assert any("relaunch" in hint for hint in diagnosis["hints"])


class TestAPollIsBounded:
    """`watch_mod_logs` is documented to be polled from the cursor `deploy_mod`
    handed back, and to keep being polled from it until the loader is seen
    starting. That region is a whole play session: on this machine's own
    profiles it reaches a megabyte, which is a quarter of a million tokens in
    one tool response."""

    @pytest.fixture()
    def project(self, fake_game, profile, tmp_path, only_these_profiles):
        game = fake_game("Game")
        target = profile("target", log=True, disk_logging=True)
        only_these_profiles(target)
        path = tmp_path / "proj"
        path.mkdir(exist_ok=True)
        ProjectConfig(
            "MyMod", "bepinex5", str(game), "Game", deploy_root=str(target)
        ).save(path)
        _deploy(target)
        return path, target / "BepInEx" / "LogOutput.log"

    def test_a_long_session_does_not_come_back_whole(self, project):
        path, log = project
        log.write_text(
            "Chainloader started\n" + "".join(f"line {i}\n" for i in range(20_000)),
            encoding="utf-8",
        )

        result = server.watch_mod_logs(str(path), since_cursor=0, lines=20)

        assert len(result["content"].splitlines()) == 20
        assert result["omitted_lines"] == 19_981

    def test_the_trim_is_stated_rather_than_left_to_be_noticed(self, project):
        path, log = project
        log.write_text("".join(f"line {i}\n" for i in range(500)), encoding="utf-8")

        result = server.watch_mod_logs(str(path), since_cursor=0, lines=10)

        assert any("not shown" in hint for hint in result["hints"])

    def test_a_read_that_fits_says_nothing_about_omissions(self, project):
        path, log = project
        log.write_text("Chainloader started\nall of it\n", encoding="utf-8")

        result = server.watch_mod_logs(str(path), since_cursor=0, lines=50)

        assert "omitted_lines" not in result

    def test_a_restart_buried_in_the_dropped_region_is_still_seen(self, project):
        """The banner sits at the top of a freshly truncated log, so it is the
        first thing a tail drops -- and dropping it would report the running
        game as still holding the previous build."""
        path, log = project
        log.write_text(
            "Chainloader started\n" + "".join(f"line {i}\n" for i in range(20_000)),
            encoding="utf-8",
        )

        result = server.watch_mod_logs(str(path), since_cursor=0, lines=20)

        assert "Chainloader started" not in result["content"]
        assert result["running_this_build"] is True
        assert "diagnosis" not in result


class TestUnknownVerdictExplainsItself:
    """A blank `running_this_build` has to say WHY it is blank.

    Four situations produce it and they have four different remedies, so a
    bare null tells them apart for nobody. The adapter contract is explicit
    that a missing load record "must mean 'this log does not show it' and
    never 'it did not load' -- callers report the difference"; these pin that
    reporting down at the caller.

    Not hypothetical. Before this existed the blank arrived with no reason
    attached, and what actually happened was that the tool got abandoned
    mid-session in favour of grepping the log by hand.
    """

    @pytest.fixture()
    def project(self, fake_game, profile, tmp_path, only_these_profiles):
        def _build(**profile_kwargs):
            game = fake_game("Game")
            profile_kwargs.setdefault("disk_logging", True)
            target = profile("target", log=True, **profile_kwargs)
            only_these_profiles(target)
            path = tmp_path / "unknown-proj"
            path.mkdir(exist_ok=True)
            ProjectConfig(
                "MyMod", "bepinex5", str(game), "Game", deploy_root=str(target)
            ).save(path)
            return path, target / "BepInEx" / "LogOutput.log"

        return _build

    def test_the_shipped_default_is_named_along_with_its_remedy(self, project):
        """The common case, and the whole reason the explanation exists: the
        log is healthy, it simply never records where plugins came from."""
        path, log = project()
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        log.write_text("[Info   :   BepInEx] Chainloader started\n", encoding="utf-8")

        unknown = server.watch_mod_logs(str(path))["running_this_build_unknown"]

        assert unknown["reason"] == LOADS_NOT_RECORDED
        assert any("set_load_recording" in hint for hint in unknown["hints"])

    def test_the_default_is_not_reported_as_someone_turning_it_off(self, project):
        """A user reading this must not go hunting for who disabled it."""
        path, log = project()
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        log.write_text("[Info   :   BepInEx] Chainloader started\n", encoding="utf-8")

        unknown = server.watch_mod_logs(str(path))["running_this_build_unknown"]

        assert any("what BepInEx ships" in hint for hint in unknown["hints"])

    def test_the_reason_is_not_stated_twice(self, project):
        """These hints go to an agent, and the adapter's wording already
        carries the file, the setting and the remedy. Saying it again above
        spends context to add nothing."""
        path, log = project()
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        log.write_text("[Info   :   BepInEx] Chainloader started\n", encoding="utf-8")

        unknown = server.watch_mod_logs(str(path))["running_this_build_unknown"]

        repeated = [h for h in unknown["hints"] if "turned off here" in h]
        assert len(repeated) == 1

    def test_recording_on_but_this_mod_absent_is_a_different_finding(self, project):
        """Much louder than the default case: the loader IS reporting what it
        loads, and this mod is not in the list. It did not load."""
        path, log = project(log_channels="Warn, Error, Info")
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        log.write_text(
            _startup_block(
                artifact.parent / "SomeoneElse.dll", _stamp(hours_ago=1)
            ),
            encoding="utf-8",
        )

        unknown = server.watch_mod_logs(str(path))["running_this_build_unknown"]

        assert unknown["reason"] == LOAD_NOT_IN_LOG

    def test_a_missing_artifact_is_reported_as_nothing_deployed(self, project):
        """No build time to compare against, so the question cannot be asked
        -- which is a different problem from the log being uninformative."""
        path, log = project()
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        artifact.unlink()
        log.write_text("[Info   :   BepInEx] Chainloader started\n", encoding="utf-8")

        unknown = server.watch_mod_logs(str(path))["running_this_build_unknown"]

        assert unknown["reason"] == NOTHING_DEPLOYED

    def test_a_stamp_the_log_rules_out_is_not_quietly_believed(self, project):
        """A load time nobody believes must not be compared against a build
        time as though it were sound.

        A whole day ahead, not a few hours: on a 12-hour clock the earlier
        reading of an afternoon stamp lands in the morning and stays perfectly
        plausible, so only a different DATE puts both readings beyond the log
        that contains them. That is what a clock jumping forward looks like.
        """
        path, log = project(log_channels="All")
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        ahead = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %I.%M.%S")
        log.write_text(_startup_block(artifact, ahead), encoding="utf-8")

        unknown = server.watch_mod_logs(str(path))["running_this_build_unknown"]

        assert unknown["reason"] == LOAD_TIME_UNUSABLE

    def test_a_known_verdict_carries_no_explanation(self, project):
        """The explanation is for the blank. A decided answer needs none, and
        attaching one anyway would make every response look like a problem."""
        path, log = project(log_channels="Warn, Error, Info")
        artifact = _deploy(log.parent.parent, age_days=1)
        _record_deploy(path, artifact)
        log.write_text(_startup_block(artifact, _stamp(hours_ago=1)), encoding="utf-8")

        result = server.watch_mod_logs(str(path))

        assert result["running_this_build"] is True
        assert "running_this_build_unknown" not in result

    def test_explaining_the_blank_does_not_scan_for_profiles(self, project, monkeypatch):
        """This runs on a poll. Profile discovery is the expensive half of the
        other diagnosis and must stay off this path."""
        path, log = project()
        artifact = _deploy(log.parent.parent)
        _record_deploy(path, artifact)
        log.write_text("[Info   :   BepInEx] Chainloader started\n", encoding="utf-8")

        def _fail():
            raise AssertionError("discovery ran while explaining a blank verdict")

        monkeypatch.setattr("modwright.diagnosis.discover_profiles", _fail)

        assert server.watch_mod_logs(str(path))["running_this_build_unknown"]
