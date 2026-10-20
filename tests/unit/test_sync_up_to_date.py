import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_sync_api_is_written_from_the_async_source_as_it_now_stands():
    checked = subprocess.run(
        [sys.executable, "scripts/unasync.py", "--check"], cwd=ROOT, capture_output=True, text=True
    )
    assert checked.returncode == 0, checked.stderr
