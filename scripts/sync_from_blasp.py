#!/usr/bin/env python3
'''Sync Profy's upstream word lists from the Blaspsoft/blasp PHP repository.

Only the per-language word lists are pulled from ``config/languages/*.php``:
``profanities``, ``false_positives`` and the ``severity`` buckets. Everything
else -- separators, substitutions, global false positives and the matching
engine itself -- is Profy-owned and edited locally.

The export is written to ``sources/upstream/<language>.json`` and the shipped
bundle in ``profy/data/languages`` is re-assembled from the word-list layers
(see ``scripts/wordlist_layers.py``). ``profy/data/upstream.json`` and
``docs/upstream-sync-report.md`` are rewritten only when the upstream commit or
the exported word lists change, so a no-op sync leaves the tree untouched.
Pass ``--check`` to exit 1 when a sync would change anything, writing nothing.
'''

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

import wordlist_layers as layers

ROOT = layers.ROOT
DEFAULT_UPSTREAM = "https://github.com/Blaspsoft/blasp.git"
SEVERITY_LEVELS = ("mild", "moderate", "high", "extreme")

EXPORT_PHP = r'''
if (!function_exists('env')) {
    function env(string $key, mixed $default = null): mixed {
        $value = getenv($key);
        return $value === false ? $default : $value;
    }
}
$out = [];
foreach (glob('config/languages/*.php') as $file) {
    $data = require $file;
    $lists = [];
    foreach (['severity', 'profanities', 'false_positives'] as $key) {
        if (is_array($data) && array_key_exists($key, $data)) {
            $lists[$key] = $data[$key];
        }
    }
    $out[basename($file, '.php')] = (object) $lists;
}
echo json_encode((object) $out, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
'''


def metadata_path(root: Path) -> Path:
    return root / "profy" / "data" / "upstream.json"


def report_path(root: Path) -> Path:
    return root / "docs" / "upstream-sync-report.md"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(command: list[str], *, cwd: Optional[Path] = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"`{command[0]}` failed ({error.returncode}): {error.stderr.strip()}") from error
    return completed.stdout.strip()


def ensure_upstream(args: argparse.Namespace) -> Path:
    if args.source_dir:
        source = Path(args.source_dir).expanduser().resolve()
        if not (source / "config" / "languages").is_dir():
            raise SystemExit(f"{source} does not look like a Blasp checkout (no config/languages)")
        return source

    cache_dir = Path(args.cache_dir).expanduser().resolve()
    if not cache_dir.exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", args.upstream, str(cache_dir)])

    run(["git", "fetch", "--tags", "--prune", "origin"], cwd=cache_dir)
    run(["git", "checkout", args.ref], cwd=cache_dir)

    if args.ref == "main":
        run(["git", "reset", "--hard", "origin/main"], cwd=cache_dir)

    return cache_dir


def _string_list(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"upstream {what} is not a list of strings")
    return value


def clean_wordlists(language: str, data: object) -> dict:
    """Validate one language's exported word lists into the layer format."""
    if not isinstance(data, dict) or "profanities" not in data:
        raise SystemExit(f"upstream language {language!r} has no profanities list")
    lists: dict = {}
    if "severity" in data:
        severity = data["severity"]
        # PHP encodes an empty associative array as a JSON list.
        severity = {} if severity == [] else severity
        if not isinstance(severity, dict):
            raise SystemExit(f"upstream {language} severity is not a mapping")
        unknown = sorted(set(severity) - set(SEVERITY_LEVELS))
        if unknown:
            raise SystemExit(
                f"upstream {language} uses unknown severity level(s) {unknown}; "
                "add them to profy.core.Severity before syncing"
            )
        lists["severity"] = {
            level: _string_list(words, f"{language} severity.{level}") for level, words in severity.items()
        }
    lists["profanities"] = _string_list(data["profanities"], f"{language} profanities")
    if "false_positives" in data:
        lists["false_positives"] = _string_list(data["false_positives"], f"{language} false_positives")
    return lists


def export_wordlists(upstream: Path) -> dict[str, dict]:
    exported = json.loads(run(["php", "-r", EXPORT_PHP], cwd=upstream))
    if not exported:
        raise SystemExit(f"no language files found under {upstream / 'config' / 'languages'}")
    return {language: clean_wordlists(language, data) for language, data in sorted(exported.items())}


def git_commit(upstream: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=upstream)


def git_remote(upstream: Path) -> str:
    try:
        return run(["git", "remote", "get-url", "origin"], cwd=upstream)
    except SystemExit:
        return str(upstream)


def load_previous(root: Path) -> dict:
    path = metadata_path(root)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_metadata(*, upstream_url: str, commit: str, wordlists: Mapping[str, dict]) -> dict:
    """Metadata without the timestamp, so it is a pure function of the sync input."""
    files = {}
    for language in sorted(wordlists):
        path = (layers.upstream_dir(ROOT) / f"{language}.json").relative_to(ROOT).as_posix()
        files[path] = hashlib.sha256(layers.dumps(wordlists[language]).encode("utf-8")).hexdigest()
    return {
        "upstream": "Blaspsoft/blasp",
        "upstream_url": upstream_url,
        "commit": commit,
        "wordlist_files": files,
    }


def render_report(
    *,
    metadata: Mapping[str, object],
    previous: Mapping[str, object],
    wordlists: Mapping[str, dict],
    synced_at: str,
) -> str:
    lines = [
        "# Upstream Blasp Sync Report",
        "",
        f"- Upstream: `{metadata['upstream_url']}`",
        f"- Current commit: `{metadata['commit']}`",
        f"- Previous synced commit: `{previous.get('commit', 'none')}`",
        f"- Synced at: `{synced_at}`",
        "",
        "Only the per-language word lists (`profanities`, `false_positives`, `severity`) are",
        "synced from upstream. Separators, substitutions, global false positives and the",
        "matching engine are Profy-owned.",
        "",
        "## Exported Word Lists",
        "",
        "| Language | Profanities | False positives | Severity-classified |",
        "| --- | ---: | ---: | ---: |",
    ]
    for language in sorted(wordlists):
        lists = wordlists[language]
        classified = sum(len(words) for words in lists.get("severity", {}).values())
        lines.append(
            f"| {language} | {len(lists['profanities'])} | {len(lists.get('false_positives', []))} | {classified} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="Blasp git URL")
    parser.add_argument("--ref", default="main", help="Git ref to sync")
    parser.add_argument(
        "--cache-dir",
        default=str(ROOT / ".cache" / "blasp-upstream"),
        help="Local cache used when --source-dir is not provided",
    )
    parser.add_argument(
        "--source-dir",
        help="Use an existing local Blasp checkout instead of cloning/fetching",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if a sync would change any file; write nothing",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if not shutil.which("php"):
        raise SystemExit("php is required to export Blasp config arrays")
    if not shutil.which("git"):
        raise SystemExit("git is required to fetch upstream Blasp")

    args = parse_args(argv)
    check = args.check
    upstream = ensure_upstream(args)
    wordlists = export_wordlists(upstream)
    commit = git_commit(upstream)
    upstream_url = git_remote(upstream)

    changed: list[Path] = []
    layer_dir = layers.upstream_dir(ROOT)
    for language, lists in wordlists.items():
        target = layer_dir / f"{language}.json"
        if layers.write_if_changed(target, layers.dumps(lists), check=check):
            changed.append(target)
    for stale in sorted(layer_dir.glob("*.json")):
        if stale.stem not in wordlists:
            # The bundled file keeps its last word lists; drop it by hand if the
            # language should go away too.
            print(f"upstream no longer provides {stale.stem}; profy/data/languages/{stale.name} left as is")
            changed.append(stale)
            if not check:
                stale.unlink()
    changed.extend(layers.assemble(ROOT, check=check, upstream=wordlists))

    previous = load_previous(ROOT)
    metadata = build_metadata(upstream_url=upstream_url, commit=commit, wordlists=wordlists)
    previous_core = {key: value for key, value in previous.items() if key != "synced_at"}
    if previous_core != metadata or not report_path(ROOT).is_file():
        changed.append(metadata_path(ROOT))
        if not check:
            synced_at = utcnow()
            metadata_path(ROOT).write_text(
                json.dumps({**metadata, "synced_at": synced_at}, indent=2, ensure_ascii=False, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            report_path(ROOT).parent.mkdir(parents=True, exist_ok=True)
            report_path(ROOT).write_text(
                render_report(metadata=metadata, previous=previous, wordlists=wordlists, synced_at=synced_at),
                encoding="utf-8",
            )

    relative = [path.relative_to(ROOT).as_posix() for path in changed]
    if check:
        if relative:
            print(f"Upstream {commit} would change:")
            print("\n".join(f"- {path}" for path in relative))
            return 1
        print(f"Up to date with upstream {commit}")
        return 0
    if relative:
        print(f"Synced Blasp word lists from {commit}:")
        print("\n".join(f"- {path}" for path in relative))
    else:
        print(f"Already up to date with upstream {commit}; nothing written")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
