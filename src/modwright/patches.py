"""Extracting patch targets from the MOD's own C# source.

Scope note, because it is easy to get backwards: tree-sitter is pointed at the
*mod's* source only. It never inspects the game -- that is DecompilerServer's
job. All this does is read what the mod claims to patch, syntactically, so
those claims can then be checked against the real assembly.

Intended implementation: parse each `.cs` file with `tree-sitter-c-sharp` and
query for attribute lists, pulling `[HarmonyPatch(typeof(X), "Y")]` and its
variants (`nameof(...)`, class-level attributes combined with method-level
ones, `MethodType` arguments). A syntactic read cannot resolve `using` aliases
or generics, so extracted names are best-effort claims, not resolved symbols.
"""

from __future__ import annotations

from pathlib import Path

from modwright.errors import AdapterStepNotImplementedError
from modwright.models import PatchTarget


def extract_harmony_targets(mod_source_dir: Path) -> list[PatchTarget]:
    """Parse Harmony attributes out of every `.cs` file under a directory.

    Shared by every Harmony-based adapter (BepInEx, MelonLoader, and the
    native-loader games that pull Harmony in as a plain dependency).
    """
    raise AdapterStepNotImplementedError(
        "Harmony patch-target extraction is not implemented yet."
    )
