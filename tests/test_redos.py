"""Adversarial inputs must stay fast for every bundled language.

Blasp's generated expressions backtracked exponentially on runs of characters
shared by several letters and the separators: ``"*" * 22`` took ~29s and
``"*" * 24`` never finished. Each language is exercised in its own interpreter
with a hard timeout, so a regression fails instead of hanging the test run.
"""

import json
import subprocess
import sys

import pytest

from helpers import ROOT

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
