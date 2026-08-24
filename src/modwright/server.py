"""ModWright MCP server: the mod-authoring lifecycle as agent-callable tools.

Division of labour: DecompilerServer answers "what does the game's code look
like"; ModWright answers "turn that into a mod that builds, deploys, and runs".
Every tool here returns a plain dict with `success`, and on failure a stable
`code` from `modwright.errors.ErrorCode` so an agent can branch without
matching on message text.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Callable

from mcp.server import MCPServer

from modwright.adapters import detect_framework, get_adapter
from modwright.errors import DeployTargetUnsetError, LogNotFoundError, ModwrightError
from modwright.logs import read_since
from modwright.models import GameContext
from modwright.profiles import discover_profiles
from modwright.project_config import ProjectConfig
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


def _require_chosen_target(adapter: Any, config: ProjectConfig) -> None:
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
    _, adapter, _ = _context_for_project(Path(project_path))
    outcome = adapter.build(Path(project_path))
    return {
        "success": True,
        "artifact": str(outcome.artifact) if outcome.artifact else None,
        "deployed_by_build": outcome.deployed_by_build,
        "deploy_required": not outcome.deployed_by_build,
        "log": outcome.log,
    }


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
    config.save(project)

    return {
        "success": True,
        "deploy_root": config.deploy_root,
        "mods_dir": str(context.mods_dir) if context.mods_dir else None,
    }


@mcp.tool()
@_tool
def deploy_mod(project_path: str) -> dict[str, Any]:
    """Build the mod and place it where the game loads mods from.

    That is the project's configured deploy target -- a mod-manager profile
    when one is set, otherwise the game install.
    """
    context, adapter, config = _context_for_project(Path(project_path))
    _require_chosen_target(adapter, config)
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
    """
    context, adapter, _ = _context_for_project(Path(project_path))
    log_path = adapter.resolve_log(context)
    if log_path is None:
        raise LogNotFoundError(
            f"No log file found for {context.game_name}.",
            hints=["Run the game at least once with the mod loader installed."],
        )

    read = read_since(log_path, since_cursor=since_cursor, lines=lines)
    return {
        "success": True,
        "log_path": str(read.path),
        "cursor": read.cursor,
        "content": read.content,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
