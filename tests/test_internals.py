"""Unit tests for engine helpers whose edge cases bundled data cannot reach."""

import copy
import re
import threading

import pytest

from profy import ProfanityFilter, filter_text
from profy import core


def _within(seconds, function, *args):
    """Run ``function`` in a daemon thread; a hang fails the test instead of the run."""
    outcome = {}

    def target():
        outcome["value"] = function(*args)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "scan loop did not terminate"
    return outcome["value"]


def _with_extra_expressions(shield, *extra):
    dictionary = copy.copy(shield.dictionary)
    dictionary.sorted_expressions = tuple(extra) + dictionary.sorted_expressions
    shield.dictionary = dictionary
    return shield


def test_scan_loop_never_accepts_zero_length_matches():
    shield = _with_extra_expressions(ProfanityFilter(block=["zz-empty"]), ("", re.compile(r"(?=!)|$")))
    result = _within(30, shield.check, "! shit !")
    assert result.clean == "! **** !"
    assert all(match.length for match in result.matches)


def test_scan_loop_ignores_matches_over_already_masked_text():
    # Masked characters are replaced by \x01, which no generated expression can
    # match; a custom expression that could must still not re-report them.
    shield = _with_extra_expressions(ProfanityFilter(block=["zz-masked"]), ("\x01", re.compile("\x01+")))
    result = _within(30, shield.check, "shit")
    assert result.clean == "****"
    assert [match.base for match in result.matches] == ["shit"]


def test_dictionary_normalize_without_a_span_map():
    german = ProfanityFilter(languages="german").dictionary
    assert german.normalize("Scheiße") == "Sheisse"
    normalized, span_map = german.normalize_with_mapping("äb")
    assert normalized == "aeb"
    assert span_map == [(0, 1), (0, 1), (1, 2)]


def test_substitution_tokens_mirror_blasps_class_and_alternation_split():
    single = core._substitution_token(["s", "5", "\\$"], optional=False)
    assert single.chars == frozenset({"s", "5", "\\", "$"}) and single.multi == ()

    mixed = core._substitution_token(["ss", "s", "\\$", "ss", "sch"], optional=True)
    assert mixed.chars == frozenset({"s", "$"})
    assert mixed.multi == ("sch", "ss")
    assert mixed.optional


def test_custom_substitution_data_is_handled():
    expressions = core._generate_expressions(
        ["ab", "a-b", "xa"],
        separators=["-", "."],
        substitutions={"//": ["?"], "/a/": ["a", "4"], "/b/": ["b", "bb", "\\$"], "/x/": ["x"]},
    )
    assert expressions["ab"].fullmatch("4-$")
    assert expressions["ab"].fullmatch("abbbb")
    assert expressions["a-b"].fullmatch("4-b")
    assert expressions["xa"].fullmatch("xx4")
    assert not expressions["ab"].fullmatch("?b")


def test_multi_character_letters_in_every_position():
    substitutions = {"/sch/": ["sch", "sh"], "/a/": ["a", "*"], "/e/": ["e", "*"], "/k/": ["k", "ck", "*"]}
    expressions = core._generate_expressions(["schake", "kek", "kk"], ["-"], substitutions)
    assert expressions["schake"].fullmatch("sh-*ck*")
    assert expressions["schake"].fullmatch("schaaake")
    assert expressions["kek"].fullmatch("ck*ck")
    assert expressions["kk"].fullmatch("ckck")
    assert expressions["kk"].fullmatch("**")


def test_letters_made_only_of_separators():
    # No real letter can follow, so a separator can never split the run.
    expressions = core._generate_expressions(["xx", "xxa"], ["*", "!"], {"/x/": ["*", "!"], "/a/": ["a"]})
    assert expressions["xx"].fullmatch("*!")
    assert expressions["xxa"].fullmatch("**a")
    assert core._split_run_expression(core._Token(frozenset("*!"), ()), "[!*]", ["*", "!"], frozenset("*!")) == ""


def test_gap_expression_edge_cases():
    assert core._gap_expression([], frozenset({" ", "."})) == ""
    assert core._gap_expression(["-"], frozenset({"."})) == r"(?:[\s\-]){0,3}"
    assert core._gap_expression([], frozenset({"a"})) == r"(?:[\s]|\.(?=\w)){0,3}"


def test_options_and_class_members():
    assert core._options_expression(["a"], []) == "a"
    assert core._options_expression(["a", "b"], []) == "[ab]"
    assert core._options_expression([], ["ss"]) == "(?:ss)"
    assert core._options_expression(["s"], ["ss"]) == "(?:ss|s)"
    assert core._options_expression(["a", "b", "c"], []) == "[a-c]"
    assert core._class_members(["A", "a", "b", "c", "e", "-", ".", "/"]) == [r"\--/", "a-c", "e"]
    assert core._options_expression(["-"], []) == r"[\-]"
    assert core._class_members(["İ"]) == ["İ"]


def test_start_guard_without_shared_characters():
    token = core._Token(frozenset({"q"}), ())
    assert core._start_guard(token, "q", frozenset({"z"})) == r"(?<!q[\s\S])"
    assert core._start_guard(token, "", frozenset({"q"})) == ""


def test_spanning_word_boundary_helper():
    assert core._is_spanning_word_boundary("sh it", "sh itx", 0)
    assert core._is_spanning_word_boundary("sh it", "xsh it", 1)
    assert core._is_spanning_word_boundary("sh it", "xsh itx", 1)
    assert not core._is_spanning_word_boundary("s h i t", "s h i t", 0)
    assert not core._is_spanning_word_boundary("f*ck it", "f*ck its", 0)
    assert core._is_spanning_word_boundary("f*ck it", "f*ck itx", 0)
    assert core._is_spanning_word_boundary("fuck - you", "fuck - yourself", 0)
    assert core._is_spanning_word_boundary("shit, s", "shit, said", 0)
    assert core._is_spanning_word_boundary("t, shit", "it, shit", 1)
    assert not core._is_spanning_word_boundary("sh it", "sh it", 0)
    assert not core._is_spanning_word_boundary("shit", "shit", 0)
    assert filter_text("sh itx").is_clean


@pytest.mark.parametrize(
    "matched, word, base, profanities, protected",
    [
        ("sh1t", "sh1t", "shit", [], False),
        ("hell", "hell9o", "hell", [], False),
        ("hell", "hello", "hell", [], True),
        ("hell", "hell", "hell", [], False),
        ("hell", "hells", "hell", [], False),
        ("hell", "hellfuck", "hell", ["fuck"], False),
        ("hell", "hellfu", "hell", ["fu"], True),
        ("hell", "xyzwvu", "hell", [], True),
        ("cook", "cooks", "cok", [], True),
        ("cooook", "cooook", "cok", [], False),
        ("anall", "anally", "anal", [], False),
        ("ccum", "accumulate", "cum", [], True),
        ("dth", "zerowidth", "deth", ["ero"], True),
        ("tard", "astards", "tard", ["ass"], True),
        ("shit", "xshitfuck", "shit", ["fuck"], False),
        ("fuck", "dumbassfuck", "fuck", ["ass"], False),
        ("cuum", "vacuum", "cum", [], True),
        ("shiit", "shiit", "shit", [], False),
        ("shiit", "shiits", "shit", [], False),
        ("fuuck", "fuuckx", "fuck", [], True),
        ("fuuck", "fuuck", "fuuck", [], False),
        ("fuuck", "fuuck", "fuuck", [], False),
        ("fck", "fcks", "fuck", [], False),
    ],
)
def test_pure_alpha_substring_guard(matched, word, base, profanities, protected):
    assert core._is_pure_alpha_substring(matched, word, base, profanities) is protected


def test_score_is_capped():
    matches = [core.Match("x", "x", core.Severity.EXTREME, 0, 1)] * 5
    assert core._score(matches, "x") == 100
    assert core._score([], "anything") == 0


@pytest.mark.parametrize(
    "source, replacement, expected",
    [("LL", "y", "Y"), ("Ll", "y", "Y"), ("ll", "y", "y"), ("SCH", "sh", "SH"), ("Sch", "sh", "Sh")],
)
def test_case_like(source, replacement, expected):
    assert core._case_like(source, replacement) == expected


@pytest.mark.parametrize(
    "character, invisible",
    [
        ("\u200b", True), ("\u200d", True), ("\u2063", True), ("\u00ad", True), ("\ufeff", True),
        ("\ufe0f", True), ("\ufe00", True), ("\U000e0100", True), ("\U000e0067", True),
        ("a", False), ("\u0301", False), ("\U0001F600", False), ("\u3000", False),
    ],
)
def test_invisible_characters(character, invisible):
    assert core._is_invisible(character) is invisible


def test_strip_invisible_with_mapping():
    stripped, span_map = core._strip_invisible_with_mapping("a\u200bb\u200d")
    assert stripped == "ab"
    assert span_map == [(0, 1), (2, 3)]


def test_longest_needed_run():
    assert core._longest_needed_run(["ass"], {}) == 3
    assert core._longest_needed_run(["booooooobs"], {}) == 7
    assert core._longest_needed_run(["gilipollas"], {"/ll/": ["ll", "y"], "/x/": ["ks"]}) == 4
    assert ProfanityFilter(languages="spanish").dictionary.long_run_pattern == r"(.)\1{4,}"


def test_matches_never_share_original_characters():
    # Defensive invariant: even if two expressions match neighbouring normalized
    # characters that come from one original character, only one match is kept.
    # German normalizes "\u00e4" to "ae": "!a" and "e#" share the original "\u00e4".
    shield = _with_extra_expressions(
        ProfanityFilter(languages="german", block=["zz-overlap"]),
        ("zz-a", re.compile("!a")),
        ("zz-e", re.compile("e#")),
    )
    result = _within(30, shield.check, "!\u00e4#")
    assert [(match.base, match.position, match.length) for match in result.matches] == [("zz-a", 0, 2)]
    assert result.clean == "**#"


def test_shorten_runs_with_mapping():
    text, span_map = core._shorten_runs_with_mapping("xaaaaay", [(i, i + 1) for i in range(7)], r"(.)\1{3,}", 3)
    assert text == "xaaay"
    assert span_map == [(0, 1), (1, 2), (2, 3), (3, 6), (6, 7)]
