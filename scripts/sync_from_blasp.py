#!/usr/bin/env python3
'''Sync Profy's bundled data from the upstream Blasp PHP repository.

The script fetches or reads Blaspsoft/blasp, exports PHP config arrays to JSON,
updates profy/data, and writes a report that flags upstream PHP implementation
changes for manual port review.
'''

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DATA = ROOT / "profy" / "data"
REPORT_PATH = ROOT / "docs" / "upstream-sync-report.md"
METADATA_PATH = PACKAGE_DATA / "upstream.json"
DEFAULT_UPSTREAM = "https://github.com/Blaspsoft/blasp.git"
IMPLEMENTATION_GLOBS = [
    "src/Core/**/*.php",
    "src/Drivers/**/*.php",
    "src/Enums/**/*.php",
    "src/PendingCheck.php",
    "config/blasp.php",
]


def run(command: list[str], *, cwd: Optional[Path] = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def ensure_upstream(args: argparse.Namespace) -> Path:
    if args.source_dir:
        source = Path(args.source_dir).expanduser().resolve()
        if not (source / "config" / "blasp.php").is_file():
            raise SystemExit(f"{source} does not look like a Blasp checkout")
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


def php_json(upstream: Path, php_expression: str) -> str:
    php = r'''
if (!function_exists('env')) {
    function env(string $key, mixed $default = null): mixed {
        $value = getenv($key);
        return $value === false ? $default : $value;
    }
}
''' + php_expression
    return run(["php", "-r", php], cwd=upstream) + "\n"


def export_data(upstream: Path) -> list[Path]:
    languages_dir = PACKAGE_DATA / "languages"
    languages_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    global_json = php_json(
        upstream,
        r'''
$global = require 'config/blasp.php';
$out = [
    'separators' => $global['separators'] ?? [],
    'substitutions' => $global['substitutions'] ?? [],
    'false_positives' => $global['false_positives'] ?? [],
    'phonetic_false_positives' => $global['drivers']['phonetic']['false_positives'] ?? [],
];
echo json_encode($out, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
''',
    )
    global_path = PACKAGE_DATA / "global.json"
    global_path.write_text(global_json, encoding="utf-8")
    written.append(global_path)

    for language_file in sorted((upstream / "config" / "languages").glob("*.php")):
        language = language_file.stem
        language_json = php_json(
            upstream,
            f'''
$data = require 'config/languages/{language}.php';
echo json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
''',
        )
        target = languages_dir / f"{language}.json"
        target.write_text(language_json, encoding="utf-8")
        written.append(target)

    return written


def git_commit(upstream: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=upstream)


def git_remote(upstream: Path) -> str:
    try:
        return run(["git", "remote", "get-url", "origin"], cwd=upstream)
    except subprocess.CalledProcessError:
        return str(upstream)


def implementation_files(upstream: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for pattern in IMPLEMENTATION_GLOBS:
        for path in sorted(upstream.glob(pattern)):
            if path.is_file():
                relative = path.relative_to(upstream).as_posix()
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def load_previous() -> dict[str, object]:
    if not METADATA_PATH.is_file():
        return {}
    return json.loads(METADATA_PATH.read_text(encoding="utf-8"))


def changed_files(previous: dict[str, object], current: dict[str, str]) -> list[str]:
    old = previous.get("implementation_files")
    if not isinstance(old, dict):
        return []
    changed: list[str] = []
    for path, digest in current.items():
        if old.get(path) != digest:
            changed.append(path)
    for path in old:
        if path not in current:
            changed.append(str(path))
    return sorted(set(changed))


def write_metadata(
    *,
    upstream_url: str,
    commit: str,
    implementation: dict[str, str],
    data_files: Iterable[Path],
) -> None:
    metadata = {
        "upstream": "Blaspsoft/blasp",
        "upstream_url": upstream_url,
        "commit": commit,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "data_files": [path.relative_to(ROOT).as_posix() for path in data_files],
        "implementation_files": implementation,
    }
    METADATA_PATH.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_report(
    *,
    upstream_url: str,
    commit: str,
    previous: dict[str, object],
    changed: list[str],
    data_files: Iterable[Path],
) -> None:
    previous_commit = previous.get("commit", "none")
    lines = [
        "# Upstream Blasp Sync Report",
        "",
        f"- Upstream: `{upstream_url}`",
        f"- Current commit: `{commit}`",
        f"- Previous synced commit: `{previous_commit}`",
        f"- Synced at: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Exported Data",
        "",
    ]
    lines.extend(f"- `{path.relative_to(ROOT).as_posix()}`" for path in data_files)
    lines.extend(["", "## PHP Implementation Changes", ""])

    if not previous:
        lines.append("No previous upstream metadata existed; this sync establishes the baseline.")
    elif changed:
        lines.append(
            "The following upstream PHP implementation files changed. Review these "
            "manually and port relevant behavior into `profy/core.py`:"
        )
        lines.append("")
        lines.extend(f"- `{path}`" for path in changed)
    else:
        lines.append("No tracked PHP implementation files changed since the previous sync.")

    lines.append("")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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
    return parser.parse_args()


def main() -> None:
    if not shutil.which("php"):
        raise SystemExit("php is required to export Blasp config arrays")
    if not shutil.which("git"):
        raise SystemExit("git is required to fetch upstream Blasp")

    args = parse_args()
    upstream = ensure_upstream(args)
    previous = load_previous()
    data_files = export_data(upstream)
    implementation = implementation_files(upstream)
    changed = changed_files(previous, implementation)
    commit = git_commit(upstream)
    upstream_url = git_remote(upstream)

    write_metadata(
        upstream_url=upstream_url,
        commit=commit,
        implementation=implementation,
        data_files=data_files,
    )
    write_report(
        upstream_url=upstream_url,
        commit=commit,
        previous=previous,
        changed=changed,
        data_files=data_files,
    )

    print(f"Synced Blasp data from {commit}")
    if changed:
        print("Tracked PHP implementation changes need manual port review:")
        for path in changed:
            print(f"- {path}")


if __name__ == "__main__":
    main()
