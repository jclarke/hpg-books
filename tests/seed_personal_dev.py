"""Fill a throwaway database with the fake business ledger and fake personal data (dev only).

Refuses to run unless HPBOOKS_DB points under /tmp and a key comes from
HPBOOKS_KEY or HPBOOKS_KEY_FILE, so it can never touch the real ledger:

    HPBOOKS_DB=/tmp/personal_dev/hpbooks.db HPBOOKS_KEY_FILE=/tmp/personal_dev/key \\
        .venv/bin/python tests/seed_personal_dev.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    db = os.environ.get("HPBOOKS_DB", "")
    if not db.startswith("/tmp/") or not (os.environ.get("HPBOOKS_KEY") or os.environ.get("HPBOOKS_KEY_FILE", "").startswith("/tmp/")):
        print("refusing: set HPBOOKS_DB and HPBOOKS_KEY_FILE to paths under /tmp", file=sys.stderr)
        return 2
    if Path(db).exists():
        print(f"refusing: {db} already exists; remove it first", file=sys.stderr)
        return 2
    os.environ.setdefault("HPBOOKS_CONFIG", str(Path(__file__).parent / "fixtures" / "config.test.toml"))
    os.environ["HPGBOOKS_SEED_CSV"] = str(Path(db).parent / "no-seed.csv")
    from golden_fixture import build_business_ledger, run_cli
    from personal_fake import write_inbox

    work = Path(db).parent
    build_business_ledger(work)
    day = write_inbox(work / "inbox")
    print(run_cli(["accounts", "discover", str(work / "inbox" / "finance_list_accounts_2026-09-30.json"), "--as-of", "2026-09-30"])[1])
    code, out, err = run_cli(["import", str(day)])
    print(out, err)
    print(run_cli(["personal", "recurring", "detect"])[1][:400])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
