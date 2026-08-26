"""ModWright MCP server: the mod-authoring lifecycle as agent-callable tools.

Division of labour: DecompilerServer answers "what does the game's code look
like"; ModWright answers "turn that into a mod that builds, deploys, and runs".
Every tool here returns a plain dict with `success`, and on failure a stable
`code` from `modwright.errors.ErrorCode` so an agent can branch without
matching on message text.
"""

from __future__ import annotations

import functools
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mcp.server import MCPServer

from modwright.adapters import detect_framework, get_adapter
from modwright.diagnosis import diagnose_silence, last_deployed, mtime_of
from modwright.errors import (
    BuildFailedError,
    DeployTargetUnsetError,
    LogNotFoundError,
    ModReferenceNotFoundError,
    ModwrightError,
)
from modwright.logs import read_since
from modwright.mods import find_installed_mod, list_installed_mods
from modwright.models import GameContext
from modwright.profiles import discover_profiles
from modwright.project_config import ModReference, ProjectConfig
from modwright.validation import validate_targets

mcp = MCPServer("modwright")


def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Convert `ModwrightError` into a structured response.

    Anything else propagates: an unexpected exception is a bug, and burying it
    in a success-shaped envelope would hide it from both user and agent.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except ModwrightError as exc:
            return exc.to_response()

    return wrapper


def _async_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`_tool` for coroutine tools -- those that call DecompilerServer."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except ModwrightError as exc:
            return exc.to_response()

    return wrapper


def _context_for_project(project_path: Path) -> tuple[GameContext, Any, ProjectConfig]:
    """Resolve a project to its game install, plus any separate loader tree.

    Detection always runs against the game install, since that is what owns
    the assemblies. A configured `deploy_root` then moves deploy and log
    resolution onto a mod-manager profile without touching the rest.
    """
    config = ProjectConfig.load(project_path)
    adapter = get_adapter(config.framework_id)
    context = detect_framework(config.install_root)
    if config.deploy_root:
        context = adapter.adopt_loader_root(context, Path(config.deploy_root))
    return context, adapter, config


def _isoformat(mtime: float | None) -> str | None:
    """A timestamp as text, or None when there is no timestamp to report."""
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


def _require_chosen_target(
    context: GameContext, adapter: Any, config: ProjectConfig
) -> None:
    """Refuse to guess a destination when the user has a real choice.

    Silence is the dangerous answer here. If the player runs this game through
    a mod manager, installing into the game folder succeeds at every step and
    then loads nothing, which is near-impossible to debug from the symptom.
    So: ask when there is something to ask about, and stay quiet when there is
    genuinely only one place the mod can go.
    """
    if config.deploy_root or not adapter.supports_deploy_target:
        return
    profiles = discover_profiles(config.game_name)
    if not profiles:
        if context.mods_dir is None:
            # The game is launched through a loader that lives outside the
            # install, and no profile for it could be found -- an unfamiliar
            # manager, or a tree assembled by hand. The path cannot be
            # guessed, but it can be asked for.
            raise DeployTargetUnsetError(
                f"{config.game_name} is launched through a mod loader that is "
                "not inside the game folder, and none could be found "
                "automatically.",
                hints=[
                    "Ask the user where their loader lives, then pass that "
                    "path to set_deploy_target.",
                    "For a mod manager, this is the profile folder -- the one "
                    "containing BepInEx/.",
                ],
            )
        return  # No manager involved; the game folder is the only option.

    raise DeployTargetUnsetError(
        f"{len(profiles)} mod-manager profile(s) exist for {config.game_name}, "
        "so where this mod should be installed is ambiguous.",
        hints=[
            "Ask which profile to deploy into, then set it with "
            "set_deploy_target -- it is remembered afterwards.",
            "Only one profile is active at a time: the mod loads only if the "
            "player launches the same profile it was deployed into.",
            "To install into the game folder itself, pass that path to "
            "set_deploy_target explicitly.",
            "A fresh profile is often the better answer than reusing a busy "
            "one: create it in the mod manager, then install the BepInEx pack "
            "into it (installing any mod pulls it in). ModWright does not "
            "create profiles -- the manager keeps its own records, and a "
            "folder made behind its back would not match them.",
        ],
        details={
            "profiles": [
                {
                    "name": p.name,
                    "path": str(p.path),
                    "mod_count": p.mod_count,
                    "ever_launched": p.log_path is not None,
                }
                for p in profiles
            ]
        },
    )


@mcp.tool()
@_tool
def detect_game(install_root: str) -> dict[str, Any]:
    """Identify which modding framework a game install uses.

    Call this before scaffolding to confirm the game is supported. Refuses
    clearly for IL2CPP games, whose real logic cannot be decompiled at all.
    """
    context = detect_framework(install_root)
    adapter = get_adapter(context.framework_id)
    response = {
        "success": True,
        "game_name": context.game_name,
        "framework_id": context.framework_id,
        "framework": adapter.display_name,
        "install_root": str(context.install_root),
        "managed_dir": str(context.managed_dir) if context.managed_dir else None,
        "mods_dir": str(context.mods_dir) if context.mods_dir else None,
    }

    # Surface profiles here rather than waiting to be asked. If the player
    # runs this game through a mod manager, the loader in the game folder is
    # often stale or absent, and deploying into it fails silently: the build
    # succeeds, the copy succeeds, and the mod never loads.
    profiles = (
        discover_profiles(context.game_name)
        if adapter.supports_deploy_target
        else []
    )
    if profiles:
        response["mod_manager_profiles"] = [
            {"name": p.name, "manager": p.manager, "path": str(p.path)}
            for p in profiles
        ]
        response["hints"] = [
            f"{len(profiles)} mod-manager profile(s) found for this game. "
            "Only one profile is active at a time: the mod will load only if "
            "it is deployed into the same profile the player launches. Pass "
            "deploy_root when scaffolding to choose which.",
            "Ask which profile to use rather than defaulting to the game "
            "folder -- deploying there when the player launches through a "
            "manager fails silently.",
        ]
    return response


@mcp.tool()
@_tool
def scaffold_mod_project(
    install_root: str,
    project_path: str,
    mod_name: str,
    deploy_root: str | None = None,
) -> dict[str, Any]:
    """Create a buildable mod project targeting a specific game install.

    References to the game's own assemblies are resolved automatically from the
    detected install, which is the step framework templates leave manual.

    Pass `deploy_root` when mods are run through a manager (r2modman,
    Thunderstore Mod Manager, Gale) rather than from the game folder: the
    project still compiles against the install, but deploys into that
    profile and reads its log. `list_mod_profiles` finds the candidates.
    """
    context = detect_framework(install_root)
    adapter = get_adapter(context.framework_id)
    project = Path(project_path)

    if deploy_root:
        context = adapter.adopt_loader_root(context, Path(deploy_root))

    written = adapter.scaffold(project, context, mod_name)
    config = ProjectConfig(
        mod_name=mod_name,
        framework_id=context.framework_id,
        install_root=str(context.install_root),
        game_name=context.game_name,
        deploy_root=str(context.loader_root) if context.loader_root else None,
    )
    written.append(config.save(project))

    return {
        "success": True,
        "project_path": str(project),
        "framework": adapter.display_name,
        "deploy_root": config.deploy_root,
        "files_written": [str(p) for p in written],
    }


@mcp.tool()
@_async_tool
async def validate_mod_patches(project_path: str) -> dict[str, Any]:
    """Check that every method the mod patches actually exists in the game.

    Harmony takes its target method name as a plain string, so the compiler
    never checks it -- a typo or a name changed by a game update builds fine
    and only fails when the game launches and `PatchAll` cannot resolve it.
    This runs that same lookup against the game assembly's metadata first.
    """
    context, adapter, _ = _context_for_project(Path(project_path))
    extracted = adapter.extract_patch_targets(Path(project_path))

    # Targets whose attribute arguments were not literals cannot be checked
    # without compiling; they are reported as unchecked rather than passed.
    unresolved = [t for t in extracted if t.unresolved_reason]
    checkable = [t for t in extracted if not t.unresolved_reason]

    validated = await validate_targets(checkable, context)
    missing = [v for v in validated if not v.exists]

    return {
        "success": True,
        "valid": not missing,
        "checked": len(validated),
        "missing": [
            {
                "target": v.target.display,
                "file": str(v.target.source_file),
                "line": v.target.line,
                "did_you_mean": v.candidates,
            }
            for v in missing
        ],
        "found": [
            {"target": v.target.display, "line": v.target.line}
            for v in validated
            if v.exists
        ],
        "unchecked": [
            {
                "target": t.display,
                "file": str(t.source_file),
                "line": t.line,
                "reason": t.unresolved_reason,
            }
            for t in unresolved
        ],
    }


@mcp.tool()
@_tool
def build_mod(project_path: str) -> dict[str, Any]:
    """Compile the mod project.

    Some frameworks place the finished artifact during the build itself; the
    response says which happened so the agent knows whether a deploy is still
    needed.
    """
    context, adapter, config = _context_for_project(Path(project_path))
    try:
        outcome = adapter.build(Path(project_path))
    except BuildFailedError as exc:
        # Only when the build breaks. A dependency's assembly timestamp moves
        # every time it is rebuilt and redeployed, which during development is
        # most of the time -- reported on every passing build it would be
        # constant noise and quickly tuned out. Beside a real failure it is
        # worth knowing, so it is attached there and nowhere else.
        changed = _changed_references(context, config)
        if changed:
            exc.details.setdefault("reference_warnings", []).extend(changed)
        raise
    response = {
        "success": True,
        "artifact": str(outcome.artifact) if outcome.artifact else None,
        "deployed_by_build": outcome.deployed_by_build,
        "deploy_required": not outcome.deployed_by_build,
        "log": outcome.log,
    }
    drift = _reference_drift(context, config)
    if drift:
        response["reference_warnings"] = drift
    return response


def _installed_dependencies(
    context: GameContext, config: ProjectConfig
) -> list[Any]:
    """Installed mods, minus this project's own deployed build output.

    A project deploys into the same folder it reads dependencies from, so once
    dependencies installed as a bare file are visible, the project's own
    artifact sitting right there would be offered back to it. Matched on name
    rather than on a filename, which keeps the framework's file extension out
    of here.
    """
    if context.mods_dir is None:
        return []
    own = config.mod_name.lower()
    return [m for m in list_installed_mods(context.mods_dir) if m.package.lower() != own]


def _changed_references(context: GameContext, config: ProjectConfig) -> list[str]:
    """Dependencies whose assembly has moved since the reference was added.

    What a version cannot answer for a dependency installed as a bare file,
    which carries no manifest to hold one. Reported as an observation: a
    dependency changing and a method name being misspelled produce the same
    compiler error, and nothing here can tell them apart.
    """
    if not config.references or context.mods_dir is None:
        return []

    warnings: list[str] = []
    for reference in config.references:
        if reference.assembly_mtime_when_added is None:
            continue
        installed = find_installed_mod(context.mods_dir, reference.package)
        if installed is None:
            continue  # Already reported, as missing, by `_reference_drift`.
        now = installed.last_changed
        if now is not None and now != reference.assembly_mtime_when_added:
            warnings.append(
                f"{reference.package} has changed on disk since this project "
                f"started referencing it "
                f"(now {_isoformat(now)}, was "
                f"{_isoformat(reference.assembly_mtime_when_added)}). "
                "If the build only just started failing, check that first."
            )
    return warnings


def _reference_drift(context: GameContext, config: ProjectConfig) -> list[str]:
    """Report dependencies whose installed version has moved since they were added.

    Not a failure and not a pin: the project compiles against whatever is
    installed either way. The point is that a dependency updating underneath
    you should be something you are told, rather than something inferred later
    from a puzzling compile error about a method that used to exist.
    """
    if not config.references or context.mods_dir is None:
        return []

    warnings: list[str] = []
    for reference in config.references:
        installed = find_installed_mod(context.mods_dir, reference.package)
        if installed is None:
            warnings.append(
                f"{reference.package} is referenced but no longer installed."
            )
        elif (
            reference.version_when_added
            and installed.version
            and installed.version != reference.version_when_added
        ):
            warnings.append(
                f"{reference.package} was {reference.version_when_added} when it "
                f"was added and is {installed.version} now; the build compiles "
                "against the installed version."
            )
    return warnings


@mcp.tool()
@_tool
def list_mod_profiles(game_name: str | None = None) -> dict[str, Any]:
    """List mod-manager profiles that a mod could be deployed into.

    Mod managers keep each profile as a standalone loader tree outside the
    game folder, so for most players the game install is NOT where mods
    actually load from. Call this before scaffolding to pick a deploy target.

    Returns an empty list rather than failing when no manager is installed --
    that just means mods load from the game folder, which is the default.
    """
    profiles = discover_profiles(game_name)
    return {
        "success": True,
        "profiles": [
            {
                "manager": p.manager,
                "game": p.game_folder,
                "name": p.name,
                "path": str(p.path),
                "mod_count": p.mod_count,
                "ever_launched": p.log_path is not None,
                "framework_id": p.framework_id,
            }
            for p in profiles
        ],
        "hints": [
            "A profile with few mods keeps the log readable and makes a "
            "failure attributable to your mod. Test against a busy profile "
            "when you need to check for conflicts.",
            "Any loader tree works, listed or not -- pass its path directly.",
        ]
        if profiles
        else [
            "No mod-manager profiles found; mods will deploy into the game "
            "folder. Pass a path directly if your manager keeps them "
            "somewhere ModWright does not know about.",
        ],
    }


@mcp.tool()
@_tool
def set_deploy_target(project_path: str, deploy_root: str | None) -> dict[str, Any]:
    """Change where an existing project deploys to, and reads logs from.

    Pass None to go back to deploying into the game install itself. The
    project keeps compiling against the game's assemblies either way.
    """
    project = Path(project_path)
    config = ProjectConfig.load(project)
    adapter = get_adapter(config.framework_id)
    context = detect_framework(config.install_root)

    if deploy_root is not None:
        # Validate before persisting, so a bad path fails now rather than at
        # the next deploy.
        context = adapter.adopt_loader_root(context, Path(deploy_root))
        config.deploy_root = str(context.loader_root)
    else:
        config.deploy_root = None

    # Regenerate BEFORE persisting. The loader moving means what the project
    # COMPILES against moves with it, and this step can legitimately fail --
    # the new target may not have a referenced mod installed. Saving first
    # would leave the config pointing somewhere the generated build file does
    # not, which is worse than not switching at all.
    _regenerate_props(project, context, adapter, config)
    config.save(project)

    return {
        "success": True,
        "deploy_root": config.deploy_root,
        "mods_dir": str(context.mods_dir) if context.mods_dir else None,
    }


@mcp.tool()
@_tool
def set_game_install(project_path: str, install_root: str) -> dict[str, Any]:
    """Point a project at the game install on THIS machine.

    Needed in two situations: a mod project cloned from someone else, whose
    paths were never committed, and a game that has been moved or reinstalled
    somewhere else. Everything else about the project -- its dependencies, its
    code -- is unaffected.
    """
    project = Path(project_path)
    config = ProjectConfig.load_shared(project)
    adapter = get_adapter(config.framework_id)

    # Validate by detecting against it, so a wrong path is refused now rather
    # than surfacing later as a confusing build error.
    context = detect_framework(install_root)
    config.install_root = str(context.install_root)
    if config.deploy_root:
        context = adapter.adopt_loader_root(context, Path(config.deploy_root))
    # Same ordering rule as set_deploy_target: nothing is persisted until the
    # generated build file has been written successfully.
    _regenerate_props(project, context, adapter, config)
    config.save(project)

    return {
        "success": True,
        "install_root": config.install_root,
        "game_name": context.game_name,
        "deploy_root": config.deploy_root,
        "mods_dir": str(context.mods_dir) if context.mods_dir else None,
        "configured": context.mods_dir is not None,
    }


def _regenerate_props(
    project: Path, context: GameContext, adapter: Any, config: ProjectConfig
) -> None:
    """Rewrite the generated build file after a path changed.

    Skipped when the deploy target is still unknown: the file needs a mods
    directory, and inventing one is exactly what must not happen. The build
    then fails loudly saying the project is unconfigured, which is true.
    """
    if context.mods_dir is None:
        return
    adapter.write_project_props(project, context, [r.package for r in config.references])


@mcp.tool()
@_tool
def list_available_mods(project_path: str) -> dict[str, Any]:
    """List mods installed alongside this project, available to reference.

    Use before `add_mod_reference` to see what is there. A mod that builds on
    another -- a networking library, a shared API -- compiles against the copy
    already installed in the deploy target, so it matches the version the game
    will actually load and nothing has to be downloaded or copied.
    """
    context, _, config = _context_for_project(Path(project_path))
    if context.mods_dir is None:
        return {"success": True, "mods": []}

    return {
        "success": True,
        "mods_dir": str(context.mods_dir),
        "mods": [
            {
                "package": mod.package,
                "name": mod.display_name,
                "version": mod.version,
                "assemblies": [a.name for a in mod.assemblies],
                "referenceable": mod.referenceable,
            }
            for mod in _installed_dependencies(context, config)
        ],
    }


@mcp.tool()
@_tool
def add_mod_reference(project_path: str, package: str) -> dict[str, Any]:
    """Compile this project against another installed mod.

    `package` accepts the folder name (`xilophor-StaticNetcodeLib`) or just
    the mod name (`StaticNetcodeLib`).

    Every managed assembly the package ships is referenced, because which one
    holds the API cannot be known from the outside -- package names and
    assembly names often differ entirely. Unused references cost nothing: C#
    binds only what the code actually uses. Native libraries and framework
    assemblies are excluded, since referencing either breaks the build.
    """
    project = Path(project_path)
    context, adapter, config = _context_for_project(project)
    if context.mods_dir is None:
        raise ModReferenceNotFoundError(
            f"{config.game_name} has no mods directory to reference from."
        )

    mod = find_installed_mod(context.mods_dir, package)
    if mod is None:
        raise ModReferenceNotFoundError(
            f"No mod matching {package!r} is installed in {context.mods_dir}.",
            hints=[
                "Install it through this profile's mod manager first.",
                "list_available_mods shows what is installed.",
            ],
        )
    if mod.package.lower() == config.mod_name.lower():
        # This project deploys into the folder it reads dependencies from, so
        # its own artifact is sitting there among them.
        raise ModReferenceNotFoundError(
            f"{mod.package} is this project's own build output, not a "
            "dependency it can compile against.",
            hints=["A project cannot reference itself."],
        )
    if not mod.referenceable:
        raise ModReferenceNotFoundError(
            f"{mod.package} ships no assembly that can be referenced.",
            hints=[skip.reason for skip in mod.skipped] or None,
        )

    packages = [r.package for r in config.references if r.package != mod.package]
    packages.append(mod.package)
    adapter.write_project_props(project, context, packages)

    config.references = [r for r in config.references if r.package != mod.package]
    config.references.append(
        ModReference(
            package=mod.package,
            version_when_added=mod.version,
            assembly_mtime_when_added=mod.last_changed,
        )
    )
    config.save(project)

    return {
        "success": True,
        "package": mod.package,
        "version": mod.version,
        "referenced": [str(a) for a in mod.assemblies],
        "skipped": [
            {"file": skip.path.name, "reason": skip.reason} for skip in mod.skipped
        ],
        "hints": [
            "Declare this as a dependency when publishing, or players will "
            "not have it installed.",
        ],
    }


@mcp.tool()
@_tool
def remove_mod_reference(project_path: str, package: str) -> dict[str, Any]:
    """Stop compiling this project against another mod."""
    project = Path(project_path)
    context, adapter, config = _context_for_project(project)

    remaining = [r for r in config.references if r.package.lower() != package.lower()]
    if len(remaining) == len(config.references):
        raise ModReferenceNotFoundError(
            f"{package!r} is not referenced by this project.",
            hints=[f"Referenced: {', '.join(r.package for r in config.references)}"]
            if config.references
            else ["This project references no other mods."],
        )

    adapter.write_project_props(project, context, [r.package for r in remaining])
    config.references = remaining
    config.save(project)
    return {
        "success": True,
        "references": [r.package for r in remaining],
    }


@mcp.tool()
@_tool
def deploy_mod(project_path: str) -> dict[str, Any]:
    """Build the mod and place it where the game loads mods from.

    That is the project's configured deploy target -- a mod-manager profile
    when one is set, otherwise the game install.
    """
    context, adapter, config = _context_for_project(Path(project_path))
    _require_chosen_target(context, adapter, config)
    outcome = adapter.build(Path(project_path))
    deployed = adapter.deploy(outcome, context)
    return {
        "success": True,
        "destination": str(deployed.destination),
        "copied": deployed.copied,
        "deploy_root": config.deploy_root,
    }


@mcp.tool()
@_tool
def watch_mod_logs(
    project_path: str, since_cursor: int | None = None, lines: int = 50
) -> dict[str, Any]:
    """Read the game's mod log, resuming from a previous cursor.

    Poll-on-demand: call once after deploying, then again with the returned
    cursor while reproducing an issue in-game. An MCP server cannot push
    updates, so the agent drives the loop.

    Every read reports `log_written_at` and `deployed_at`. Compare them before
    trusting the content: a log older than the deploy holds only text from
    before this build existed, which reads exactly like a live session.

    A `diagnosis` explains the silence whenever there is silence to explain --
    nothing new to read, or nothing written since the deploy. Most often the
    game was launched through a different loader than the one this mod deploys
    into, which otherwise fails completely silently. Its `reason` reports what
    was observed, not what caused it: ask the user before acting on it, since
    the likeliest fixes are opposites.
    """
    context, adapter, _ = _context_for_project(Path(project_path))
    log_path = adapter.resolve_log(context)
    if log_path is None:
        # No log at all is the strongest form of "nothing was written here",
        # so it gets the same diagnosis rather than a flat instruction to run
        # the game -- which is actively misleading when the user did run it,
        # just through a different loader.
        raise LogNotFoundError(
            f"No log file found for {context.game_name}.",
            hints=["Run the game at least once with the mod loader installed."],
            details={"diagnosis": diagnose_silence(context, adapter, None)},
        )

    read = read_since(log_path, since_cursor=since_cursor, lines=lines)
    # Both on every read, not just empty ones. A read with no cursor returns
    # the tail of whatever is already there, which can be weeks old, and the
    # text alone cannot be told apart from a live session. One timestamp does
    # not settle it either -- there has to be something to compare it against.
    log_written_at = mtime_of(log_path)
    deployed_at = last_deployed(context)
    response = {
        "success": True,
        "log_path": str(read.path),
        "cursor": read.cursor,
        "content": read.content,
        "log_written_at": _isoformat(log_written_at),
        "deployed_at": _isoformat(deployed_at),
    }

    # Stale content is silence too. Gating on an empty read alone missed the
    # most common shape of this bug entirely: after a redeploy the agent reads
    # afresh, gets a full tail written before the build it just made, and has
    # nothing to tell it so.
    stale = deployed_at is not None and (
        log_written_at is None or log_written_at < deployed_at
    )
    if not read.content or stale:
        # Still gated. A log written since the deploy proves this loader ran,
        # which settles the question outright -- and this is the one tool an
        # agent polls in a loop, so the profile scan must stay off that path.
        response["diagnosis"] = diagnose_silence(context, adapter, log_path)
    return response


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
