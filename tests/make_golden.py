"""Regenerate tests/golden/ from the current code.

Run only on code whose business output is known to be right (it was first run
on master before personal mode). Uses a throwaway database and a fake key:

    .venv/bin/python tests/make_golden.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
# The fake test config, as in conftest.py, before hpgbooks is imported.
os.environ["HPBOOKS_CONFIG"] = str(Path(__file__).parent / "fixtures" / "config.test.toml")

from golden_fixture import GOLDEN_DIR, build_business_ledger, capture_outputs, freeze_today, set_env


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        tmp_path = Path(tmp)
        set_env(mp, tmp_path)
        freeze_today(mp)
        build_business_ledger(tmp_path)
        outputs = capture_outputs()
    GOLDEN_DIR.mkdir(exist_ok=True)
    for old in GOLDEN_DIR.iterdir():
        old.unlink()
    for name, data in sorted(outputs.items()):
        (GOLDEN_DIR / name).write_bytes(data)
    print(f"wrote {len(outputs)} files to {GOLDEN_DIR}")


if __name__ == "__main__":
    main()
