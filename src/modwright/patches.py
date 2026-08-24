"""Extracting patch targets from the MOD's own C# source.

Scope note, because it is easy to get backwards: tree-sitter is pointed at the
*mod's* source only. It never inspects the game -- that is DecompilerServer's
job. All this does is read what the mod claims to patch, so those claims can
then be checked against the real assembly.

Why a parser rather than a regex: real attributes contain generic arguments
with commas and nested brackets, wrap across lines, and sit next to comments
and string literals that a pattern would happily match inside. tree-sitter also
keeps working on a file that is mid-edit and does not compile.

Harmony merges attributes declared on the containing class with those on each
method, so the same must happen here -- the common layout puts the type on the
class and only the method name on each member:

    [HarmonyPatch(typeof(EnemyAI))]
    internal class Patches {
        [HarmonyPatch("KillEnemy")]
        static void Postfix() { }
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter_c_sharp
from tree_sitter import Language, Node, Parser

from modwright.models import PatchTarget

_LANGUAGE = Language(tree_sitter_c_sharp.language())

#: Attribute usage may or may not include the conventional suffix, and may be
#: namespace-qualified.
_PATCH_ATTRIBUTE_NAMES = frozenset({"HarmonyPatch", "HarmonyPatchAttribute"})

#: Attributes marking a method as the body of a patch. A method carrying one
#: of these is a handler even with no `[HarmonyPatch]` of its own, because the
#: containing class supplied the target.
_PATCH_ROLE_ATTRIBUTES = frozenset(
    {
        "HarmonyPrefix",
        "HarmonyPostfix",
        "HarmonyTranspiler",
        "HarmonyFinalizer",
        "HarmonyReversePatch",
    }
)

#: Harmony also accepts these names by convention, with no attribute at all.
_PATCH_ROLE_NAMES = frozenset({"Prefix", "Postfix", "Transpiler", "Finalizer"})

#: Declarations that can carry Harmony patches and contain patch methods.
_TYPE_DECLARATIONS = frozenset({"class_declaration", "struct_declaration"})


@dataclass
class _PatchSpec:
    """The type/method a set of HarmonyPatch attributes names, if readable."""

    type_name: str | None = None
    method_name: str | None = None
    unresolved: str | None = None
    #: Whether this declaration carried any Harmony attribute at all. Used to
    #: tell a patch handler from an ordinary helper method sitting in the same
    #: class, which must not be reported as a target.
    is_patch_role: bool = False

    def merged_with(self, fallback: "_PatchSpec") -> "_PatchSpec":
        """Layer this spec over one from an enclosing declaration.

        `is_patch_role` deliberately does not inherit: the enclosing class
        being a patch class says nothing about whether a given method inside
        it is a patch handler.
        """
        return _PatchSpec(
            type_name=self.type_name or fallback.type_name,
            method_name=self.method_name or fallback.method_name,
            unresolved=self.unresolved or fallback.unresolved,
            is_patch_role=self.is_patch_role,
        )

    @property
    def is_empty(self) -> bool:
        return not (self.type_name or self.method_name or self.unresolved)


def extract_harmony_targets(mod_source_dir: Path) -> list[PatchTarget]:
    """Parse Harmony attributes out of every `.cs` file under a directory.

    Shared by every Harmony-based adapter (BepInEx, MelonLoader, and the
    native-loader games that pull Harmony in as a plain dependency).
    """
    parser = Parser(_LANGUAGE)
    targets: list[PatchTarget] = []

    for source_file in sorted(mod_source_dir.rglob("*.cs")):
        # Skip build output; it contains generated sources that are not the
        # author's and would double-report every target.
        if any(part in {"bin", "obj"} for part in source_file.parts):
            continue
        source = source_file.read_bytes()
        tree = parser.parse(source)
        _collect(tree.root_node, source, source_file, _PatchSpec(), targets)

    return targets


def _collect(
    node: Node,
    source: bytes,
    source_file: Path,
    inherited: _PatchSpec,
    out: list[PatchTarget],
) -> None:
    """Walk declarations, carrying enclosing-type attributes downward."""
    for child in node.named_children:
        if child.type in _TYPE_DECLARATIONS:
            own = _read_patch_attributes(child, source)
            scope = own.merged_with(inherited)
            body = child.child_by_field_name("body")
            if body is not None:
                _collect(body, source, source_file, scope, out)
            continue

        if child.type == "method_declaration":
            own = _read_patch_attributes(child, source)
            if not own.is_patch_role and _method_name(child, source) in _PATCH_ROLE_NAMES:
                # Harmony's naming convention: a method called Postfix inside a
                # patch class is a handler even with no attribute.
                own.is_patch_role = True
            spec = own.merged_with(inherited)
            if spec.is_patch_role and not spec.is_empty:
                out.append(
                    PatchTarget(
                        type_name=spec.type_name,
                        member_name=spec.method_name,
                        source_file=source_file,
                        line=child.start_point[0] + 1,
                        unresolved_reason=spec.unresolved,
                    )
                )
            continue

        # Namespaces, top-level declaration lists, etc.
        _collect(child, source, source_file, inherited, out)


def _read_patch_attributes(declaration: Node, source: bytes) -> _PatchSpec:
    """Read the HarmonyPatch attributes attached directly to one declaration."""
    spec = _PatchSpec()

    for attribute_list in declaration.named_children:
        if attribute_list.type != "attribute_list":
            continue
        for attribute in attribute_list.named_children:
            if attribute.type != "attribute":
                continue
            name = _text(attribute.named_children[0], source).rsplit(".", 1)[-1]
            bare = name.removesuffix("Attribute")
            if bare in _PATCH_ROLE_ATTRIBUTES:
                spec.is_patch_role = True
                continue
            if name not in _PATCH_ATTRIBUTE_NAMES:
                continue
            spec.is_patch_role = True
            _read_arguments(attribute, source, spec)

    return spec


def _method_name(declaration: Node, source: bytes) -> str:
    name = declaration.child_by_field_name("name")
    return _text(name, source) if name is not None else ""


def _read_arguments(attribute: Node, source: bytes, spec: _PatchSpec) -> None:
    """Pull the type and method name out of one attribute's arguments.

    Several attributes may contribute to the same target, so values are only
    filled in where still missing -- `[HarmonyPatch(typeof(X))]` followed by
    `[HarmonyPatch("Y")]` has to accumulate rather than overwrite.
    """
    # The argument list is an unnamed child in this grammar, so it has to be
    # found by type rather than by field name.
    argument_list = next(
        (c for c in attribute.named_children if c.type == "attribute_argument_list"),
        None,
    )
    if argument_list is None:
        return  # bare `[HarmonyPatch]`; a marker carrying nothing

    typeof_name: str | None = None
    strings: list[str] = []
    unresolved: str | None = None

    for argument in argument_list.named_children:
        if not argument.named_children:
            continue
        value = argument.named_children[0]

        if value.type == "typeof_expression":
            if value.named_children:
                typeof_name = _text(value.named_children[0], source)

        elif value.type in {"string_literal", "verbatim_string_literal"}:
            strings.append(_string_value(value, source))

        elif value.type == "invocation_expression":
            # `nameof(Type.Member)` -- already compile-checked, but read it so
            # the target is reported accurately rather than as unresolved.
            text = _text(value, source)
            if text.startswith("nameof"):
                strings.append(text.rstrip(")").rsplit(".", 1)[-1].strip())

        elif value.type == "member_access_expression":
            text = _text(value, source)
            # `MethodType.Getter` and friends select which member kind is
            # targeted; they are not a name and must not be read as one.
            if not text.startswith("MethodType."):
                unresolved = (
                    f"argument {text!r} is not a literal, so it cannot be "
                    "checked without compiling"
                )

        elif value.type == "identifier":
            unresolved = (
                f"argument {_text(value, source)!r} is not a literal, so it "
                "cannot be checked without compiling"
            )

        # Anything else (array_creation_expression for argumentTypes, etc.)
        # narrows an overload rather than naming the target, so it is ignored.

    # Harmony's overloads decide what the strings mean. Confirmed against
    # HarmonyLib.HarmonyPatch's own constructors:
    #     HarmonyPatch(string methodName)
    #     HarmonyPatch(Type declaringType, string methodName)
    #     HarmonyPatch(string assemblyQualifiedDeclaringType, string methodName)
    # So a lone string is a method name, but two strings are type-then-method.
    if typeof_name is not None and spec.type_name is None:
        spec.type_name = typeof_name

    if len(strings) >= 2:
        if spec.type_name is None:
            # Assembly-qualified names carry ", AssemblyName" that is not part
            # of the type's own name.
            spec.type_name = strings[0].split(",", 1)[0].strip()
        if spec.method_name is None:
            spec.method_name = strings[1]
    elif len(strings) == 1 and spec.method_name is None:
        spec.method_name = strings[0]

    if unresolved is not None and spec.unresolved is None:
        spec.unresolved = unresolved


def _string_value(node: Node, source: bytes) -> str:
    """Content of a string literal, without its surrounding quotes."""
    for child in node.named_children:
        if child.type in {"string_literal_content", "verbatim_string_literal_content"}:
            return _text(child, source)
    return _text(node, source).strip('"@')


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
