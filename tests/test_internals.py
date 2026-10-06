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
    assert core._split_run_expression(core._Token(frozenset("*!"), ()), "[!*]", ["*", "!"], frozenset("*!"), []) == ""


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
    assert not core._is_spanning_word_boundary("f, u, c, k", "f, u, c, ks", 0)
    assert not core._is_spanning_word_boundary("sh, ithead", "sh, itheads", 0)
    assert core._is_spanning_word_boundary("shit, s", "shit, sing", 0)
    assert not core._is_spanning_word_boundary("sh it", "sh it", 0)
    assert not core._is_spanning_word_boundary("shit", "shit", 0)
    assert filter_text("sh itx").is_clean


@pytest.mark.parametrize(
    "matched, before, after, base, parts, protected",
    [
        ("sh1t", "", "", "shit", [], False),
        ("hell", "", "o", "hell", [], True),
        ("hell", "", "", "hell", [], False),
        ("hell", "", "s", "hell", [], False),
        ("hell", "", "fuck", "hell", ["fuck"], False),
        ("hell", "", "fu", "hell", ["fu"], True),
        ("hell", "xyz", "wvu", "hell", [], True),
        ("cook", "", "s", "cok", [], True),
        ("cooook", "", "", "cok", [], False),
        ("anall", "", "y", "anal", [], False),
        ("ccum", "a", "ulate", "cum", [], True),
        ("fuuck", "", "", "fuuck", [], False),
        ("fck", "", "s", "fuck", [], False),
        ("dth", "zerowi", "", "deth", ["ero"], True),
        ("tard", "as", "s", "tard", ["ass"], True),
        ("shit", "x", "fuck", "shit", ["fuck"], False),
        ("fuck", "dumbass", "", "fuck", ["ass"], False),
        ("cuum", "va", "", "cum", [], True),
        ("shiit", "", "", "shit", [], False),
        ("shiit", "", "s", "shit", [], False),
        ("fuuck", "", "x", "fuck", [], True),
        ("shitt", "", "ier", "shit", ["shitty"], False),
        ("crapp", "", "iest", "crap", ["crappy"], False),
        ("crapp", "", "ily", "crap", ["crappy"], False),
        ("shitt", "", "iness", "shit", ["shitty"], False),
        ("shitt", "", "ier", "shit", [], False),
        ("shit", "", "ier", "shit", [], True),
        ("asss", "", "", "ass", [], False),
        ("hell", "", "ier", "hell", [], True),
        ("shit", "", "ier", "shit", ["shity"], False),
        ("tard", "", "iness", "tard", [], True),
        ("shitt", "", "ery", "shit", [], True),
        ("asshole", "", "d", "asshole", [], True),
        ("nazii", "", "ng", "nazi", [], False),
        ("cock", "", "d", "cock", [], True),
        # The guard judges this occurrence, not the first one in the word.
        ("hell", "shitxshit", "", "hell", ["shit"], False),
        ("shit", "", "xshithell", "shit", ["shit", "hell"], True),
    ],
)
def test_pure_alpha_substring_guard(matched, before, after, base, parts, protected):
    assert core._is_pure_alpha_substring(matched, before, after, base, frozenset(parts)) is protected


def test_run_index():
    index = core._RunIndex("ab cd-ef  g", r"\w+")
    assert index.around(0, 1) == (0, 2)
    assert index.around(3, 4) == (3, 5)
    assert index.around(2, 3) == (0, 5)
    assert index.around(10, 11) == (10, 11)
    assert core._RunIndex("", r"\w+").around(0, 0) == (0, 0)


def test_mask_spans():
    assert core._mask_spans("abcdef", [(4, 5), (0, 2)]) == "\x01\x01cd\x01f"
    assert core._mask_spans("abc", []) == "abc"


@pytest.mark.parametrize(
    "token, verdict",
    [
        ("123e4567-e89b-12d3-a456-42661417b00b", True),
        ("-deadbeef1234-", True),
        ("deadbeef", False),
        ("a55a55a55", True),
        ("a55", False),
        ("f*ck", False),
    ],
)
def test_hex_tokens(token, verdict):
    assert core._is_hex_token(token) is verdict


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


def test_phrase_break_retry_is_still_guarded():
    # The shorter retry before the phrase break is rejected too when it still
    # spans words ("x|ab cd" starts inside a word and its tail is plain).
    shield = _with_extra_expressions(ProfanityFilter(block=["zz-retry"]), ("zz-ab", re.compile("ab cd, ef|ab cd")))
    result = _within(30, shield.check, "xab cd, efx")
    assert result.is_clean


def test_resolve_chains():
    masked = lambda index: index in {20}  # noqa: E731
    pending = [
        (0, 3, "a", False, True),  # needs the right neighbour (3, 6)
        (3, 6, "b", True, False),  # needs the left neighbour (0, 3)
        (10, 13, "c", True, False),  # needs a left neighbour, has none
        (13, 16, "d", True, False),  # leans on (10, 13), which falls
        (17, 20, "e", False, True),  # touches a masked character
        (3, 5, "f", True, False),  # overlaps (3, 6), which came first
    ]
    assert core._resolve_chains(pending, masked) == [(0, 3, "a"), (3, 6, "b"), (17, 20, "e")]
    assert core._resolve_chains([(0, 3, "a", True, False)], lambda index: index in {0, 1}) == []


def test_resolve_chains_support_counting():
    never = lambda index: False  # noqa: E731
    pending = [
        (0, 3, "l", False, True),  # two right neighbours; one falls, one stays
        (3, 6, "m", True, False),
        (3, 5, "n", True, True),  # nothing starts at 5
        (7, 10, "a", False, False),
        (8, 10, "b", True, False),  # nothing ends at 8
        (10, 13, "r", True, False),  # two left neighbours; one falls, one stays
        (20, 23, "d", True, True),  # no left neighbour, and its right one falls too
        (23, 26, "f", False, True),
    ]
    assert core._resolve_chains(pending, never) == [(0, 3, "l"), (3, 6, "m"), (7, 10, "a"), (10, 13, "r")]


STRATEGY_TEXTS = [
    "shit " * 300,
    "fuck shit damn hell " * 50,
    "a long and perfectly clean sentence about cooking " * 20 + "shit",
    "shit, said the bitches " * 30,
    "biitchbiitch fuuck shiit cook " * 25,
    "f u c k s h i t " * 40,
    "deadbeef1234 fuck " * 30,
    "\u200bf\u200bu\u200bc\u200bk " * 20,
    "sh!t f*ck @ss a$$ " * 40,
    "hell, Lloyd and ass, sam " * 30,
    "coo-on koo oon pimm-mel " * 30,
    "fuckfuckfuck shitshit " * 30,
]


def _result_key(result):
    return result.clean, [(match.base, match.position, match.length) for match in result.matches]


@pytest.mark.parametrize("languages", ["english", "german"])
def test_confirming_pass_strategies_are_equivalent(monkeypatch, languages):
    full = ProfanityFilter(languages=languages)
    expected = []
    dictionary = copy.copy(full.dictionary)
    dictionary.stops = {}  # every pass scans the whole text
    full.dictionary = dictionary
    expected = [_result_key(full.check(text)) for text in STRATEGY_TEXTS]
    shield = ProfanityFilter(languages=languages)
    monkeypatch.setattr(core, "_SHORTCUT_MINIMUM", 0)
    for spacing in (0, 10**12, core._WINDOW_SPACING):
        monkeypatch.setattr(core, "_WINDOW_SPACING", spacing)
        assert [_result_key(shield.check(text)) for text in STRATEGY_TEXTS] == expected, spacing


def test_windowed_rescan_falls_back_for_empty_matches(monkeypatch):
    # Generated expressions never match empty; anything that does is scanned
    # in full rather than windowed, both for reused and for new matches.
    monkeypatch.setattr(core, "_WINDOW_SPACING", 0)
    monkeypatch.setattr(core, "_SHORTCUT_MINIMUM", 0)
    loose = re.compile("x*")
    text = "ab\x01\x01xx"
    expected = [(match.start(), match.end(), match.group(0)) for match in loose.finditer(text)]
    finder = core._CandidateFinder(text, [(2, 4)], {id(loose): re.compile("[^x]")})
    assert finder.candidates(loose, [(0, 0, "")]) == expected
    assert finder.candidates(loose, []) == expected


def test_short_texts_and_unknown_expressions_are_scanned_in_full():
    finder = core._CandidateFinder("shit", [(0, 4)], {})
    assert finder.mode == "full"
    loose = re.compile("x")
    long_text = "x" * 600
    finder = core._CandidateFinder(long_text, [(0, 1)], {})
    assert finder.mode == "windowed"
    assert finder.candidates(loose, None) == finder.candidates(loose, []) == [
        (index, index + 1, "x") for index in range(600)
    ]
