"""Harmony patch-target extraction.

Pure tests: they write C# to a temp directory and read it back. No game
install, no DecompilerServer, no network.

The attribute forms exercised here were taken from `HarmonyLib.HarmonyPatch`'s
own 21 constructor overloads rather than from memory, which is how the
two-string case below was found to be handled wrongly.
"""

from __future__ import annotations

from modwright.patches import extract_harmony_targets


def targets(mod_source, code: str):
    """Extract from a single source file, as (display, line) pairs."""
    return [(t.display, t.line) for t in extract_harmony_targets(mod_source(code))]


class TestAttributeForms:
    def test_typeof_and_method_string(self, mod_source):
        code = """
using HarmonyLib;
class P {
    [HarmonyPatch(typeof(StartOfRound), "StartGame")]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("StartOfRound.StartGame", 4)]

    def test_type_on_class_method_on_member(self, mod_source):
        """The common split form: type declared once, method per handler."""
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch("KillEnemy")]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("EnemyAI.KillEnemy", 5)]

    def test_two_strings_are_type_then_method(self, mod_source):
        """Regression: `HarmonyPatch(string declaringType, string methodName)`.

        Previously the first string was read as the method name, so this
        resolved to a method called `StartOfRound`.
        """
        code = """
using HarmonyLib;
class P {
    [HarmonyPatch("StartOfRound", "StartGame")]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("StartOfRound.StartGame", 4)]

    def test_assembly_qualified_type_string_is_trimmed(self, mod_source):
        code = """
using HarmonyLib;
class P {
    [HarmonyPatch("GameNetcodeStuff.PlayerControllerB, Assembly-CSharp", "KillPlayer")]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [
            ("GameNetcodeStuff.PlayerControllerB.KillPlayer", 4)
        ]

    def test_argument_types_array_is_ignored(self, mod_source):
        """`argumentTypes` selects an overload; it does not name the target."""
        code = """
using HarmonyLib;
class P {
    [HarmonyPatch(typeof(Terminal), "LoadNewNode", new Type[] { typeof(int) })]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("Terminal.LoadNewNode", 4)]

    def test_method_type_is_not_read_as_a_name(self, mod_source):
        """`MethodType.Constructor` is a member kind, not a method name."""
        code = """
using HarmonyLib;
class P {
    [HarmonyPatch(typeof(TimeOfDay), MethodType.Constructor)]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("TimeOfDay", 4)]

    def test_nameof_is_read(self, mod_source):
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch(nameof(EnemyAI.Start))]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("EnemyAI.Start", 5)]

    def test_qualified_and_suffixed_attribute_names(self, mod_source):
        code = """
class P {
    [HarmonyLib.HarmonyPatch(typeof(Terminal), "BeginUsingTerminal")]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("Terminal.BeginUsingTerminal", 3)]


class TestPatchHandlerDetection:
    def test_helper_methods_are_not_targets(self, mod_source):
        """Regression: a class-level type must not turn every method into a
        patch target -- ordinary helpers live in patch classes too."""
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch("KillEnemy")]
    static void Postfix() { }

    static int Helper(int x) { return x; }
    private static void AnotherHelper() { }
}
"""
        assert targets(mod_source, code) == [("EnemyAI.KillEnemy", 5)]

    def test_class_declares_target_handlers_named_by_convention(self, mod_source):
        """Harmony accepts a method named Postfix with no attribute at all."""
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(Terminal), "BeginUsingTerminal")]
class P {
    static void Postfix() { }
    static void NotAHandler() { }
}
"""
        assert targets(mod_source, code) == [("Terminal.BeginUsingTerminal", 5)]

    def test_class_declares_target_handler_marked_by_attribute(self, mod_source):
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(Terminal), "BeginUsingTerminal")]
class P {
    [HarmonyPrefix]
    static void AnyNameAtAll() { }
}
"""
        assert targets(mod_source, code) == [("Terminal.BeginUsingTerminal", 5)]

    def test_bare_patch_attribute_alone_yields_nothing(self, mod_source):
        code = """
using HarmonyLib;
[HarmonyPatch]
class P {
    [HarmonyPostfix]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == []

    def test_non_harmony_attributes_ignored(self, mod_source):
        code = """
class P {
    [Obsolete("x")]
    [Serializable]
    static void NotAPatch() { }
}
"""
        assert targets(mod_source, code) == []


class TestScoping:
    def test_sibling_class_does_not_inherit(self, mod_source):
        """Each class's own attributes apply only within it."""
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class A {
    [HarmonyPatch("KillEnemy")]
    static void Postfix() { }
}
class B {
    [HarmonyPatch(typeof(Terminal), "BeginUsingTerminal")]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [
            ("EnemyAI.KillEnemy", 5),
            ("Terminal.BeginUsingTerminal", 9),
        ]

    def test_nested_class_inherits_from_its_own_parent(self, mod_source):
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class Outer {
    [HarmonyPatch(typeof(Terminal))]
    class Inner {
        [HarmonyPatch("BeginUsingTerminal")]
        static void Postfix() { }
    }
}
"""
        assert targets(mod_source, code) == [("Terminal.BeginUsingTerminal", 7)]

    def test_namespaced_declarations_are_found(self, mod_source):
        code = """
using HarmonyLib;
namespace Deep.Namespace {
    [HarmonyPatch(typeof(EnemyAI))]
    class P {
        [HarmonyPatch("KillEnemy")]
        static void Postfix() { }
    }
}
"""
        assert targets(mod_source, code) == [("EnemyAI.KillEnemy", 6)]


class TestUnresolvable:
    def test_non_literal_argument_is_reported_not_guessed(self, mod_source):
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    [HarmonyPatch(Constants.KillMethod)]
    static void Postfix() { }
}
"""
        extracted = extract_harmony_targets(mod_source(code))
        assert len(extracted) == 1
        assert extracted[0].unresolved_reason is not None
        assert "not a literal" in extracted[0].unresolved_reason
        assert extracted[0].member_name is None


class TestSourceHandling:
    def test_commented_out_patches_are_ignored(self, mod_source):
        """Regression: a regex would match attributes inside comments.

        The real MoneyForKills mod has exactly this -- a commented-out debug
        patch that must not be reported as a live target.
        """
        code = """
using HarmonyLib;
[HarmonyPatch(typeof(EnemyAI))]
class P {
    // [HarmonyPatch("LineCommentedOut")]
    /*
    [HarmonyPatch("BlockCommentedOut")]
    static void Old() { }
    */
    [HarmonyPatch("KillEnemy")]
    static void Postfix() { }
}
"""
        assert targets(mod_source, code) == [("EnemyAI.KillEnemy", 10)]

    def test_build_output_directories_are_skipped(self, mod_source, tmp_path):
        directory = mod_source(
            """
using HarmonyLib;
class P {
    [HarmonyPatch(typeof(EnemyAI), "KillEnemy")]
    static void Postfix() { }
}
"""
        )
        for junk in ("obj", "bin"):
            generated = directory / junk / "Debug"
            generated.mkdir(parents=True)
            (generated / "Generated.cs").write_text(
                '[HarmonyPatch(typeof(Fake), "Generated")] class G { '
                "static void Postfix() { } }",
                encoding="utf-8",
            )

        found = [t.display for t in extract_harmony_targets(directory)]
        assert found == ["EnemyAI.KillEnemy"]

    def test_unparseable_file_does_not_crash(self, mod_source):
        """tree-sitter is error tolerant; a file mid-edit must not abort."""
        code = """
using HarmonyLib;
class P {
    [HarmonyPatch(typeof(EnemyAI), "KillEnemy")]
    static void Postfix() { {{{ unclosed
"""
        assert extract_harmony_targets(mod_source(code)) is not None

    def test_no_sources_yields_no_targets(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert extract_harmony_targets(empty) == []
