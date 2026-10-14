#!/usr/bin/env python3
"""Compare a Capital One CSV export to statement PDFs.

Invoked by `hpgbooks verify-capitalone`. Can also be run directly:

    .venv/bin/python sync/verify_capone_statements.py --csv FILE --statements PDF...
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hpgbooks.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["verify-capitalone", *sys.argv[1:]]))
