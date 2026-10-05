import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def run_isolated(code: str, timeout: float) -> object:
    """Run ``code`` in a fresh interpreter and return the JSON it prints last.

    Used for inputs that used to hang: a regression then fails the test via the
    timeout instead of freezing the whole test run.
    """
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])
