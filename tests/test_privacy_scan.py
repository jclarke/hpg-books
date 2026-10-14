"""scripts/privacy_scan.sh against small trees with planted fake findings.

Every planted value is invented here (and secrets are built at run time) so this
file itself scans clean.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "privacy_scan.sh"

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def _lists(tmp_path: Path) -> Path:
    priv = tmp_path / "priv"
    priv.mkdir()
    (priv / "privacy-terms.local.txt").write_text("# fake deny list\nzebra ?finch\n\\bquokka\\b\n")
    (priv / "privacy-ids.local.txt").write_text("acct-zz-9f3e\n")
    (priv / "privacy-last4.local.txt").write_text("9876 5432\n")
    return priv


def _scan(tree: Path, priv: Path | None, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("HPGBOOKS_PRIVACY_DIR", None)
    env.pop("HPBOOKS_PRIVACY_DIR", None)
    env["HPGBOOKS_PRIVACY_DIR"] = str(priv) if priv else str(tree / "no-lists-here")
    return subprocess.run(["bash", str(SCRIPT), *extra, str(tree)], capture_output=True, text=True, env=env, timeout=300)


def _clean_tree(root: Path) -> Path:
    tree = root / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "app.py").write_text(
        'OWNER = "Alice Example <alice@example.test>"\n'
        'HOST = "127.0.0.1"\n'
        'DOCS = "192.0.2.10"\n'
        'ACCOUNT = "Example Bank Checking 1111"\n'
        "PAGE = 9876  # a page number, nothing else on the line\n"
    )
    (tree / "README.txt").write_text("Nothing personal here. See ~/notes for setup.\n")
    return tree


def _findings(proc: subprocess.CompletedProcess) -> list[str]:
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_clean_tree_passes(tmp_path):
    proc = _scan(_clean_tree(tmp_path), _lists(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _findings(proc) == []


def test_planted_findings_are_reported(tmp_path):
    tree = _clean_tree(tmp_path)
    token = "gh" + "p_" + "Q7x" * 12
    home = "/" + "home" + "/someone/books"
    (tree / "src" / "leaky.py").write_text(
        "\n".join(
            [
                'VENDOR = "Zebra Finch Holdings"',  # 1 terms
                'NOTE = "the QUOKKA account"',  # 2 terms (case-insensitive)
                'ACCT = "acct-zz-9f3e"',  # 3 ids
                'CARD = "Sample card ending 9876"',  # 4 last4 (account context)
                'MASK = "xx5432"',  # 5 last4-mask
                'MAIL = "someone' + "@" + 'corp-mail.com"',  # 6 email
                'PHONE = "(202) 555' + "-" + '0199"',  # 7 phone
                'TAILNET_IP = "100.' + '70.1.2"',  # 8 ipv4 (CGNAT)
                'LAN = "10.' + '1.2.3"',  # 9 ipv4
                'HOST = "box.tail0000.ts' + "." + 'net"',  # 10 tailnet
                f'TOKEN = "{token}"',  # 11 token
                f'PATH = "{home}"',  # 12 home-path
                'DIGEST = "' + "ab" * 32 + '"',  # 13 hex64
            ]
        )
        + "\n"
    )
    (tree / "data").mkdir()
    (tree / "data" / "books.db").write_bytes(b"\0")
    (tree / "config").mkdir()
    (tree / "config" / "local.toml").write_text("[app]\n")
    (tree / ".env").write_text("X=1\n")
    (tree / "photo.png").write_bytes(b"\x89PNG\r\n")

    proc = _scan(tree, _lists(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = _findings(proc)
    text = "\n".join(out)

    def has(line: int, check: str) -> bool:
        return any(f.startswith(f"src/leaky.py:{line}: [{check}]") for f in out)

    for line, check in [
        (1, "terms"), (2, "terms"), (3, "ids"), (4, "last4"), (5, "last4-mask"), (6, "email"),
        (7, "phone"), (8, "ipv4"), (9, "ipv4"), (10, "tailnet"), (11, "token"), (12, "home-path"),
        (13, "hex64"),
    ]:
        assert has(line, check), f"line {line} [{check}] not reported:\n{text}"
    for path in ("data/books.db", "config/local.toml", ".env", "photo.png"):
        assert f"{path}:0: [file]" in text, f"{path} not reported:\n{text}"
    # The clean file stays clean: allowed email, loopback, doc range, 9876 without account words.
    assert "src/app.py" not in text


def test_missing_lists_fail_unless_allowed(tmp_path):
    tree = _clean_tree(tmp_path)
    proc = _scan(tree, None)
    assert proc.returncode == 3
    assert "deny lists missing" in proc.stderr
    proc = _scan(tree, None, "--allow-missing-local-lists")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WARNING" in proc.stderr


def test_allowlist_suppresses_documented_false_positive(tmp_path, monkeypatch):
    tree = _clean_tree(tmp_path)
    (tree / "rules.py").write_text('GROCERIES = r"FAKE MART|QUOKKA MARKET|SAMPLE FOODS"\n')
    priv = _lists(tmp_path)
    assert _scan(tree, priv).returncode == 1  # "quokka" is on the fake deny list
    allow = tmp_path / "allow.txt"
    allow.write_text("# a public store name in a generic rule\nQUOKKA MARKET\n")
    monkeypatch.setenv("HPGBOOKS_PRIVACY_ALLOWLIST", str(allow))
    proc = _scan(tree, priv)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_repo_allowlist_entries_are_documented():
    """Every allowlist entry is preceded by a comment saying why it is a false positive."""
    lines = (SCRIPT.parent / "privacy_allowlist.txt").read_text().splitlines()
    for index, line in enumerate(lines):
        if line.strip() and not line.startswith("#"):
            assert index > 0 and lines[index - 1].startswith("#"), f"undocumented allowlist entry: {line}"
