"""Adversarial inputs must stay fast for every bundled language.

Blasp's generated expressions backtracked exponentially on runs of characters
shared by several letters and the separators: ``"*" * 22`` took ~29s and
``"*" * 24`` never finished. Each language is exercised in its own interpreter
with a hard timeout, so a regression fails instead of hanging the test run.
"""

import copy
import json
import subprocess
import sys
import time

import pytest

from helpers import ROOT
from profy import ProfanityFilter

# Generous: typical inputs take ~5-40 ms; the regression took seconds to forever.
PER_INPUT_BUDGET = 2.0
PROCESS_TIMEOUT = 300

FILTERS = {
    "english": {},
    "spanish": {"languages": "spanish"},
    "german": {"languages": "german"},
    "french": {"languages": "french"},
    "english+german": {"languages": ["english", "german"]},
    "all_languages": {"all_languages": True},
}

MIXES = [
    "*" * 22,
    "*" * 24,
    "*" * 500,
    "*-" * 150,
    "-*-" * 100,
    "*@" * 150,
    "!|" * 150,
    "* " * 200,
    "*." * 150,
    "f" + "*" * 300 + "k",
    "f*" * 150,
    "fu" * 250,
    "f-u-" * 100,
    "f" + "u" * 400 + "ck",
    "fuuuu" * 80,
    "a" * 500,
    "a" * 250 + "*" * 250,
    "$h!t" * 100,
    "fuck " * 100,
    "sh*t " * 100,
    "\u200b*" * 150,
    "s" * 300 + "x",
]


def _data_characters() -> tuple[list[str], list[str]]:
    data = ROOT / "profy" / "data"
    global_data = json.loads((data / "global.json").read_text(encoding="utf-8"))
    # Whitespace always separates letters (``\s``), so it is not listed in the data.
    characters = set(global_data["separators"]) | {" ", "\t", "\n", "\u00a0", "\u3000"}
    multi = set()
    sources = [global_data] + [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted((data / "languages").glob("*.json"))
    ]
    for source in sources:
        for key, options in source.get("substitutions", {}).items():
            for text in [key.strip("/"), *options]:
                characters.update(text)
                if len(text) > 1:
                    multi.add(text)
    return sorted(characters), sorted(multi)


CHARACTERS, MULTI_OPTIONS = _data_characters()
INPUTS = (
    [character * 300 for character in CHARACTERS]
    + [option * (300 // len(option)) for option in MULTI_OPTIONS]
    + MIXES
)

SCRIPT = """
import json, sys, time
from profy import ProfanityFilter
options, inputs = json.loads(open(sys.argv[1], encoding="utf-8").read())
shield = ProfanityFilter(**options)
timings = []
for text in inputs:
    started = time.perf_counter()
    shield.check(text)
    timings.append(time.perf_counter() - started)
print(json.dumps(timings))
"""


def test_inputs_cover_every_bundled_character():
    assert len(CHARACTERS) > 140
    for character in "*@!|$-._ ":
        assert character in CHARACTERS
    assert "sch" in MULTI_OPTIONS


def test_adversarial_inputs_finish_quickly_in_every_language(tmp_path):
    processes = {}
    for name, options in FILTERS.items():
        payload = tmp_path / f"{name}.json"
        payload.write_text(json.dumps([options, INPUTS]), encoding="utf-8")
        # All interpreters run concurrently; each gets its input from a file.
        processes[name] = subprocess.Popen(
            [sys.executable, "-c", SCRIPT, str(payload)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    try:
        outputs = {name: process.communicate(timeout=PROCESS_TIMEOUT) for name, process in processes.items()}
    finally:
        for process in processes.values():
            process.kill()

    for name, (stdout, stderr) in outputs.items():
        assert processes[name].returncode == 0, stderr
        timings = json.loads(stdout)
        slow = [(repr(text[:12]), len(text), round(seconds, 3)) for text, seconds in zip(INPUTS, timings) if seconds > PER_INPUT_BUDGET]
        assert not slow, f"{name}: {slow}"


@pytest.mark.parametrize("length", [18, 22, 24, 64])
def test_star_runs_are_still_masked(english, length):
    # The fix keeps behaviour: a run of censoring stars is still reported.
    result = english.check("*" * length)
    assert result.is_offensive
    assert result.clean == "*" * length


SCALING_SHAPES = {
    "a55": lambda n: "a55" * n,
    "ab": lambda n: "ab" * n,
    "a1": lambda n: "a1" * n,
    "x": lambda n: "x" * n,
    "f*": lambda n: "f*" * n,
    "hex": lambda n: ("deadbeef0123" * n)[: 3 * n],
    "long word": lambda n: ("thequickbrownfox" * n)[: 3 * n],
    "shit": lambda n: "shit" * n,
    "fuck ": lambda n: "fuck " * n,
    "a55 ": lambda n: "a55 " * n,
    "uuid": lambda n: "123e4567-e89b-12d3-a456-42661417b00b " * (n // 12),
    "word gap repeated": lambda n: "h e l! " * (n // 2),
    "joined words": lambda n: "hell-Lloyd " * (n // 4),
    "elided": lambda n: "*ll fck " * (n // 3),
}


def _best_of_two(shield, text):
    timings = []
    for _ in range(2):
        started = time.perf_counter()
        shield.check(text)
        timings.append(time.perf_counter() - started)
    return min(timings)


@pytest.mark.parametrize("shape", list(SCALING_SHAPES))
def test_scanning_scales_linearly_with_long_tokens(english, shape):
    # Context lookups used to rescan the whole token or word per candidate
    # match: "a55" * 2000 took ~4.5s and "shit" * 4000 ~33s. Quadrupling the
    # input must now roughly quadruple the time (quadratic would be 16x).
    make = SCALING_SHAPES[shape]
    small = _best_of_two(english, make(500))
    large = _best_of_two(english, make(2000))
    assert large < 5.0, (shape, large)
    assert large / max(small, 0.02) < 9, (shape, small, large)


# Quadratic shapes whose linear part dominates at small sizes: measured at
# sizes where the old behaviour took 10-12x the time for 4x the input (linear
# is ~4x).
QUADRATIC_LIMIT = 8


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda n: "h e l" + "!\u00a3" * n + "x", id="symbols"),
        pytest.param(lambda n: "h\te\u3000l" + "!\u00a3" * n + "x", id="mixed-whitespace"),
        pytest.param(lambda n: "h e l-" + "!\u00a3" * n + "x", id="separator"),
    ],
)
def test_rejected_cross_word_matches_retry_in_linear_time(english, make):
    # A rejected cross-word match with a long run of symbols its last letter
    # accepts: every retry rescanned that run ("h e l" + "!\u00a3" * 8000 ~3.2s).
    small = _best_of_two(english, make(2000))
    large = _best_of_two(english, make(8000))
    assert english.check(make(8000)).is_clean
    assert large < 5.0, large
    assert large / max(small, 0.02) < QUADRATIC_LIMIT, (small, large)


@pytest.mark.parametrize("word", ["12", "1212", "deadbeef1"])
def test_repeated_hex_like_block_entries_scale_linearly(word):
    # Every match compared the whole hex-like token with the block word again:
    # block=["12"] on "12" * 64000 took ~2.6s, 12x the time of a quarter of it.
    data = json.loads((ROOT / "profy" / "data" / "languages" / "english.json").read_text(encoding="utf-8"))
    alone = ProfanityFilter(allow=data["profanities"], block=[word])
    small = _best_of_two(alone, word * (32000 // len(word)))
    large = _best_of_two(alone, word * (128000 // len(word)))
    assert alone.check(word * 100).clean == "*" * (100 * len(word))
    assert large < 5.0, large
    assert large / max(small, 0.02) < QUADRATIC_LIMIT, (small, large)


def _distinct_hex_matches(n):
    # One long hex-like token holding n distinct matches of "ab" and "8".
    return "x" + "".join("a" + "".join(("b" if bit == "0" else "8") + "b8" for bit in format(k, "016b")) for k in range(n)) + "x"


def test_distinct_block_matches_in_one_hex_token_scale_linearly():
    # The exemption was cached per distinct match text, so each new one compared
    # the whole token again: 12000 such matches took ~2.3s, 10.6x a quarter of them.
    data = json.loads((ROOT / "profy" / "data" / "languages" / "english.json").read_text(encoding="utf-8"))
    alone = ProfanityFilter(allow=data["profanities"], block=["ab", "8"])
    small = _best_of_two(alone, _distinct_hex_matches(3000))
    large = _best_of_two(alone, _distinct_hex_matches(12000))
    assert alone.check(_distinct_hex_matches(100)).is_clean  # not the block word repeated
    # Only the entry as typed fills a hex-like token; "ab" obfuscated as "ab8"
    # stays protected there.
    assert alone.check("abababab").clean == "*" * 8
    assert alone.check("ab8ab8ab8").is_clean
    assert large < 5.0, large
    assert large / max(small, 0.02) < QUADRATIC_LIMIT, (small, large)


def test_match_dense_text_scales_linearly():
    # Masking used to copy the working text and shift index lists per match,
    # so "shit " * 128000 took ~5.7s and * 512000 ~80s with a one-word filter.
    data = json.loads((ROOT / "profy" / "data" / "languages" / "english.json").read_text(encoding="utf-8"))
    single = ProfanityFilter(allow=[word for word in data["profanities"] if word.lower() != "shit"])
    assert list(single.dictionary.expressions) == ["shit"]
    small = _best_of_two(single, "shit " * 8000)
    large = _best_of_two(single, "shit " * 32000)
    assert single.check("shit " * 32000).count == 32000
    assert large < 5.0, large
    assert large / max(small, 0.02) < 9, (small, large)


def test_match_dense_text_with_the_default_filter(english):
    small = _best_of_two(english, "shit " * 500)
    large = _best_of_two(english, "shit " * 2000)
    assert english.check("shit " * 2000).count == 2000
    assert large < 5.0, large
    assert large / max(small, 0.02) < 9, (small, large)


@pytest.mark.parametrize(
    "unit",
    [
        pytest.param("twat", id="plain"),
        pytest.param("twaat", id="stretched"),
        pytest.param("twatt", id="stretched-end"),
        pytest.param("dickwad", id="long"),
        pytest.param("shitass", id="mixed-entries"),
        pytest.param("shit-ass", id="hyphenated-entry"),
        pytest.param("shit-ass,", id="separator"),
    ],
)
def test_glued_copies_scale_linearly(english, unit):
    # Each copy's reading led to the next copy again from every earlier one,
    # so the same matches were judged over and over: "twat" * 640 took ~1.4s,
    # 13x the time of a quarter of it.
    small = _best_of_two(english, unit * 160)
    large = _best_of_two(english, unit * 640)
    assert english.check(unit * 640).clean.count("*") >= len(unit.rstrip(",")) * 640
    assert large < 5.0, large
    assert large / max(small, 0.02) < QUADRATIC_LIMIT, (small, large)


def _block_only(block):
    data = json.loads((ROOT / "profy" / "data" / "languages" / "english.json").read_text(encoding="utf-8"))
    return ProfanityFilter(allow=data["profanities"], block=block)


@pytest.mark.parametrize(
    "block, make",
    [
        pytest.param(["x-z"], lambda n: "x" * n + "-z", id="letter"),
        pytest.param(["x-z"], lambda n: "x" * n + "-" + "z" * n, id="letter-both-ends"),
        pytest.param(["--x"], lambda n: "-" * n + "x", id="separator"),
        pytest.param(["\U0001F4A9x"], lambda n: "\U0001F4A9" * n + "x", id="emoji"),
        pytest.param(["1x"], lambda n: "1" * n + "x", id="digit"),
        pytest.param(["xy"], lambda n: "xX" * (n // 2) + "y", id="mixed-case"),
    ],
)
def test_block_only_dictionaries_scale_linearly_on_long_runs(block, make):
    # With only block entries, nothing shortened the runs of their letters
    # and every split of a long first-letter run re-matched the whole rest:
    # block=["x-z"] on "x" * 8000 + "-z" took ~0.9s, 15x a quarter of it.
    shield = _block_only(block)
    small = _best_of_two(shield, make(2000))
    large = _best_of_two(shield, make(8000))
    assert large < 5.0, large
    assert large / max(small, 0.02) < QUADRATIC_LIMIT, (small, large)


def test_block_only_letter_runs_are_shortened_and_masked():
    # Block entries' letters are runs the shortener knows again (only their
    # run lengths do not count), so a long run still matches.
    assert _block_only(["xy"]).check("x" * 80 + "y").clean == "*" * 81
    assert _block_only(["xy"]).check("xX" * 40 + "y").clean == "*" * 81
    assert _block_only(["x-z"]).check("x" * 9000 + "-z").clean == "*" * 9002


class _KeepRuns:
    """A run shortener that shortens nothing."""

    def shorten(self, text, span_map):
        return text, span_map

    def kept(self, character):
        return 0


@pytest.mark.parametrize(
    "block, make",
    [
        pytest.param(["x-z"], lambda n: "x" * n + "-z", id="block-only"),
        pytest.param([], lambda n: "bitch" + "h" * n + "hell", id="bundled"),
    ],
)
def test_split_readings_stay_linear_without_run_shortening(block, make):
    # The splits tried inside a run are bounded by themselves, not by the run
    # shortener: with shortening switched off, "x" * 8000 + "-z" took ~0.9s.
    shield = _block_only(block) if block else ProfanityFilter()
    dictionary = copy.copy(shield.dictionary)
    dictionary.runs = _KeepRuns()
    shield.dictionary = dictionary
    small = _best_of_two(shield, make(2000))
    large = _best_of_two(shield, make(8000))
    assert shield.check(make(8000)).clean.startswith("*" * 100)
    assert large < 5.0, large
    assert large / max(small, 0.02) < QUADRATIC_LIMIT, (small, large)
