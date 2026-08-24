"""Thin client for DecompilerServer, which owns all game-assembly inspection.

ModWright deliberately does not decompile or index anything itself. It asks
DecompilerServer, an existing MCP server, and spends its own effort on the
lifecycle around that.

Tool and parameter names below were confirmed against DecompilerServer 1.3.8
by driving it directly over stdio. Notable shapes:

* Tools return `content[0].text` holding a JSON envelope, `{"status": "ok",
  "data": {...}}` -- the payload needs a second decode.
* `search_symbols` takes `query`, while member-scoped follow-ups take
  `memberId`. Passing the wrong name fails at argument binding, not with a
  useful hint, so the wrappers here fix the names in one place.
* Member IDs look like `<mvid>:<token>:<kind>` and already identify their
  assembly, so follow-up calls need no alias.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import Client, StdioServerParameters, stdio_client

from modwright.errors import DecompilerUnavailableError

#: Overridable so a user who installed a release build elsewhere is not forced
#: to match this repo's sibling-checkout layout.
DECOMPILER_EXE_ENV = "MODWRIGHT_DECOMPILER_PATH"


def _executable() -> str:
    path = os.environ.get(DECOMPILER_EXE_ENV)
    if not path:
        raise DecompilerUnavailableError(
            "DecompilerServer executable not configured.",
            hints=[
                f"Set {DECOMPILER_EXE_ENV} to the DecompilerServer executable.",
                "Build it with: dotnet build DecompilerServer.sln -c Release",
            ],
        )
    return path


@asynccontextmanager
async def connect() -> AsyncIterator["DecompilerClient"]:
    """Open a session against DecompilerServer for the duration of a call."""
    params = StdioServerParameters(command=_executable(), args=[])
    try:
        # `stdio_client(...)` is itself the transport Client expects: an async
        # context manager yielding the (read, write) stream pair. It is
        # single-use, so it is built fresh per connection rather than cached.
        async with Client(stdio_client(params)) as client:
            yield DecompilerClient(client)
    except DecompilerUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced with a stable code
        raise DecompilerUnavailableError(
            f"Could not start or talk to DecompilerServer: {exc}"
        ) from exc


class DecompilerClient:
    """Typed-ish wrapper over the handful of tools ModWright actually needs."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._client.call_tool(tool, arguments)
        if result.is_error:
            raise DecompilerUnavailableError(
                f"DecompilerServer tool {tool!r} failed: {_first_text(result)}"
            )
        envelope = json.loads(_first_text(result))
        if envelope.get("status") != "ok":
            raise DecompilerUnavailableError(
                f"DecompilerServer tool {tool!r} returned: {envelope}"
            )
        return envelope.get("data", {})

    async def load_assembly(self, assembly_path: Path, alias: str) -> dict[str, Any]:
        return await self._call(
            "load_assembly",
            {"assemblyPath": str(assembly_path), "contextAlias": alias},
        )

    async def search_symbols(
        self, query: str, alias: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        data = await self._call(
            "search_symbols",
            {"query": query, "limit": limit, "contextAlias": alias},
        )
        return data.get("items", [])

    async def get_decompiled_source(self, member_id: str) -> dict[str, Any]:
        return await self._call("get_decompiled_source", {"memberId": member_id})


def _first_text(result: Any) -> str:
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return ""
