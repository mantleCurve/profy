import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from helpers import run_isolated
from profy import ProfanityFilter, Severity, check_text, clean_text, filter_text
from profy import core

# --- languages ---------------------------------------------------------------


@pytest.mark.parametrize(
    "languages, expected",
    [
        ("english", ["english"]),
        ("German", ["german"]),
        (" SPANISH ", ["spanish"]),
        (["english", "French", "english"], ["english", "french"]),
        (("german", "english"), ["german", "english"]),
    ],
)
def test_language_names_are_normalized(languages, expected):
    assert core._coerce_languages(languages) == expected


def test_filter_reports_normalized_languages():
    assert ProfanityFilter(languages=[" Spanish", "SPANISH"]).languages == ["spanish"]


@pytest.mark.parametrize("languages", ["englsh", ["english", "klingon"], "../english", "english.json"])
def test_unknown_languages_raise(languages):
    with pytest.raises(ValueError) as error:
        filter_text("fuck", languages=languages)
    message = str(error.value)
    assert "Unknown language" in message
    assert "english, french, german, spanish" in message


@pytest.mark.parametrize("languages", [[], (), ""])
def test_empty_language_selection_raises(languages):
    with pytest.raises(ValueError, match="(At least one language|Unknown language)"):
        ProfanityFilter(languages=languages)


def test_non_string_language_raises():
    with pytest.raises(TypeError, match="language names must be strings"):
        ProfanityFilter(languages=["english", None])


def test_all_languages_uses_every_bundled_dictionary(every_language):
    assert every_language.languages == ["english", "french", "german", "spanish"]
    result = every_language.check("Putain de merde, shit, Scheiße, maldición")
    assert result.clean == "****** de *****, ****, *******, *********"
    assert result.matches[0].language == "english,french,german,spanish"


# --- multi-language substitutions --------------------------------------------

ENGLISH_WORDS = [
    "sock", "socks", "cancer", "pork", "corral", "ice", "pooch", "toned", "sons", "bitten", "cousin",
    "minnow", "petty", "kicker", "cranky", "cocoon", "liner", "occupy", "tricky", "cut", "sow",
]


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"languages": ["english", "german"]},
        {"all_languages": True},
    ],
)
def test_combining_languages_does_not_leak_letter_substitutions(options):
    # German "c" -> "s" (and similar ASCII-letter or multi-letter rules) used to be
    # merged into every language, so "sock" became "****" in a combined filter.
    shield = ProfanityFilter(**options)
    for word in ENGLISH_WORDS:
        assert shield.check(word).clean == word, (options, word)


def test_combined_dictionary_keeps_accent_substitutions(every_language):
    # Single non-ASCII keys (accents, "ß") are still merged, as in Blasp.
    assert every_language.check("Scheiße").clean == "*******"


# --- allow / block -------------------------------------------------------------


def test_blank_block_entries_are_ignored_without_hanging():
    # filter_text("!", block=[""]) used to loop forever on zero-length matches.
    blocks = [[""], ["   "], ["", "\t", "\n"], [" "], "", ["", "ship"]]
    code = (
        "import json; from profy import filter_text; "
        f"print(json.dumps([filter_text('! shit ship', block=b).clean for b in {blocks!r}]))"
    )
    assert run_isolated(code, timeout=120) == ["! **** ship"] * 5 + ["! **** ****"]


def test_blank_allow_entries_are_ignored():
    assert filter_text("shit", allow=["", "  "]).clean == "****"


def test_block_entries_are_stripped_lowercased_and_single_strings_accepted():
    assert filter_text("ship it", block=[" Ship "]).clean == "**** it"
    assert filter_text("ship it", block="SHIP").clean == "**** it"
    assert filter_text("heck", allow="Heck").is_clean


@pytest.mark.parametrize("option", ["allow", "block"])
def test_non_string_entries_raise(option):
    with pytest.raises(TypeError, match=f"{option} entries must be strings"):
        filter_text("x", **{option: ["ok", 3]})


def test_blocking_an_existing_word_keeps_its_severity():
    assert filter_text("coon", minimum_severity="extreme").clean == "****"
    result = filter_text("coon", minimum_severity="extreme", block=["coon"])
    assert result.clean == "****"
    assert result.matches[0].severity is Severity.EXTREME
    assert filter_text("damn", block=["damn"]).matches[0].severity is Severity.MILD


def test_new_block_words_default_to_high():
    result = filter_text("ship", block=["ship"])
    assert result.matches[0].severity is Severity.HIGH
    assert filter_text("ship", block=["ship"], minimum_severity="extreme").is_clean


def test_block_overrides_bundled_false_positives():
    assert filter_text("class").is_clean
    assert filter_text("a class act", block=["class"]).clean == "a ***** act"
    # Only the blocked word itself loses its protection.
    assert filter_text("a classroom", block=["class"]).is_clean
    assert filter_text("bass", block=["class"]).is_clean


def test_allow_wins_over_block():
    assert filter_text("ship", allow=["ship"], block=["ship"]).is_clean
    assert filter_text("shit", allow=["SHIT"], block=["shit"]).is_clean


def test_allow_only_removes_the_listed_word():
    result = filter_text("heck and shit", allow=["heck"])
    assert result.clean == "heck and ****"


# --- severity -------------------------------------------------------------------


@pytest.mark.parametrize(
    "minimum, flagged",
    [
        (None, ["damn", "ass", "shit", "coon"]),
        ("mild", ["damn", "ass", "shit", "coon"]),
        ("moderate", ["ass", "shit", "coon"]),
        (Severity.HIGH, ["shit", "coon"]),
        ("EXTREME", ["coon"]),
    ],
)
def test_minimum_severity_levels(minimum, flagged):
    shield = ProfanityFilter(minimum_severity=minimum)
    assert [word for word in ["damn", "ass", "shit", "coon"] if shield.check(word).is_offensive] == flagged


def test_invalid_severity_raises():
    with pytest.raises(ValueError):
        ProfanityFilter(minimum_severity="severe")


def test_severity_ordering():
    assert Severity.EXTREME.is_at_least(Severity.HIGH)
    assert not Severity.MILD.is_at_least(Severity.MODERATE)
    assert [level.weight for level in Severity] == [5, 15, 30, 50]


# --- masks -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mask, clean",
    [
        ("#", "oh ####"),
        ("█", "oh ████"),
        ("ab", "oh aaaa"),
        ("", "oh ****"),
        (lambda word, length: "[censored]", "oh [censored]"),
        (lambda word, length: word[0] + "-" * (length - 1), "oh s---"),
    ],
)
def test_mask_kinds(mask, clean):
    assert filter_text("oh shit", mask=mask).clean == clean


def test_callback_mask_receives_original_text_and_length():
    calls = []

    def mask(word, length):
        calls.append((word, length))
        return "X"

    assert filter_text("oh Sh\u200bit", mask=mask).clean == "oh X"
    assert calls == [("Sh\u200bit", 5)]


# --- pattern driver ------------------------------------------------------------------


def test_pattern_driver_matches_literal_words_only():
    shield = ProfanityFilter(driver="pattern")
    assert shield.driver == "pattern"
    assert shield.check("This is SHIT, Fuck!").clean == "This is ****, ****!"
    for text in ["fuuuck", "sh!!t", "sh_it", "fuxk", "Scunthorpe", "classic", "shitxyz"]:
        assert shield.check(text).clean == text, text
    assert shield.check("").clean == ""
    assert shield.check(None).original == ""


def test_pattern_driver_prefers_the_longest_overlapping_match():
    result = filter_text("motherfucker", driver="pattern")
    assert result.count == 1
    assert result.matches[0].base == "motherfucker"
    # "ass" also matches inside "dumb ass" but the longer match wins.
    assert [match.base for match in filter_text("dumb ass", driver="pattern").matches] == ["dumb ass"]
    spanish = filter_text("hijo de puta", languages="spanish", driver="pattern")
    assert [match.base for match in spanish.matches] == ["hijo de puta"]


def test_pattern_driver_filters_severity_before_deduplication():
    # "damn" is mild and dropped; the shorter high-severity match survives.
    assert filter_text("damn shit", driver="pattern", minimum_severity="high").clean == "damn ****"
    assert filter_text("damn", driver="pattern", minimum_severity="high").is_clean


def test_pattern_driver_filters_severity_before_deduplicating_overlaps():
    # "hi coon" (high) overlaps "coon" (extreme); filtering after de-duplication
    # would drop the longer match first and lose the extreme one.
    result = filter_text("hi coon", driver="pattern", block=["hi coon"], minimum_severity="extreme")
    assert result.clean == "hi ****"
    assert [match.base for match in result.matches] == ["coon"]


BLOCK_WORDS = [
    "full-length", "cross-site", "all-languages", "niggardliness's", "Ab-Ba", "a--b", "x.y", "e-mail", "o'clock",
    "rock'n'roll", "co-op", "re-enter", "pre-existing", "well-off", "zz top", "two  words", "tic tac toe", "R2-D2",
    "covid19", "404", "1337", "a1b2c3", "!!wow", "wow!!", "@home", "#hashtag", "c++", "c#", "f*ck", "s!h!t",
    "$money$", "100%", "a/b", "(x)", "[tag]", "{brace}", "aaaa", "aaaaaaaaaaaa", "bookkeeper", "mississippi",
    "llama", "ssss-ssss", "M\u00e4dchen", "na\u00efve", "fa\u00e7ade", "\u0395\u03bb\u03bb\u03ac\u03b4\u03b1",
    "\u65e5\u672c", "\U0001F600", "a\U0001F600b", "MiXeD", "UPPER", "\u0141\u00d3D\u0179", "stra\u00dfe",
    "x", "ab", "a b", "a-", "-a", "...", "--", "_under_", "tab\tbed", "*6zy",
]


def _random_block_words(count):
    rng = random.Random(20261006)
    alphabet = string.ascii_letters + string.digits + "-'_.!*@ \u00e4\u00f6\u00fc\u00e9"
    words = ("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14))) for _ in range(count))
    return [word for word in words if word.strip()]


@pytest.mark.parametrize("driver", ["regex", "pattern"])
def test_a_block_word_always_masks_its_own_text(driver):
    for word in BLOCK_WORDS + _random_block_words(300):
        result = filter_text(word, block=[word], driver=driver)
        core = word.strip()
        lead = word[: len(word) - len(word.lstrip())]
        expected = lead + "*" * len(core) + word[len(lead) + len(core) :]
        assert result.original == word
        assert result.clean == expected, (driver, word, result.clean)


def test_block_entries_collapse_whitespace():
    assert filter_text("say two \t words", block=["two   words"]).clean == "say " + "*" * 11
    assert filter_text("say two \t words", block=["two   words"], driver="pattern").clean == "say " + "*" * 11


def test_long_block_words_match():
    # Generated expressions use more than 99 groups for long words; numbered
    # backreferences past \99 would be read as octal escapes.
    for word in ["ab" * 50 + "a", "x" * 300, "abcdefghij" * 20]:
        result = filter_text(f"say {word} now", block=[word])
        assert result.clean == f"say {'*' * len(word)} now"
        assert result.matches[0].base == word


def test_runs_of_a_single_character_block_word_never_overlap():
    poo = "\U0001F4A9"
    result = filter_text(poo * 8 + " hello", block=[poo], mask=lambda word, length: "X")
    assert result.clean.endswith(" hello")
    assert result.original == poo * 8 + " hello"
    spans = [(match.position, match.position + match.length) for match in result.matches]
    assert spans == sorted(spans)
    assert all(end <= start for (_, end), (start, _) in zip(spans, spans[1:]))
    assert spans[0][0] == 0 and spans[-1][1] == 8
    assert result.unique_words == [poo]
    assert result.count == len(result.clean) - len(" hello")
    assert filter_text(poo * 8 + " hello", block=[poo]).clean == "*" * 8 + " hello"
    assert filter_text(poo * 3, block=[poo]).count == 3


def test_pattern_driver_respects_allow_block_and_false_positives():
    assert filter_text("ship", driver="pattern", block=["ship"]).clean == "****"
    assert filter_text("shit", driver="pattern", allow=["shit"]).is_clean
    # A profanity that is also a bundled false positive is skipped, as upstream.
    assert filter_text("class", driver="pattern", block=["ass", "class"]).clean == "*****"
    assert filter_text("hello", driver="pattern", block=["hello"]).clean == "*****"
    assert filter_text("analyst", driver="pattern", block=["analyst"], allow=[]).clean == "*******"


def test_pattern_driver_skips_profanities_listed_as_false_positives(monkeypatch):
    shield = ProfanityFilter(driver="pattern", block=["zork"])
    dictionary = shield.dictionary
    patched = core._Dictionary.__new__(core._Dictionary)
    patched.__dict__.update(dictionary.__dict__)
    patched.false_positives = dictionary.false_positives | {"zork"}
    shield.dictionary = patched
    assert shield.check("zork shit").clean == "zork ****"


def test_pattern_driver_works_through_helpers():
    assert check_text("shit", driver="pattern")
    assert clean_text("fuuuck shit", driver="pattern") == "fuuuck ****"


def test_unknown_driver_raises():
    with pytest.raises(ValueError, match="Unknown driver 'phonetic'"):
        filter_text("x", driver="phonetic")


# --- caching -------------------------------------------------------------------------


def test_filters_with_equal_options_share_one_compiled_dictionary():
    assert ProfanityFilter().dictionary is ProfanityFilter(languages="English").dictionary
    assert ProfanityFilter(block=["a", "b"]).dictionary is ProfanityFilter(block=["B", " a "]).dictionary
    assert ProfanityFilter(block=["zzz"]).dictionary is not ProfanityFilter().dictionary
    assert ProfanityFilter(driver="pattern").dictionary is not ProfanityFilter().dictionary


def test_cache_is_bounded():
    assert core._cached_dictionary.cache_info().maxsize == core._DICTIONARY_CACHE_SIZE
    assert core._compile_profanity.cache_info().maxsize is not None


def test_options_do_not_leak_between_calls():
    assert filter_text("ship", block=["ship"]).is_offensive
    assert filter_text("ship").is_clean
    assert filter_text("shit", allow=["shit"]).is_clean
    assert filter_text("shit").is_offensive
    assert filter_text("shit", mask="#").clean == "####"
    assert filter_text("shit", mask=lambda word, length: "!").clean == "!"
    assert filter_text("shit").clean == "****"
    assert filter_text("damn", minimum_severity="high").is_clean
    assert filter_text("damn").is_offensive


def test_shared_dictionary_is_read_only():
    dictionary = ProfanityFilter().dictionary
    assert isinstance(dictionary.profanities, tuple)
    assert isinstance(dictionary.false_positives, frozenset)
    with pytest.raises(TypeError):
        dictionary.severity_map["shit"] = Severity.MILD


def test_expressions_are_sorted_once_longest_first():
    dictionary = ProfanityFilter().dictionary
    lengths = [len(base) for base, _ in dictionary.sorted_expressions]
    assert lengths == sorted(lengths, reverse=True)
    assert len(dictionary.sorted_expressions) == len(dictionary.expressions)


def test_repeated_helper_calls_are_fast():
    filter_text("warm up the cache")
    started = time.perf_counter()
    for _ in range(200):
        assert check_text("shit")
        assert clean_text("hello there") == "hello there"
    # ~1 ms per call once cached (it was ~1.5 s per call before caching).
    assert time.perf_counter() - started < 10


def test_helpers_are_thread_safe():
    cases = [
        ({}, "oh shit", "oh ****"),
        ({"block": ["ship"]}, "ship it", "**** it"),
        ({"allow": ["shit"]}, "oh shit", "oh shit"),
        ({"mask": "#"}, "oh shit", "oh ####"),
        ({"driver": "pattern"}, "fuuuck shit", "fuuuck ****"),
        ({"minimum_severity": "extreme"}, "oh shit", "oh shit"),
    ]
    barrier = threading.Barrier(8)

    def worker(index):
        barrier.wait()
        for round_number in range(30):
            options, text, expected = cases[(index + round_number) % len(cases)]
            assert filter_text(text, **options).clean == expected

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(worker, index) for index in range(8)]:
            future.result()
