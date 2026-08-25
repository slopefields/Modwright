"""Explaining an empty mod log.

A mod deployed into a loader the player never launches produces no error
anywhere: the build compiles, the copy succeeds, every step reports success,
and the log simply has nothing in it. It is the hardest failure in this tool
to diagnose from its symptom, because the symptom is silence.

This module turns that silence into evidence. It runs only when a log read
came back empty -- content in the log *proves* the loader ran, so there is
nothing to explain otherwise, and the check stays off the hot path of a tool
an agent polls repeatedly.

Two rules shape everything here:

* **Report what was observed, never what it means.** "Another loader tree
  wrote more recently than yours" is knowable. *Why* is not, and the two
  causes have opposite fixes: the player launched a different profile (switch
  the target), or they launched this one and it died before writing a line --
  loader failed to start, a mod threw during load -- where switching would
  HIDE a real fault and send the investigation the wrong way. Nothing in the
  filesystem distinguishes them, so the reasons are named for the observation
  and the agent is told to ask.

* **The framework's vocabulary stays in the adapter.** Which file holds the
  logging setting, and what to call it, is asked of the adapter rather than
  written down here. This module knows only that a loader can be configured
  not to log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from modwright.models import GameContext
from modwright.profiles import discover_profiles

#: Logging is turned off for the target, so it writes nothing however well the
#: mod is running. Fully explains an empty log; no other claim is made.
LOGGING_DISABLED = "logging_disabled"
#: No loader for this game has written anything since the mod was last put
#: there. Nothing has run yet, so comparing loaders against each other would
#: only rank stale logs against one another and read like a wrong-profile
#: report when the game simply has not been launched.
NOTHING_RAN_SINCE_DEPLOY = "nothing_ran_since_deploy"
#: Some other loader tree for this game wrote its log more recently than the
#: target did, and did so after the mod was deployed. An observation with two
#: opposite explanations -- see above.
OTHER_LOADER_RAN_LATER = "other_loader_ran_later"
#: Logging is not off, the target's own log is the most recent one there is,
#: and it is still empty. Named for what was seen rather than "failed": the
#: loader may have died before writing, or may never have been launched here.
LOADER_WROTE_NOTHING = "loader_wrote_nothing"

_ASK_DONT_SWITCH = (
    "Ask the user which profile they launched -- do not switch the deploy "
    "target on this evidence alone. Two very different situations look "
    "identical from disk: they may have launched a different profile (switch "
    "or relaunch), or they may have launched this one and had it fail before "
    "writing anything (switching would hide the real fault)."
)


@dataclass(frozen=True)
class _Candidate:
    """Another loader tree for this game that might have run instead."""

    label: str
    path: Path
    #: When this tree last wrote its log, or None if it never has. Read once
    #: at construction rather than on access: the comparison below, the
    #: sort, and the response all want the same number, and a file that
    #: disappeared between two of those reads would otherwise turn a
    #: diagnosis into a TypeError.
    written_at: float | None
    manager: str | None = None


def diagnose_empty_log(
    context: GameContext, adapter: Any, target_log: Path | None
) -> dict[str, Any]:
    """Explain why the target loader's log is empty (or absent).

    `target_log` is None when no log file exists at all -- the strongest form
    of the same symptom, not a separate problem.
    """
    target_root = context.effective_loader_root

    status = adapter.inspect_logging(target_root)
    if status.disabled:
        # This alone accounts for the empty log, and nothing can be inferred
        # about which profile ran: a loader told not to log looks exactly like
        # one that never started.
        return _payload(LOGGING_DISABLED, target_root, hints=[status.hint])

    newest = _most_recent_other_loader(context, adapter, target_root)
    target_written = _mtime(target_log)

    # Rank every loader against the moment the mod was put in place, before
    # ranking them against each other. Without this, two logs left over from
    # last week get compared and the older one is reported as the wrong
    # profile -- on the very first poll, before the player has had a chance
    # to launch anything.
    deployed_at = _last_deployed(context)
    latest_run = max(
        (t for t in (target_written, newest.written_at if newest else None)
         if t is not None),
        default=None,
    )
    if deployed_at is not None and (latest_run is None or latest_run < deployed_at):
        payload = _payload(
            NOTHING_RAN_SINCE_DEPLOY,
            target_root,
            hints=[
                "No loader for this game has written a log since this mod was "
                "last built and deployed, so nothing here has run yet.",
                "Most often that just means the game has not been launched "
                "since the deploy -- ask before reading anything more into it. "
                "The alternative is that it WAS launched and the loader never "
                "got far enough to write its first line.",
            ],
        )
        payload["deployed_at"] = _isoformat(deployed_at)
        payload["last_loader_activity"] = _isoformat(latest_run)
        return payload

    if newest is not None and (
        target_written is None or newest.written_at > target_written
    ):
        payload = _payload(
            OTHER_LOADER_RAN_LATER,
            target_root,
            hints=[
                f"{newest.label} wrote its log at "
                f"{_isoformat(newest.written_at)}, more recently than the "
                f"loader this mod deploys into. Only one loader is active per "
                f"launch, so whichever ran is the only one whose mods loaded.",
                _ASK_DONT_SWITCH,
                "If they did launch a different profile, set_deploy_target "
                "moves this project onto it.",
            ],
        )
        payload["more_recent_loader"] = {
            "label": newest.label,
            "path": str(newest.path),
            "manager": newest.manager,
            "log_written_at": _isoformat(newest.written_at),
        }
        payload["target_log_written_at"] = _isoformat(target_written)
        return payload

    hints = [
        "Logging is enabled here and no other loader tree for this game has "
        "written more recently, so the mod's own loader is the one that ran "
        "last -- and it still produced nothing.",
        "That points at the loader rather than the deploy target: it may not "
        "have started at all, or may have failed before writing its first "
        "line. Check that the game is actually launched through this loader.",
    ]
    if status.config_path is None:
        # Weak evidence, offered as evidence rather than acted on: an imported
        # profile arrives carrying configs it never wrote itself.
        hints.append(
            "This loader has no config file yet, which loaders normally write "
            "on their first run -- so it may never have been launched at all."
        )
    return _payload(LOADER_WROTE_NOTHING, target_root, hints=hints)


def _most_recent_other_loader(
    context: GameContext, adapter: Any, target_root: Path
) -> _Candidate | None:
    """The most recently written loader tree that is *not* the target."""
    target = _resolved(target_root)
    dated = [
        candidate
        for candidate in _candidates(context, adapter)
        if _resolved(candidate.path) != target and candidate.written_at is not None
    ]
    return max(dated, key=lambda c: c.written_at) if dated else None


def _candidates(context: GameContext, adapter: Any) -> list[_Candidate]:
    """Every loader tree for this game that could have run instead."""
    found = [
        _Candidate(
            label=f"Profile {profile.name!r} ({profile.manager})",
            path=profile.path,
            written_at=_mtime(profile.log_path),
            manager=profile.manager,
        )
        for profile in discover_profiles(context.game_name)
    ]

    # The game folder is a loader tree too, and deploying into a stale one
    # there is the original form of this bug.
    info = adapter.inspect_loader_root(context.install_root)
    if info is not None:
        found.append(
            _Candidate(
                label="The game folder's own loader",
                path=context.install_root,
                written_at=_mtime(info.log_path),
            )
        )
    return found


def _last_deployed(context: GameContext) -> float | None:
    """When something was last installed into the target's mods folder.

    Read as the newest file in there rather than the folder's own timestamp:
    on Windows a directory's mtime does not move when an existing file is
    overwritten, and overwriting is exactly what a redeploy does -- so the
    folder would report the FIRST deploy forever. `shutil.copy2` carries the
    build's timestamp across, which makes this "when was this code last
    built and put here", which is the question worth asking.
    """
    mods_dir = context.mods_dir
    if mods_dir is None or not mods_dir.is_dir():
        return None
    try:
        stamps = [entry.stat().st_mtime for entry in mods_dir.iterdir()]
    except OSError:
        return None
    return max(stamps, default=None)


def _payload(reason: str, loader_root: Path, hints: list[str]) -> dict[str, Any]:
    return {
        "reason": reason,
        "loader_root": str(loader_root),
        "hints": [hint for hint in hints if hint],
    }


def _mtime(path: Path | None) -> float | None:
    if path is None or not path.is_file():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _isoformat(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


def _resolved(path: Path) -> Path:
    # Profiles and install roots reach here from different sources (a config
    # file, a directory scan), so compare canonical paths rather than text.
    try:
        return path.resolve()
    except OSError:
        return path
