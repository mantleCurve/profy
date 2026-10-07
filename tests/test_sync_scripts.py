"""Offline, deterministic tests for the word-list sync scripts.

git, php and HTTP are replaced by fakes; every test works in a temporary copy of
the repository layout.
"""

import io
import json
import shutil
import subprocess
import urllib.error

import pytest

import sync_external_sources as external
import sync_from_blasp as blasp
import wordlist_layers as layers

UPSTREAM = {
    "english": {
        "severity": {"mild": ["damn"], "moderate": [], "high": ["shit"], "extreme": ["coon"]},
        "profanities": ["damn", "shit", "coon", "fuck", "Fucking"],
        "false_positives": ["scunthorpe", "classy"],
    },
    "german": {"severity": [], "profanities": ["scheisse"], "false_positives": []},
}

SOURCE_PAYLOADS = {
    "leo-profanity": json.dumps(["Fuck", "bollocks", "x", "SHIT", "wanker"]),
    "profane-words": json.dumps(["wanker", "wankers", "classy", "fuckings", "a" * 41, "123", "tw@t"]),
    "badwords-list": "export default ['arse', 'bugger', 'ab<c'];",
    "ldnoobw-en": "bloody\n\n  git  \nfuck\n",
}

BUNDLE_ENGLISH = {
    "severity": {"mild": ["old"]},
    "profanities": ["old"],
    "false_positives": [],
    "substitutions": {"/a/": ["a", "@"]},
}
BUNDLE_GERMAN = {"profanities": ["alt"], "substitutions": {"/ä/": ["ä", "ae"]}, "severity": {}}


class FakeTools:
    """Stands in for ``subprocess.run`` (git and php) and records every call."""

    def __init__(self, commit="a" * 40):
        self.commit = commit
        self.upstream = json.loads(json.dumps(UPSTREAM))
        self.calls = []
        self.remote_fails = False
        self.fail = None

    def __call__(self, command, cwd=None, **kwargs):
        self.calls.append(command[:2])
        if self.fail and command[0] == self.fail:
            raise subprocess.CalledProcessError(2, command, output="", stderr="boom\n")
        stdout = ""
        if command[:2] == ["git", "clone"]:
            (cwd or __import__("pathlib").Path(command[3])).mkdir(parents=True, exist_ok=True)
        elif command[:2] == ["git", "rev-parse"]:
            stdout = self.commit
        elif command[:2] == ["git", "remote"]:
            if self.remote_fails:
                raise subprocess.CalledProcessError(1, command, output="", stderr="no remote")
            stdout = "https://example.test/blasp.git"
        elif command[0] == "php":
            stdout = json.dumps(self.upstream)
        return subprocess.CompletedProcess(command, 0, stdout=stdout + "\n", stderr="")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    languages = root / "profy" / "data" / "languages"
    languages.mkdir(parents=True)
    (languages / "english.json").write_text(layers.dumps(BUNDLE_ENGLISH), encoding="utf-8")
    (languages / "german.json").write_text(layers.dumps(BUNDLE_GERMAN), encoding="utf-8")
    for module in (layers, blasp, external):
        monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(blasp.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(blasp, "utcnow", lambda: "2026-10-06T00:00:00+00:00")
    return root


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(blasp.subprocess, "run", fake)
    return fake


@pytest.fixture
def sources(monkeypatch):
    payloads = {external.SOURCES[name]: text for name, text in SOURCE_PAYLOADS.items()}
    monkeypatch.setattr(external, "fetch", lambda url: payloads[url])
    return payloads


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".cache" not in path.parts
    }


def bundle(root, language="english"):
    return json.loads((root / "profy" / "data" / "languages" / f"{language}.json").read_text(encoding="utf-8"))


def sync(root, *args):
    return blasp.main(["--cache-dir", str(root / ".cache" / "blasp"), *args])


# --- sync_from_blasp -----------------------------------------------------------------


def test_first_sync_exports_only_word_lists(repo, tools, capsys):
    assert sync(repo) == 0

    english_layer = json.loads((repo / "sources" / "upstream" / "english.json").read_text(encoding="utf-8"))
    assert english_layer == UPSTREAM["english"]
    german_layer = json.loads((repo / "sources" / "upstream" / "german.json").read_text(encoding="utf-8"))
    assert german_layer == {"severity": {}, "profanities": ["scheisse"], "false_positives": []}

    english = bundle(repo)
    assert list(english) == ["severity", "profanities", "false_positives", "substitutions"]
    assert english["profanities"] == UPSTREAM["english"]["profanities"]
    assert english["substitutions"] == BUNDLE_ENGLISH["substitutions"]
    # Existing key order is kept; newly provided word-list keys are appended.
    assert list(bundle(repo, "german")) == ["profanities", "substitutions", "severity", "false_positives"]
    assert bundle(repo, "german")["substitutions"] == BUNDLE_GERMAN["substitutions"]

    metadata = json.loads((repo / "profy" / "data" / "upstream.json").read_text(encoding="utf-8"))
    assert set(metadata) == {"commit", "synced_at", "upstream", "upstream_url", "wordlist_files"}
    assert metadata["commit"] == "a" * 40
    assert sorted(metadata["wordlist_files"]) == ["sources/upstream/english.json", "sources/upstream/german.json"]
    report = (repo / "docs" / "upstream-sync-report.md").read_text(encoding="utf-8")
    assert "Previous synced commit: `none`" in report
    assert "| english | 5 | 2 | 3 |" in report
    assert "Implementation" not in report
    assert not (repo / "profy" / "data" / "global.json").exists()

    assert ["git", "clone"] in tools.calls and ["git", "reset"] in tools.calls
    assert "Synced Blasp word lists" in capsys.readouterr().out


def test_noop_resync_writes_nothing(repo, tools, monkeypatch, capsys):
    sync(repo)
    before = snapshot(repo)
    # A later clock must not rewrite anything: the time is not even taken
    # when nothing changed.
    clock = []
    monkeypatch.setattr(blasp, "utcnow", lambda: clock.append(1) or "2030-01-01T00:00:00+00:00")
    capsys.readouterr()

    assert sync(repo) == 0
    assert snapshot(repo) == before
    assert clock == []
    assert "nothing written" in capsys.readouterr().out
    assert ["git", "clone"] not in tools.calls[-6:]


def test_check_reports_pending_changes_without_writing(repo, tools, capsys):
    sync(repo)
    assert sync(repo, "--check") == 0
    assert "Up to date" in capsys.readouterr().out

    tools.upstream["english"]["profanities"].append("bastard")
    before = snapshot(repo)
    assert sync(repo, "--check") == 1
    out = capsys.readouterr().out
    assert "- sources/upstream/english.json" in out
    assert "- profy/data/languages/english.json" in out
    assert "- profy/data/upstream.json" in out
    assert snapshot(repo) == before


def test_new_upstream_commit_with_same_data_only_updates_metadata(repo, tools):
    sync(repo)
    before = snapshot(repo)
    tools.commit = "b" * 40

    assert sync(repo) == 0
    after = snapshot(repo)
    changed = {path for path in after if after[path] != before.get(path)}
    assert changed == {"profy/data/upstream.json", "docs/upstream-sync-report.md"}
    report = after["docs/upstream-sync-report.md"].decode()
    assert f"Previous synced commit: `{'a' * 40}`" in report


def test_changed_upstream_data_flows_into_the_bundle(repo, tools):
    sync(repo)
    tools.upstream["english"]["profanities"].append("bastard")
    tools.upstream["english"]["false_positives"] = []
    assert sync(repo) == 0
    assert bundle(repo)["profanities"][-1] == "bastard"
    assert bundle(repo)["false_positives"] == []


def test_word_list_keys_upstream_drops_are_removed_from_the_bundle(repo, tools):
    del tools.upstream["english"]["false_positives"]
    sync(repo)
    assert "false_positives" not in bundle(repo)
    assert bundle(repo)["substitutions"] == BUNDLE_ENGLISH["substitutions"]


def test_language_removed_upstream(repo, tools, capsys):
    sync(repo)
    german_bundle = (repo / "profy" / "data" / "languages" / "german.json").read_bytes()
    del tools.upstream["german"]

    assert sync(repo, "--check") == 1
    assert (repo / "sources" / "upstream" / "german.json").exists()
    assert sync(repo) == 0
    assert not (repo / "sources" / "upstream" / "german.json").exists()
    assert (repo / "profy" / "data" / "languages" / "german.json").read_bytes() == german_bundle
    assert "upstream no longer provides german" in capsys.readouterr().out


def test_missing_report_is_regenerated(repo, tools):
    sync(repo)
    (repo / "docs" / "upstream-sync-report.md").unlink()
    assert sync(repo, "--check") == 1
    assert sync(repo) == 0
    assert (repo / "docs" / "upstream-sync-report.md").is_file()


def test_source_dir_skips_git_fetching(repo, tools, tmp_path):
    checkout = tmp_path / "blasp"
    (checkout / "config" / "languages").mkdir(parents=True)
    assert blasp.main(["--source-dir", str(checkout)]) == 0
    assert ["git", "fetch"] not in tools.calls
    assert ["git", "rev-parse"] in tools.calls


def test_bad_source_dir(repo, tools, tmp_path):
    with pytest.raises(SystemExit, match="does not look like a Blasp checkout"):
        blasp.main(["--source-dir", str(tmp_path)])


def test_existing_cache_and_non_main_ref(repo, tools):
    (repo / ".cache" / "blasp").mkdir(parents=True)
    assert sync(repo, "--ref", "v4.0.0") == 0
    assert ["git", "clone"] not in tools.calls
    assert ["git", "checkout"] in tools.calls
    assert ["git", "reset"] not in tools.calls


def test_remote_lookup_falls_back_to_the_path(repo, tools):
    tools.remote_fails = True
    sync(repo)
    metadata = json.loads((repo / "profy" / "data" / "upstream.json").read_text(encoding="utf-8"))
    assert metadata["upstream_url"].endswith("blasp")


@pytest.mark.parametrize("missing", ["php", "git"])
def test_required_tools(repo, tools, monkeypatch, missing):
    monkeypatch.setattr(blasp.shutil, "which", lambda name: None if name == missing else "/usr/bin/x")
    with pytest.raises(SystemExit, match=f"{missing} is required"):
        sync(repo)


def test_failing_command_reports_stderr(repo, tools):
    tools.fail = "php"
    with pytest.raises(SystemExit, match="`php` failed \\(2\\): boom"):
        sync(repo)


@pytest.mark.parametrize(
    "upstream, message",
    [
        ({}, "no language files found"),
        ({"english": []}, "has no profanities list"),
        ({"english": {"false_positives": []}}, "has no profanities list"),
        ({"english": {"profanities": "shit"}}, "english profanities is not a list of strings"),
        ({"english": {"profanities": ["ok", 3]}}, "english profanities is not a list of strings"),
        ({"english": {"profanities": [], "false_positives": [None]}}, "false_positives is not a list"),
        ({"english": {"profanities": [], "severity": ["high"]}}, "severity is not a mapping"),
        ({"english": {"profanities": [], "severity": {"critical": ["x"]}}}, "unknown severity level"),
        ({"english": {"profanities": [], "severity": {"high": "x"}}}, "severity.high is not a list"),
    ],
)
def test_invalid_upstream_data_is_rejected(repo, tools, upstream, message):
    tools.upstream = upstream
    before = snapshot(repo)
    with pytest.raises(SystemExit, match=message):
        sync(repo)
    assert snapshot(repo) == before


# --- sync_external_sources ---------------------------------------------------------------


def test_parsers_and_normalize():
    assert external.parse_leo('["a", "b"]') == ["a", "b"]
    assert external.parse_zautumn('["c"]') == ["c"]
    assert external.parse_badwords_ts("x = ['one', 'two words'];") == ["one", "two words"]
    assert external.parse_ldnoobw(" one \n\ntwo\n") == ["one", "two"]
    assert external.normalize("  Hello ") == "hello"
    assert external.normalize("a") is None
    assert external.normalize("a" * 41) is None
    assert external.normalize("ab<c") is None
    assert external.normalize("1234") is None
    assert external.normalize("f*ck-it_now!") == "f*ck-it_now!"


def test_fetch_decodes_the_response(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(external.urllib.request, "urlopen", lambda url, timeout: Response("ä".encode()))
    assert external.fetch("https://example.test") == "ä"


def test_collect_normalizes_and_dedupes(sources):
    layer, dropped = external.collect()
    assert list(layer["sources"]) == list(external.SOURCES)
    assert layer["sources"]["leo-profanity"] == {
        "url": external.SOURCES["leo-profanity"],
        "words": ["fuck", "bollocks", "shit", "wanker"],
    }
    assert layer["sources"]["ldnoobw-en"]["words"] == ["bloody", "git", "fuck"]
    assert dropped == {"leo-profanity": 1, "profane-words": 2, "badwords-list": 1, "ldnoobw-en": 0}


def test_external_sync_requires_the_upstream_layer(repo, sources):
    with pytest.raises(SystemExit, match="run scripts/sync_from_blasp.py first"):
        external.main()


def test_external_sync_merges_new_words(repo, tools, sources, capsys):
    sync(repo)
    capsys.readouterr()
    assert external.main() == 0

    profanities = bundle(repo)["profanities"]
    assert profanities[: len(UPSTREAM["english"]["profanities"])] == UPSTREAM["english"]["profanities"]
    # New, non-false-positive, non-suffix-variant words in source order.
    assert profanities[len(UPSTREAM["english"]["profanities"]) :] == [
        "bollocks", "wanker", "tw@t", "arse", "bugger", "bloody", "git",
    ]
    err = capsys.readouterr().err
    assert "[merge] leo-profanity: +2 new, dropped 1" in err
    assert "bundle updated" in err

    before = snapshot(repo)
    assert external.main() == 0
    assert snapshot(repo) == before
    assert "bundle unchanged" in capsys.readouterr().err


def test_failed_download_writes_nothing(repo, tools, sources, monkeypatch):
    sync(repo)
    before = snapshot(repo)

    def broken(url):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(external, "fetch", broken)
    with pytest.raises(SystemExit, match="failed to fetch leo-profanity"):
        external.main()
    assert snapshot(repo) == before


# --- layering and assembly -------------------------------------------------------------------


def test_scripts_are_order_independent(repo, tools, sources, tmp_path):
    sync(repo)
    external.main()
    other = tmp_path / "other"
    shutil.copytree(repo, other)

    # New upstream data and new source data, applied in opposite orders.
    tools.upstream["english"]["profanities"].append("wanker")
    tools.upstream["english"]["false_positives"].append("bugger")
    payloads = {external.SOURCES[name]: text for name, text in SOURCE_PAYLOADS.items()}
    payloads[external.SOURCES["ldnoobw-en"]] += "minger\n"
    external.fetch = lambda url: payloads[url]  # restored by the sources fixture's monkeypatch

    sync(repo)
    external.main()
    first = snapshot(repo)

    for module in (layers, blasp, external):
        module.ROOT = other
    external.main()
    sync(other)
    second = snapshot(other)

    assert {path: data for path, data in first.items() if "upstream.json" not in path and "report" not in path} == {
        path: data for path, data in second.items() if "upstream.json" not in path and "report" not in path
    }
    english = json.loads(first["profy/data/languages/english.json"])
    assert "minger" in english["profanities"] and "bugger" not in english["profanities"]
    assert english["profanities"].count("wanker") == 1


def test_assemble_cli(repo, tools, capsys):
    sync(repo)
    capsys.readouterr()
    assert layers.main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out

    path = repo / "profy" / "data" / "languages" / "english.json"
    edited = bundle(repo)
    edited["profanities"] = ["hand-edited"]
    path.write_text(layers.dumps(edited), encoding="utf-8")
    assert layers.main(["--check"]) == 1
    assert "out of date: profy/data/languages/english.json" in capsys.readouterr().out
    assert layers.main([]) == 0
    assert "assembled: profy/data/languages/english.json" in capsys.readouterr().out
    assert bundle(repo)["profanities"] == UPSTREAM["english"]["profanities"]


def test_assemble_creates_missing_bundles(repo, tools):
    tools.upstream["italian"] = {"profanities": ["cazzo"]}
    sync(repo)
    assert bundle(repo, "italian") == {"profanities": ["cazzo"]}


def test_merge_external_rules():
    merged, added = layers.merge_external(
        ["Fuck"],
        ["Classy"],
        {"mild": ["Damn"]},
        {"one": ["fuck", "damn", "classy", "fucks", "new"], "two": ["new", "news", "other"]},
    )
    assert merged == ["Fuck", "new", "other"]
    assert added == {"one": 1, "two": 1}


@pytest.mark.parametrize(
    "word, redundant",
    [("fuckings", True), ("fucked", True), ("fucky", True), ("ass", False), ("asses", True), ("ases", False), ("fucking", True), ("new", False)],
)
def test_suffix_variants(word, redundant):
    assert layers.is_redundant_suffix_variant(word, {"fuck", "fucking", "ass"}) is redundant


def test_write_if_changed(tmp_path):
    path = tmp_path / "nested" / "file.json"
    assert layers.write_if_changed(path, "x", check=True)
    assert not path.exists()
    assert layers.write_if_changed(path, "x")
    assert not layers.write_if_changed(path, "x")
    assert layers.dumps({"a": ["é"]}) == '{\n    "a": [\n        "é"\n    ]\n}\n'


def test_utcnow_is_an_aware_iso_timestamp():
    from datetime import datetime

    assert datetime.fromisoformat(blasp.utcnow()).utcoffset() is not None
