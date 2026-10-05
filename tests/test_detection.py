import time

import pytest

from profy import Match, ProfanityFilter, Severity, ShieldResult, filter_text


@pytest.mark.parametrize(
    "text, clean",
    [
        ("This is shit", "This is ****"),
        ("Hello FUCKING World", "Hello ******* World"),
        ("FuCk", "****"),
        ("what the hell, damn", "what the ****, ****"),
        ("oh shit, bitches", "oh ****, *******"),
    ],
)
def test_english_detection_preserves_surrounding_text_and_case(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "language, text, clean, base",
    [
        ("spanish", "maldición", "*********", "maldición"),
        ("spanish", "MALDICIÓN", "*********", "maldición"),
        ("spanish", "eres una PERRA", "eres una *****", "perra"),
        ("spanish", "hijo de puta", "************", "hijo de puta"),
        ("german", "Das ist scheisse", "Das ist ********", "scheisse"),
        ("german", "Scheiße!", "*******!", "scheisse"),
        ("german", "SCHEISSE", "********", "scheisse"),
        ("german", "Schlampe", "********", "schlampe"),
        ("french", "putain de merde", "****** de *****", "putain"),
        ("french", "ENCULÉ", "******", "enculé"),
        ("french", "Connard", "*******", "connard"),
    ],
)
def test_every_language_detects_and_normalizes(language, text, clean, base):
    result = filter_text(text, languages=language)
    assert result.clean == clean
    assert result.matches[0].base == base
    assert result.matches[0].language == language


@pytest.mark.parametrize("text", ["LLama", "Llama", "llama", "Rrrr", "RR", "carro"])
def test_spanish_normalization_handles_every_case_shape(text):
    # "ll" before a vowel and "rr" are normalized for matching only.
    result = filter_text(text, languages="spanish")
    assert result.original == text
    assert result.is_clean


@pytest.mark.parametrize(
    "text",
    [
        "f-u-c-k", "f.u.c.k", "f_u_c_k", "f u c k", "s h i t", "f--ck", "fu-ck", "f-uuck!ng",
        "sh1t", "$h!t", "b1tch", "@ss", "a$$", "a**", "a*s", "f*ck", "f**k", "sh*t", "f*u*c*k",
        "fuuuuck", "shiiiit", "fuuuuuuuuuuuuuuck", "fuuuck", "fuuuuu**ck", "fck", "sht", "dmn",
        "phuck", "b00bs", "f*-uck",
    ],
)
def test_obfuscations_are_caught(english, text):
    result = english.check(text)
    assert result.is_offensive, text
    assert "*" * len(text) == result.clean or result.clean != text


@pytest.mark.parametrize(
    "text",
    [
        "Scunthorpe", "Penistone", "assignment", "passion", "classroom", "classic", "assume", "bass",
        "compass", "hello", "shell", "cocktail", "analyst", "arsenal", "butterscotch", "therapist",
        "assess", "assessing", "assassin", "assassination",
        "hello9", "9hello", "shell9", "scunthorpe9", "hello9world",
    ],
)
def test_scunthorpe_words_stay_clean(english, text):
    result = english.check(text)
    assert result.is_clean, text
    assert result.clean == text


@pytest.mark.parametrize(
    "text",
    [
        "cook", "cooks", "cookie", "cooked", "cooking", "book", "look", "good", "goods", "annals",
        "accumulate", "assessable", "abhorrent", "anteroom", "ampullary", "Cook", "COOKING",
        "a good book about cooking", "aberrometer",
    ],
)
def test_ordinary_double_letters_do_not_manufacture_profanity(english, text):
    # "cook" only matched "cok" because repeated letters are expanded; ordinary
    # double letters now get the same Scunthorpe protection as substrings.
    assert english.check(text).clean == text


@pytest.mark.parametrize(
    "text, clean",
    [
        ("fuuuck", "******"),
        ("cooook", "******"),
        ("asss", "****"),
        ("anally", "*****y"),
        ("cumm", "****"),
        ("fuckk", "*****"),
        ("f u u c k", "*********"),
    ],
)
def test_stretched_or_inflected_repetitions_are_still_caught(english, text, clean):
    # Three or more identical letters are deliberate stretching; a doubled
    # letter that merely extends a literal profanity is judged without it.
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, options",
    [
        ("f" + "u" * 400 + "ck", {}),
        ("FUUUUUUUUUUUUUUUUUUUUuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuCK", {}),
        ("b" + "o" * 12 + "bs", {}),
        ("shiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiit", {}),
        ("*" * 1000, {}),
        ("gilipollllllas", {"languages": "spanish"}),
        ("Scheissssssssse", {"languages": "german"}),
    ],
)
def test_very_long_letter_runs_are_masked_entirely(text, options):
    # Long runs of one character are shortened for matching only; the mask
    # still covers the whole original run.
    result = filter_text(text, **options)
    assert result.clean == "*" * len(text)


def test_vowel_elision_does_not_flag_unrelated_short_tokens(english):
    for text in ["fc", "ss", "ck", "FC Barcelona", "miss the bus", "a class of students"]:
        assert english.check(text).is_clean, text


@pytest.mark.parametrize(
    "text, clean",
    [
        ("deadbeef1234 fuck", "deadbeef1234 ****"),
        ("id a55a55a55a55 here", "id a55a55a55a55 here"),
        ("123e4567-e89b-12d3-a456-42661417b00b", "123e4567-e89b-12d3-a456-42661417b00b"),
    ],
)
def test_hex_and_uuid_tokens_are_not_flagged(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, clean",
    [
        ("this hit", "this hit"),
        ("is hit", "is hit"),
        ("Assistant hit", "Assistant hit"),
        ("musicals hit", "musicals hit"),
        ("s h i t", "*******"),
    ],
)
def test_matches_spanning_word_boundaries(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize("text", [None, "", "   ", "\n\t", "\u200b", "\u200b\u200d"])
def test_empty_and_blank_input(english, text):
    result = english.check(text)
    expected = text or ""
    assert result.original == expected
    assert result.clean == expected
    assert result.is_clean and result.score == 0 and result.severity is None


def test_very_long_input(english):
    text = "a perfectly ordinary sentence " * 400 + "and then shit"
    started = time.perf_counter()
    result = english.check(text)
    assert time.perf_counter() - started < 10
    assert result.count == 1
    assert result.clean.endswith("and then ****")
    assert result.matches[0].position == len(text) - 4


def test_many_matches_are_all_masked(english):
    text = "fuck " * 200
    result = english.check(text)
    assert result.count == 200
    assert result.clean == "**** " * 200


def test_result_shape_and_helpers():
    result = filter_text("shit and damn and shit")

    assert isinstance(result, ShieldResult)
    assert result.is_offensive and not result.is_clean
    assert result.count == 3
    assert sorted(result.unique_words) == ["damn", "shit"]
    assert result.unique_words == list(dict.fromkeys(match.base for match in result.matches))
    assert result.severity is Severity.HIGH
    assert str(result) == result.clean == "**** and **** and ****"
    data = result.to_dict()
    assert data["original"] == "shit and damn and shit"
    assert data["severity"] == "high"
    assert data["is_offensive"] is True
    assert sorted(word["base"] for word in data["words"]) == ["damn", "shit", "shit"]
    match = min(result.matches, key=lambda item: item.position)
    assert isinstance(match, Match)
    assert match.to_dict() == {
        "text": "shit",
        "base": "shit",
        "severity": "high",
        "position": 0,
        "length": 4,
        "language": "english",
    }
    clean = filter_text("hello").to_dict()
    assert clean["severity"] is None and clean["words"] == [] and clean["score"] == 0


def test_score_bounds_and_growth(english):
    assert english.check("hello there").score == 0
    mild = english.check("damn").score
    high = english.check("shit").score
    extreme = english.check("coon").score
    assert 0 < mild < high < extreme <= 100
    assert english.check("one shit among many many ordinary words here").score < high
    assert english.check("coon " * 30).score == 100


def test_overlapping_profanities_are_masked_once(english):
    result = english.check("motherfucker")
    assert result.count == 1
    assert result.clean == "************"


def test_reusable_filter_matches_one_shot_helper(english):
    for text in ["shit", "f*ck off", "clean words", "Scunthorpe"]:
        assert english.check(text) == filter_text(text)
    assert english.clean("shit") == "****"
    assert ProfanityFilter(languages=["english"]).check("shit") == english.check("shit")
