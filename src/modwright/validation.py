"""Checking extracted patch targets against the real game assembly.

This is a dry run of the lookup Harmony performs at `PatchAll` time. Harmony
resolves `AccessTools.Method(typeof(X), "Y")` through reflection over the
assembly's metadata; we ask DecompilerServer the same question first, so a
name that would throw at game launch is caught before building.

Names are compared on their last segment. The mod source says
`typeof(PlayerControllerB)` while the assembly says
`GameNetcodeStuff.PlayerControllerB`, and bridging that gap properly would
mean resolving `using` directives -- which is compiler work. Matching the
short name is what the C# compiler effectively arrives at anyway, and the
ambiguous case (two types sharing a short name across namespaces) is reported
rather than silently resolved.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from modwright import decompiler
from modwright.models import GameContext, PatchTarget, ValidatedTarget

#: How many sibling members to offer back when a lookup misses.
_MAX_CANDIDATES = 8

#: Minimum `difflib` ratio for a member to be offered as "did you mean".
#: 0.6 keeps real typos (`KilEnemy` -> `KillEnemy`) while rejecting members
#: that merely share a word (`MurderEnemy` -> `KillEnemy` scores 0.5).
_SIMILARITY_CUTOFF = 0.6


def _short(name: str) -> str:
    """Last segment of a possibly namespace-qualified name."""
    return name.rsplit(".", 1)[-1]


def _alias_for(game_context: GameContext) -> str:
    """A stable DecompilerServer context alias for this game."""
    return re.sub(r"[^a-z0-9]+", "-", game_context.game_name.lower()).strip("-")


async def validate_targets(
    targets: list[PatchTarget], game_context: GameContext
) -> list[ValidatedTarget]:
    """Resolve every target against the game assembly's metadata."""
    if not targets:
        return []

    assert game_context.managed_dir is not None
    assembly = game_context.managed_dir / "Assembly-CSharp.dll"
    alias = _alias_for(game_context)

    results: list[ValidatedTarget] = []
    async with decompiler.connect() as client:
        await client.load_assembly(assembly, alias)

        # One query per distinct target; repeated targets (two patches on the
        # same method) reuse the answer.
        cache: dict[tuple[str | None, str | None], ValidatedTarget] = {}
        for target in targets:
            key = (target.type_name, target.member_name)
            if key not in cache:
                cache[key] = await _resolve(client, target, alias)
            cached = cache[key]
            results.append(
                ValidatedTarget(
                    target=target,
                    exists=cached.exists,
                    candidates=cached.candidates,
                    member_id=cached.member_id,
                )
            )

    return results


async def _resolve(
    client: decompiler.DecompilerClient, target: PatchTarget, alias: str
) -> ValidatedTarget:
    if target.type_name is None:
        return ValidatedTarget(target=target, exists=False)

    query = (
        f"{target.type_name}.{target.member_name}"
        if target.member_name
        else target.type_name
    )
    items = await client.search_symbols(query, alias=alias, limit=50)

    if target.member_name is None:
        return _resolve_type(target, items)
    return await _resolve_member(client, target, items, alias)


def _resolve_type(target: PatchTarget, items: list[dict[str, Any]]) -> ValidatedTarget:
    assert target.type_name is not None
    wanted = _short(target.type_name)

    for item in items:
        if item.get("kind") == "Type" and _short(item.get("fullName", "")) == wanted:
            return ValidatedTarget(
                target=target, exists=True, member_id=item.get("memberId")
            )

    candidates = [
        item["fullName"]
        for item in items
        if item.get("kind") == "Type" and "fullName" in item
    ]
    return ValidatedTarget(
        target=target, exists=False, candidates=candidates[:_MAX_CANDIDATES]
    )


async def _resolve_member(
    client: decompiler.DecompilerClient,
    target: PatchTarget,
    items: list[dict[str, Any]],
    alias: str,
) -> ValidatedTarget:
    assert target.type_name is not None and target.member_name is not None
    wanted_type = _short(target.type_name)
    wanted_member = target.member_name

    for item in items:
        if (
            _short(item.get("declaringType", "")) == wanted_type
            and item.get("name") == wanted_member
        ):
            return ValidatedTarget(
                target=target, exists=True, member_id=item.get("memberId")
            )

    candidates = await _suggest_members(client, target.type_name, wanted_member, alias)
    return ValidatedTarget(target=target, exists=False, candidates=candidates)


async def _suggest_members(
    client: decompiler.DecompilerClient,
    type_name: str,
    wanted_member: str,
    alias: str,
) -> list[str]:
    """Rank the type's real members by similarity to the name that missed.

    A plain search is not enough here. Searching `EnemyAI.KilEnemy` matches
    nothing, and DecompilerServer then falls back to returning the type's
    first members alphabetically -- offering those as "did you mean" is worse
    than offering nothing, since none of them resemble what was asked for.
    So the full member list is fetched and ranked properly, and an empty
    result is returned when genuinely nothing is close.
    """
    type_id = await _find_type_id(client, type_name, alias)
    if type_id is None:
        return []

    members = await client.list_members(type_id)
    # Harmony patches target callable members; suggesting a field for a
    # mistyped method name is noise.
    names = sorted(
        {
            member["name"]
            for member in members
            if member.get("kind") in {"Method", "Constructor", "Property"}
            and "name" in member
        }
    )
    return difflib.get_close_matches(
        wanted_member, names, n=_MAX_CANDIDATES, cutoff=_SIMILARITY_CUTOFF
    )


async def _find_type_id(
    client: decompiler.DecompilerClient, type_name: str, alias: str
) -> str | None:
    wanted = _short(type_name)
    for item in await client.search_symbols(wanted, alias=alias, limit=25):
        if item.get("kind") == "Type" and _short(item.get("fullName", "")) == wanted:
            return item.get("memberId")
    return None
