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
from modwright.errors import LogNotFoundError, ModwrightError
from modwright.logs import read_since
from modwright.models import GameContext
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
    config = ProjectConfig.load(project_path)
    adapter = get_adapter(config.framework_id)
    context = detect_framework(config.install_root)
    return context, adapter, config


@mcp.tool()
@_tool
def detect_game(install_root: str) -> dict[str, Any]:
    """Identify which modding framework a game install uses.

    Call this before scaffolding to confirm the game is supported. Refuses
    clearly for IL2CPP games, whose real logic cannot be decompiled at all.
    """
    context = detect_framework(install_root)
    adapter = get_adapter(context.framework_id)
    return {
        "success": True,
        "game_name": context.game_name,
        "framework_id": context.framework_id,
        "framework": adapter.display_name,
        "install_root": str(context.install_root),
        "managed_dir": str(context.managed_dir) if context.managed_dir else None,
        "mods_dir": str(context.mods_dir) if context.mods_dir else None,
    }


@mcp.tool()
@_tool
def scaffold_mod_project(
    install_root: str, project_path: str, mod_name: str
) -> dict[str, Any]:
    """Create a buildable mod project targeting a specific game install.

    References to the game's own assemblies are resolved automatically from the
    detected install, which is the step framework templates leave manual.
    """
    context = detect_framework(install_root)
    adapter = get_adapter(context.framework_id)
    project = Path(project_path)

    written = adapter.scaffold(project, context, mod_name)
    config = ProjectConfig(
        mod_name=mod_name,
        framework_id=context.framework_id,
        install_root=str(context.install_root),
        game_name=context.game_name,
    )
    written.append(config.save(project))

    return {
        "success": True,
        "project_path": str(project),
        "framework": adapter.display_name,
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
def deploy_mod(project_path: str) -> dict[str, Any]:
    """Build the mod and place it where the game loads mods from."""
    context, adapter, _ = _context_for_project(Path(project_path))
    outcome = adapter.build(Path(project_path))
    deployed = adapter.deploy(outcome, context)
    return {
        "success": True,
        "destination": str(deployed.destination),
        "copied": deployed.copied,
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
