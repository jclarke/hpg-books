"""Guards shared by every test: never open the real database or key.

Tests read tests/fixtures/config.test.toml (fake businesses, accounts, and
WHMCS brands), never config/local.toml. HPBOOKS_CONFIG is set before any
hpgbooks module is imported because business tags and categories are fixed at import.
"""

from __future__ import annotations

import os
from pathlib import Path

TEST_CONFIG = Path(__file__).parent / "fixtures" / "config.test.toml"
os.environ["HPBOOKS_CONFIG"] = str(TEST_CONFIG)

import pytest  # noqa: E402

import hpgbooks.config as hpconfig  # noqa: E402
import hpgbooks.db as hpdb  # noqa: E402

hpconfig.reset()

REAL_DB = os.path.realpath(hpdb.DEFAULT_DB)
REAL_KEY = os.path.realpath(hpdb.DEFAULT_KEY_FILE)
REAL_DATA = os.path.dirname(REAL_DB)


def _is_real(path: str) -> bool:
    resolved = os.path.realpath(path)
    return resolved == REAL_DB or resolved.startswith(REAL_DATA + os.sep) or resolved == REAL_KEY


# HPGBOOKS_TEST_WHMCS=off runs the suite with the WHMCS integration switched off
# (the WHMCS and margins test modules are skipped): nothing else may depend on it.
# HPBOOKS_TEST_WHMCS is the pre-rename name, still honoured (compat).
WHMCS_OFF = os.environ.get("HPGBOOKS_TEST_WHMCS", os.environ.get("HPBOOKS_TEST_WHMCS", "on")) == "off"
WHMCS_MODULES = ("test_whmcs.py", "test_whmcs_api.py", "test_whmcs_reports.py", "test_margins.py")


def pytest_collection_modifyitems(config, items):
    if not WHMCS_OFF:
        return
    skip = pytest.mark.skip(reason="HPGBOOKS_TEST_WHMCS=off")
    for item in items:
        if item.path.name in WHMCS_MODULES or "whmcs_on" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _test_config(monkeypatch):
    """Every test starts from the committed fake config; overrides do not leak."""
    monkeypatch.setenv("HPBOOKS_CONFIG", str(TEST_CONFIG))
    hpconfig.reset()
    if WHMCS_OFF:
        hpconfig.override(whmcs_enabled=False, margins_enabled=False)
    yield
    hpconfig.reset()


@pytest.fixture()
def whmcs_off():
    """The same fake config with the WHMCS integration (and margins) turned off."""
    return hpconfig.override(whmcs_enabled=False, margins_enabled=False)


@pytest.fixture(autouse=True)
def _never_the_real_database(monkeypatch):
    """Fail a test that would open the real ledger (data/hpbooks.db) or the real key."""
    real_connect = hpdb.sqlcipher3.connect

    def guarded_connect(path, *args, **kwargs):
        if _is_real(str(path)) or _is_real(hpdb.db_path()):
            pytest.fail(f"test tried to open the real database: {path}")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(hpdb.sqlcipher3, "connect", guarded_connect)
    original_load_key = hpdb.load_key

    def guarded_load_key():
        if _is_real(hpdb.db_path()):
            pytest.fail("test resolved db_path() to the real database")
        key_file = os.environ.get("HPBOOKS_KEY_FILE")
        if not os.environ.get("HPBOOKS_KEY") and (key_file is None or _is_real(key_file)):
            pytest.fail("test would read the real key file")
        return original_load_key()

    monkeypatch.setattr(hpdb, "load_key", guarded_load_key)
    yield
