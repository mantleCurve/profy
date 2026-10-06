import json
import random
import time

import pytest

from helpers import ROOT
from profy import Match, ProfanityFilter, Severity, ShieldResult, filter_text

SEPARATORS = json.loads((ROOT / "profy" / "data" / "global.json").read_text(encoding="utf-8"))["separators"]


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
    "text",
    ["fuuckin", "fuucking", "shiit", "shiits", "shiiter", "biitch", "puussy", "diick", "cuunt", "twaat", "FUUCK", "f-uuckin"],
)
def test_doubled_rarely_doubled_letters_are_deliberate(english, text):
    # English almost never doubles a, h, i, j, k, q, u, v, w, x or y.
    assert english.check(text).clean == "*" * len(text)


@pytest.mark.parametrize(
    "text",
    [
        "cook", "book", "look", "coffee", "cookie", "good", "skiing", "vacuum", "vacuums", "continuum",
        "angiitis", "Shiite", "Shiites", "taxiing", "bazaar", "zero\u200bwidth", "zerowidth", "herowidth",
    ],
)
def test_words_with_real_double_letters_stay_clean(english, text):
    assert english.check(text).clean == text


@pytest.mark.parametrize(
    "text, clean",
    [
        ("shit, said the man", "****, said the man"),
        ("Shit, sorry", "****, sorry"),
        ("sh!t, said", "****, said"),
        ("fuck, yourself", "****, yourself"),
        ("fuck - yourself", "**** - yourself"),
        ("f*ck yourself", "**** yourself"),
        ("shit ; sorry", "**** ; sorry"),
        ("you are such a butt today", "you are such a **** today"),
        ("butt today", "**** today"),
        # Deliberate separator obfuscation still spans spaces and punctuation.
        ("f u c k", "*******"),
        ("f, u, c, k", "**********"),
        ("f u c king", "**********"),
        ("@ss holes", "*********"),
        ("a$$ hats", "*******s"),
        ("fuck - you", "**********"),
        ("fuck, you", "*********"),
    ],
)
def test_matches_do_not_bleed_into_the_next_word(english, text, clean):
    result = english.check(text)
    assert result.clean == clean
    for match in result.matches:
        assert match.text == text[match.position : match.position + match.length]


INFLECTED = [
    # (word, length of the masked stem): doubled-consonant and plain inflections.
    ("shittier", 5), ("shittiest", 9), ("shittily", 5), ("shittiness", 5), ("shitter", 7), ("shitted", 7),
    ("shitting", 8), ("shits", 5), ("shitier", 4), ("crappier", 5), ("crappiest", 5), ("crappily", 5),
    ("crapped", 5), ("crapping", 5), ("craps", 4), ("fucker", 6), ("fuckers", 7), ("fucked", 6),
    ("bitchier", 5), ("bitchiest", 5), ("bitchily", 5), ("bitchiness", 5), ("bitches", 7),
    ("damned", 4), ("damning", 4), ("pissed", 6), ("cunts", 5),
    # A stem's last letter repeated by the ending stays part of the ending.
    ("naziing", 5),
]


@pytest.mark.parametrize(
    "word",
    ["tardiness", "tardily", "spicily", "spiciness", "spookily", "tested", "tester", "hoarily", "spaciness", "hellier"],
)
def test_y_endings_need_a_y_adjective_in_the_dictionary(english, word):
    # "-ier/-ily/-iness" inflect "tardy", "spicy", "spooky", which are not entries.
    assert english.check(word).clean == word


@pytest.mark.parametrize("word, masked", INFLECTED)
def test_inflected_profanities_are_caught(english, word, masked):
    result = english.check(word)
    assert result.is_offensive, word
    assert result.clean.startswith("*" * masked), (word, result.clean)


@pytest.mark.parametrize(
    "text, clean",
    [
        ("f, u, c, khead", "**************"),
        ("f, u, c, kheads", "**************s"),
        ("f, u, c, ks", "**********s"),
        ("sh, itheads", "**********s"),
        ("sh, ithead", "**********"),
        ("shit, sing", "****, sing"),
        ("shit, said", "****, said"),
    ],
)
def test_separator_obfuscation_with_inflections(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, clean",
    [
        ("hell, Lloyd", "****, Lloyd"),
        ("Hell, llama", "****, llama"),
        ("hell ,Lloyd", "**** ,Lloyd"),
        ("ass, sam", "***, sam"),
        ("ass, Sasha", "***, Sasha"),
        ("shit, tom", "****, tom"),
        ("shit; tina", "****; tina"),
        ("fuck, kim", "****, kim"),
        ("fuck! kkk", "****! ***"),
        ("damn, nancy", "****, nancy"),
    ],
)
def test_profanity_before_a_word_starting_with_its_last_letters(english, text, clean):
    # The match once ran across the phrase break into the next word
    # ("hell, Ll"), was rejected, and the plain "hell" was never retried.
    assert english.check(text).clean == clean


@pytest.mark.parametrize("separator", SEPARATORS)
def test_a_match_running_into_the_next_word_is_retried_before_any_separator(english, separator):
    # "hell - Ll|oyd" ran into the next word and was rejected; the shorter
    # match was only retried before phrase-break punctuation (",", ";", ...).
    for text, clean in [
        (f"hell {separator} Lloyd", f"**** {separator} Lloyd"),
        (f"Hell {separator} llama", f"**** {separator} llama"),
        (f"hell{separator} Lloyd", f"****{separator} Lloyd"),
        (f"ass {separator} sam", f"*** {separator} sam"),
    ]:
        assert english.check(text).clean == clean, text


@pytest.mark.parametrize("separator", SEPARATORS)
def test_a_profanity_joined_to_the_next_word_does_not_bleed_into_it(english, separator):
    # "hell-Lloyd" was masked "*******oyd": the repeated "l" ran through the
    # separator into "Lloyd".
    for word, other in [("hell", "Lloyd"), ("Hell", "llama"), ("ass", "Sasha"), ("damn", "nancy")]:
        text = f"{word}{separator}{other}"
        assert english.check(text).clean == "*" * len(word) + separator + other, text


@pytest.mark.parametrize(
    "text, clean",
    [
        ("shit-tom", "****-tom"),  # "shitt" would read "shit-t" first
        ("shit_tom", "****_tom"),
        ("sh1t-tom", "****-tom"),
        ("fuck-kim", "****-kim"),
        ("hell.Lloyd", "****.Lloyd"),
        ("hell--Lloyd", "****--Lloyd"),
        ("say hell-Lloyd now", "say ****-Lloyd now"),
        # A leading "*" stands for any letter, so with the vowel left out it is
        # no evidence: "*Ll" is not "hll".
        ("*Lloyd", "*Lloyd"),
        ("x *Lloyd", "x *Lloyd"),
        ("hell *Lloyd", "**** *Lloyd"),
        ("*ll", "*ll"),
    ],
)
def test_separator_joined_words_stay_apart(english, text, clean):
    assert english.check(text).clean == clean


SEVERITY_LEVELS = [None, "mild", "moderate", "high", "extreme"]


@pytest.mark.parametrize(
    "text, cleans",
    [
        # A separator-obfuscated word continued by another profanity is one
        # compound; cutting it at the separator ("***-holefuck") left the second
        # profanity protected inside "holefuck", unreported at "high".
        ("ass-holefuck", ["*" * 12] * 3 + ["ass-hole****", "ass-holefuck"]),
        ("ass-holebitch", ["*" * 13] * 3 + ["ass-holebitch"] * 2),
        ("sh-itfuck", ["*" * 9] * 4 + ["sh-itfuck"]),
        ("fu-ckshit", ["*" * 9] * 4 + ["fu-ckshit"]),
        ("cock-suckerass", ["*" * 14] * 3 + ["***********ass", "cock-suckerass"]),
        # Words joined to an ordinary word stay apart at every level.
        ("hell-Lloyd", ["****-Lloyd"] * 2 + ["hell-Lloyd"] * 3),
        ("shit-tom", ["****-tom"] * 4 + ["shit-tom"]),
        ("ass-Sasha", ["***-Sasha"] * 3 + ["ass-Sasha"] * 2),
    ],
)
def test_joined_words_at_every_minimum_severity(text, cleans):
    assert [filter_text(text, minimum_severity=level).clean for level in SEVERITY_LEVELS] == cleans


@pytest.mark.parametrize(
    "text",
    [
        "f-u-c-k", "f_u_c_k", "s-h-i-t", "sh-it", "sh_it", "a*s*s", "a-ss", "as-s", "fu-ck", "f--ck", "f-ck",
        "f*ck", "f**k", "*ss", "*hit", "h*ll", "d*mn", "$h!t", "$h*t", "sh1t", "fck", "sht", "dmn", "hll", "$ht",
        "5ht", "fu-cking", "f-u-c-kyou", "fu-ckhead", "a*s*swad", "shitt-ed", "coo-on", "f*-uck", "f-uuck!ng",
    ],
)
def test_deliberate_obfuscation_inside_a_word_is_still_caught(english, text):
    assert english.check(text).clean == "*" * len(text), text


@pytest.mark.parametrize(
    "text, clean",
    [
        # Symbols inside the obfuscated word mark it as one word.
        ("sh-itzilla", "******illa"),
        ("fu-ckzilla", "*****zilla"),
        ("f-u-c-ks", "*******s"),
        # So does a separator inside one dictionary word, with its letter
        # repeated around it ("cockblocker", "shitspitter", "rapist").
        ("cock*kblocker", "******blocker"),
        ("shit/tspitter", "*************"),
        ("rapis(st", "ra*****t"),
        # Symbols standing for letters are no separator ("lusting").
        ("lust**ge", "*******e"),
    ],
)
def test_obfuscated_words_running_into_more_letters_keep_their_mask(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, options",
    [
        # A stretched final run held the next copy's first letter ("twatt|wat"),
        # finditer() skipped that copy and the guard protected what was left.
        pytest.param("twattwat", {}, id="twat-2"),
        pytest.param("twattwattwat", {}, id="twat-3"),
        pytest.param("twaattwaat", {}, id="twaat-2"),
        pytest.param("twaat" * 4, {}, id="twaat-4"),
        pytest.param("blowjob" * 3, {}, id="blowjob"),
        pytest.param("kinkkink", {}, id="kink"),
        pytest.param("raperraper", {}, id="raper"),
        pytest.param("slutssluts", {}, id="sluts"),
        pytest.param("dickwaddickwad", {}, id="dickwad"),
        pytest.param("¢yberfuc¢yberfuc", {}, id="mixed-run"),
        # A shorter entry took the letters of a pending, supported longer one.
        pytest.param("baastard" * 3, {}, id="baastard-3"),
        pytest.param("baastard" * 2, {}, id="baastard-2"),
        # The entry the match starts with reads the junction ("shit|shit").
        pytest.param("5h1t5h1t", {}, id="shits-leet"),
        pytest.param("\u00aeape\u00aeape", {}, id="raper-leet"),
        pytest.param("k!nkk!nk", {}, id="kinks-leet"),
        # Through a separator into the next copy.
        pytest.param("sluut,sluut", {}, id="sluts-comma"),
        pytest.param("5hit-5hit", {}, id="shits-hyphen"),
        pytest.param("hadji-hadji", {}, id="jihad-hyphen"),
        pytest.param("tush-tush", {}, id="shat-hyphen"),
        pytest.param("skum,skum", {}, id="kums-comma"),
        pytest.param("fuckfreak-fuckfreak", {}, id="freakfuck-hyphen"),
        pytest.param("shit-brain-shit-brain", {}, id="hyphenated-entry"),
        pytest.param("shit-ass,shit-ass", {}, id="hyphenated-entry-comma"),
        pytest.param("\\ick,\\ick", {}, id="leading-substitute"),
        # Phrases glued to each other.
        pytest.param("beef curtainsbeef curtains", {}, id="glued-phrase"),
        pytest.param("date rape" * 3, {}, id="glued-phrase-3"),
        pytest.param("goo giirlgoo giirl", {}, id="glued-stretched-phrase"),
        # An entry containing an earlier, shorter reading of the same letters.
        pytest.param("fuccckd", {}, id="contains-fucck"),
        pytest.param("fagggt", {}, id="contains-fagg"),
        # Multi-letter keys spelled per letter, stretched or in leet.
        pytest.param("5chwuler", {"languages": "german"}, id="german-leet-sch"),
        pytest.param("schhm\u00e4hliches", {"languages": "german"}, id="german-stretched-sch"),
        pytest.param("heuuchlerische", {"languages": "german"}, id="german-stretched-eu"),
        pytest.param("5cheiss5cheiss", {"languages": "german"}, id="german-leet-repeat"),
        pytest.param("cerrrdo", {"languages": "spanish"}, id="spanish-rrr"),
        pytest.param("closclosclos", {"languages": "french"}, id="french-c-for-s"),
        pytest.param("cordescordes", {"languages": "french"}, id="french-plural-repeat"),
    ],
)
def test_repeated_and_stretched_profanities_are_masked_completely(text, options):
    # Every character is masked but the separator between two copies, which
    # is part of neither ("hadji-hadji" -> "*****-*****").
    junction = SEPARATED_COPIES.get(text)
    expected = "*" * len(text) if junction is None else junction.join("*" * len(part) for part in text.split(junction, 1))
    if junction is not None and text.count(junction) > 1:
        half = (len(text) - 1) // 2
        expected = "*" * half + junction + "*" * half
    assert filter_text(text, **options).clean == expected


SEPARATED_COPIES = {
    "sluut,sluut": ",",
    "5hit-5hit": "-",
    "hadji-hadji": "-",
    "tush-tush": "-",
    "skum,skum": ",",
    "fuckfreak-fuckfreak": "-",
    "shit-brain-shit-brain": "-",
    "shit-ass,shit-ass": ",",
    "\\ick,\\ick": ",",
}


@pytest.mark.parametrize(
    "text",
    [
        "cookcook", "vacuumvacuum", "shellshell", "goodgood", "bookkeeper", "hello-world", "assess", "teeth",
        "succeed", "classroom", "Tisch",
    ],
)
def test_repeated_ordinary_words_stay_clean(english, text):
    assert english.check(text).clean == text


def test_an_entry_taking_over_a_match_does_not_change_later_ones():
    # "sluts" gave back its "s" to "slut|slut"; the takeover leaked into how
    # the scan judged every later "sluts" match.
    high = ProfanityFilter(minimum_severity="high")
    assert high.check("sluts").clean == "*****"
    result = high.check("slutslut sluts")
    assert result.clean == "slutslut *****"
    assert [(match.base, match.severity) for match in result.matches] == [("sluts", Severity.HIGH)]
    result = filter_text("slutslut sluts")
    assert result.clean == "******** *****"
    assert [(match.base, match.position) for match in result.matches] == [("slut", 0), ("slut", 4), ("sluts", 9)]
    # An unrelated word after it is judged on its own.
    assert filter_text("slutslut slutsmith").clean == "******** slutsmith"
    assert filter_text("slutsmith").clean == "slutsmith"


def test_a_containing_match_keeps_the_more_severe_report():
    # "whooore" reads as "w|hooore" (high) and as "whooore" (moderate): the
    # more severe reading stays, so minimum_severity="high" still masks it.
    assert filter_text("whooore").clean == "w******"
    assert filter_text("whooore", minimum_severity="high").clean == "w******"
    # A withdrawn match below minimum_severity was never reported.
    assert filter_text("fuccckd", minimum_severity="extreme").clean == "fuccckd"


# --- every bundled entry, repeated ---------------------------------------------------

RARELY_DOUBLED = set("ahijkquvwxy")


def _stretched_variants(word, substitutions):
    # The entry; one interior letter tripled; a rarely doubled letter doubled;
    # one letter replaced by its first non-letter substitute. (Doubling an
    # ordinary letter makes a different word by design: "cook", "good".)
    variants = {"plain": word}
    interior = [index for index in range(1, len(word) - 1) if word[index].isalpha()]
    if interior:
        index = interior[len(interior) // 2]
        variants["triple"] = word[: index + 1] + word[index] * 2 + word[index + 1 :]
    rare = [index for index in interior if word[index] in RARELY_DOUBLED]
    if rare:
        variants["rare"] = word[: rare[0] + 1] + word[rare[0]] + word[rare[0] + 1 :]
    for index, character in enumerate(word):
        options = [option for option in substitutions.get(character, []) if len(option) == 1 and not option.isalpha() and option != "*"]
        if options:
            variants["leet"] = word[:index] + options[0] + word[index + 1 :]
            break
    return variants


# Known failures of the sample below (each copy is checked to be fully masked):
# a different entry spelled across the junction of two copies, or inside a
# copy that holds an earlier, more severe reading. The test fails on any other.
KNOWN_REPEAT_FAILURES = {
    ("english", "big breasts", "triple"),
    ("english", "cum licker", "triple"),
    ("english", "d1ck", "leet"),
    ("english", "dumb ass", "leet"),
    ("english", "fugly", "leet"),
    ("english", "jizzd", "leet"),
    ("french", "chapons", "rare"),
    ("french", "clans", "rare"),
    ("french", "computers", "rare"),
    ("french", "cornichons", "rare"),
    ("french", "courriels", "rare"),
    ("german", "eingeaescherte", "plain"),
    ("german", "eingeaescherte", "rare"),
    ("german", "eingeaescherte", "triple"),
}


@pytest.mark.parametrize("language, count", [("english", 60), ("german", 30), ("french", 30), ("spanish", 30)])
def test_every_entry_repeated_is_masked_completely(language, count):
    # A seeded sample of the bundled entries (not false positives), each as
    # itself and stretched, repeated with no separator and with " ", "-", ",":
    # every character of every copy must be masked. The whole dictionary is
    # swept the same way outside the test suite.
    data = json.loads((ROOT / "profy" / "data" / "languages" / f"{language}.json").read_text(encoding="utf-8"))
    global_data = json.loads((ROOT / "profy" / "data" / "global.json").read_text(encoding="utf-8"))
    false_positives = {word.lower() for word in global_data["false_positives"] + data.get("false_positives", [])}
    substitutions = {key.strip("/"): list(options) for key, options in global_data["substitutions"].items()}
    for key, options in data.get("substitutions", {}).items():
        substitutions.setdefault(key.strip("/"), []).extend(options)
    words = sorted({word for word in data["profanities"] if word.lower() not in false_positives})
    shield = ProfanityFilter(languages=language)
    failures = set()
    for word in random.Random(f"profy-{language}").sample(words, count):
        for kind, variant in _stretched_variants(word, substitutions).items():
            for separator, times in [("", 1), ("", 2), ("", 3), (" ", 2), ("-", 2), (",", 2)]:
                text = separator.join([variant] * times)
                clean = shield.check(text).clean
                step = len(variant) + len(separator)
                if any(clean[copy * step : copy * step + len(variant)] != "*" * len(variant) for copy in range(times)):
                    failures.add((language, word, kind))
    assert failures <= KNOWN_REPEAT_FAILURES, sorted(failures - KNOWN_REPEAT_FAILURES)


@pytest.mark.parametrize(
    "text, clean",
    [
        ("biitchfuck", "**********"),
        ("twaatfuck", "*********"),
        ("priickfuck", "**********"),
        ("fuckbiitch", "**********"),
        ("shiitfuck", "*********"),
        ("fuuckshit", "*********"),
        # Clean-word protections still hold.
        ("cookfuck", "cookfuck"),
        ("vacuumfuck", "vacuumfuck"),
        ("xbiitchfuck", "xbiitchfuck"),
    ],
)
def test_rare_doubles_inside_compounds(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, clean",
    [
        # Neighbours that only qualify together are resolved as a chain.
        ("biitchbiitch", "************"),
        ("biitchbiitchfuck", "****************"),
        ("shiitbiitch", "***********"),
        ("fuckbiitchbiitch", "****************"),
        ("biitch" * 40, "*" * 240),
        # A chain that does not reach a word edge stays protected.
        ("xbiitchbiitch", "xbiitchbiitch"),
        ("biitchbiitchx", "biitchbiitchx"),
        ("x" + "biitch" * 40, "x" + "biitch" * 40),
        ("vacuumvacuum", "vacuumvacuum"),
        ("cookcook", "cookcook"),
    ],
)
def test_chains_of_rare_double_candidates(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, clean",
    [
        # A candidate that could lean on either side must not be judged by the
        # side that has no neighbour ("bitchh" at the start of the word).
        ("bitchhbiitch", "************"),
        ("biitchbitchh", "************"),
        ("damnnbiitch", "***********"),
        ("biitchdamnn", "***********"),
        ("bitchhbiitchhfuck", "*****************"),
        ("fuckbitchhbiitch", "****************"),
        ("biitchbitchhbiitch", "******************"),
        ("bitchhbiitchbitchh", "******************"),
        ("bitchh shiit biitch", "****** ***** ******"),
        # Still protected: the chain does not reach the word's end, or a
        # rare double inside an ordinary word.
        ("bitchhbiitchx", "bitchhbiitchx"),
        ("bitchhx", "bitchhx"),
        ("xbiitchbiitch", "xbiitchbiitch"),
        ("vacuumvacuum", "vacuumvacuum"),
        ("cookbiitch", "cookbiitch"),
    ],
)
def test_chains_with_either_neighbour(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, clean",
    [
        # Consonant-only abbreviations chained across the whole word.
        ("shtdck", "******"),
        ("ccksck", "******"),
        ("shtcnt", "******"),
        # Inside a longer word they are ordinary letters of it.
        ("sangreeroot", "sangreeroot"),
        ("dckmngr", "dckmngr"),
    ],
)
def test_chains_of_abbreviations(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize("text", ["xsuspekty", "xsuspektery", "xpoppty", "ppoppt"])
def test_two_letter_entries_make_no_chain(text):
    # German "zu" and "po" are entries, but like compound parts a chain needs
    # words of three or more letters ("su|spek" inside "xsuspekty").
    assert filter_text(text, languages="german").clean == text


@pytest.mark.parametrize(
    "text, clean",
    [
        ("shitxshithell", "shitx********"),
        ("hellxhellshit", "hellx********"),
        ("shitshit", "********"),
        ("fuckfuckfuck", "************"),
        ("hellohell", "hellohell"),
        ("shellshell", "shellshell"),
    ],
)
def test_compound_guard_judges_each_occurrence(english, text, clean):
    assert english.check(text).clean == clean


@pytest.mark.parametrize(
    "text, options, clean",
    [
        ("coo-on", {}, "******"),
        ("coo*on", {}, "******"),
        ("coo--on", {}, "*******"),
        ("cooo-on", {}, "*******"),
        ("coo-n", {}, "*****"),
        ("koo oon", {}, "*******"),
        ("a-ss", {}, "****"),
        ("as-s", {}, "****"),
        ("shitt-ed", {}, "********"),
        ("pimm-mel", {"languages": "german"}, "********"),
        ("pimm*mel", {"languages": "german"}, "********"),
        ("cell*lule", {"languages": "french"}, "*********"),
    ],
)
def test_repeated_letters_split_by_separators(text, options, clean):
    assert filter_text(text, **options).clean == clean


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


# Characters no expression tells apart from "u" in English: its case variants
# and accented forms ("@" and "*" also stand for other letters).
U_VARIANTS = "uU\u00fc\u00dc\u00fb\u00db\u00f9\u00d9\u00fa\u00da\u016b\u016a\u00b5"


def test_runs_of_mixed_letter_variants_of_any_length_are_caught(english):
    # Only identical characters used to be shortened, so a mixed run longer
    # than the 64 characters a letter may match ("uU" * 33) went undetected.
    rng = random.Random(20261006)
    for length in range(1, 501):
        text = "f" + "".join(rng.choice(U_VARIANTS) for _ in range(length)) + "ck"
        assert english.check(text).clean == "*" * len(text), text


@pytest.mark.parametrize(
    "text, options",
    [
        pytest.param("f" + "uU" * 33 + "ck", {}, id="uU"),
        pytest.param("f" + "u\u00fc" * 33 + "ck", {}, id="u-umlaut"),
        pytest.param("sh" + "iI" * 100 + "t", {}, id="iI"),
        pytest.param("b" + "oO" * 100 + "bs", {}, id="oO"),
        pytest.param("f" + "uU" * 250 + "ck", {"all_languages": True}, id="uU-all"),
        pytest.param("f" + "u\u00fc" * 250 + "ck", {"all_languages": True}, id="u-umlaut-all"),
        pytest.param("p" + "u\u00faU\u00da" * 50 + "ta", {"languages": "spanish"}, id="spanish"),
        pytest.param("m" + "e\u00e9E\u00c9" * 50 + "rde", {"languages": "french"}, id="french"),
    ],
)
def test_alternating_case_and_accent_runs_are_caught(text, options):
    assert filter_text(text, **options).clean == "*" * len(text)


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
