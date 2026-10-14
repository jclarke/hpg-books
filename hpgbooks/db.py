"""SQLCipher storage, schema, and account seed for hpgbooks.

The database key is read from HPBOOKS_KEY or from a 0600 file. It is never
logged, returned, or interpolated into error messages.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import sqlcipher3

from hpgbooks.config import REPO_ROOT, get_config

DEFAULT_DB = str(REPO_ROOT / "data" / "hpbooks.db")
DEFAULT_KEY_FILE = os.path.expanduser("~/.config/hpbooks/key")
KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Business tags and categories come from the config file (config/local.toml).
# They are fixed for the life of the process: the CHECK constraints below are
# built from them when a database is first created.
_CONFIG = get_config()
BUSINESS_TAGS = _CONFIG.business_tags
CATEGORIES = _CONFIG.categories
REVENUE_CATEGORIES = _CONFIG.revenue_categories
COGS_CATEGORIES = _CONFIG.cogs_categories
OPEX_CATEGORIES = _CONFIG.opex_categories

# The business accounts `init` seeds, in display order. None by default;
# other accounts are registered from imports (`accounts discover`).
ACCOUNTS = tuple(
    {
        "id": acct.id,
        "name": acct.name,
        "type": acct.type,
        "last4": acct.last4,
        "institution": acct.institution,
        "notes": acct.notes,
    }
    for acct in _CONFIG.accounts
)

ACCOUNT_IDS = {a["id"] for a in ACCOUNTS}

_TAG_SQL = ", ".join(f"'{t}'" for t in BUSINESS_TAGS)
_CAT_SQL = ", ".join(f"'{c}'" for c in CATEGORIES)

SCHEMA_V1 = f"""
CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('cash', 'liability')),
  last4 TEXT,
  institution TEXT,
  notes TEXT
);

CREATE TABLE transactions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  date TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  direction TEXT,
  currency TEXT,
  name TEXT,
  merchant_name TEXT,
  description TEXT,
  pending INTEGER NOT NULL DEFAULT 0 CHECK (pending IN (0, 1)),
  provider_category TEXT,
  raw_json TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
  superseded_by TEXT REFERENCES transactions(id),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE rules (
  id INTEGER PRIMARY KEY,
  priority INTEGER NOT NULL,
  pattern TEXT NOT NULL,
  field TEXT NOT NULL DEFAULT 'any' CHECK (field IN ('name', 'merchant', 'any')),
  account_id TEXT REFERENCES accounts(id),
  amount_sign TEXT CHECK (amount_sign IN ('in', 'out')),
  min_amount REAL,
  max_amount REAL,
  business_tag TEXT NOT NULL CHECK (business_tag IN ({_TAG_SQL})),
  category TEXT NOT NULL CHECK (category IN ({_CAT_SQL})),
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  note TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL CHECK (created_by IN ('rule', 'manual', 'agent')),
  CHECK (
    (business_tag = 'owner_draw' AND category = 'Owner Draw')
    OR (business_tag = 'transfer' AND category = 'Transfer')
    OR (business_tag NOT IN ('owner_draw', 'transfer'))
  ),
  CHECK (business_tag != 'needs_review' OR confidence <= 0.5)
);

CREATE TABLE classifications (
  txn_id TEXT PRIMARY KEY REFERENCES transactions(id),
  business_tag TEXT NOT NULL CHECK (business_tag IN ({_TAG_SQL})),
  category TEXT NOT NULL CHECK (category IN ({_CAT_SQL})),
  source TEXT NOT NULL CHECK (source IN ('rule', 'manual', 'agent', 'seed')),
  rule_id INTEGER REFERENCES rules(id),
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  note TEXT,
  updated_at TEXT NOT NULL,
  CHECK (
    (business_tag = 'owner_draw' AND category = 'Owner Draw')
    OR (business_tag = 'transfer' AND category = 'Transfer')
    OR (business_tag NOT IN ('owner_draw', 'transfer'))
  ),
  CHECK (business_tag != 'needs_review' OR confidence <= 0.5)
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  txn_id TEXT,
  rule_id INTEGER,
  action TEXT NOT NULL,
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  actor TEXT,
  note TEXT
);

CREATE TABLE import_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  file TEXT,
  account_id TEXT,
  date_from TEXT,
  date_to TEXT,
  rows_in INTEGER,
  inserted INTEGER,
  updated INTEGER,
  unchanged INTEGER,
  superseded INTEGER,
  skipped INTEGER
);

CREATE INDEX idx_txn_account_date ON transactions(account_id, date);
CREATE INDEX idx_txn_status_date ON transactions(status, date);
CREATE INDEX idx_class_tag ON classifications(business_tag);
CREATE INDEX idx_rules_priority ON rules(active, priority, id);
"""

SCHEMA_V2 = """
ALTER TABLE transactions ADD COLUMN source TEXT NOT NULL DEFAULT 'finance-mcp';
"""

SCHEMA_V3 = """
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

# The web passphrase is a salted scrypt hash. The passphrase itself is never stored.
SCHEMA_V5 = """
CREATE TABLE web_passphrase (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  salt BLOB NOT NULL,
  hash BLOB NOT NULL,
  n INTEGER NOT NULL,
  r INTEGER NOT NULL,
  p INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
"""

# balance_cents: positive cash is money in the account; positive liability is amount owed.
# vendor_aliases maps a ledger spelling to a display name. Ledger rows are never rewritten.
SCHEMA_V4 = """
CREATE TABLE balance_anchors (
  id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  as_of_date TEXT NOT NULL,
  balance_cents INTEGER NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('finance', 'statement')),
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_balance_anchors_account ON balance_anchors(account_id, as_of_date, id);

CREATE TABLE vendor_aliases (
  alias_key TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL
);
CREATE INDEX idx_vendor_aliases_canonical ON vendor_aliases(canonical_name);
"""

# WHMCS billing copies, read from the brands' WHMCS databases. Customer names
# and emails live only here, inside the encrypted file. Amounts are cents.
# Every row is keyed by (brand, source_id); synced_at marks the run that last saw it.
SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS whmcs_clients (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  first_name TEXT,
  last_name TEXT,
  company TEXT,
  email TEXT,
  status TEXT,
  signup_date TEXT,
  country TEXT,
  state TEXT,
  currency TEXT,
  credit_cents INTEGER NOT NULL DEFAULT 0,
  default_gateway TEXT,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_products (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('product', 'addon')),
  name TEXT,
  group_name TEXT,
  type TEXT,
  pay_type TEXT,
  retired INTEGER NOT NULL DEFAULT 0,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, kind, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_services (
  brand TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('hosting', 'addon')),
  source_id INTEGER NOT NULL,
  client_id INTEGER,
  product_id INTEGER,
  parent_id INTEGER,
  addon_name TEXT,
  domain TEXT,
  status TEXT,
  billing_cycle TEXT,
  amount_cents INTEGER NOT NULL DEFAULT 0,
  first_payment_cents INTEGER NOT NULL DEFAULT 0,
  reg_date TEXT,
  next_due_date TEXT,
  termination_date TEXT,
  server_id INTEGER,
  payment_method TEXT,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, kind, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_invoices (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  client_id INTEGER,
  invoice_num TEXT,
  date TEXT,
  due_date TEXT,
  date_paid TEXT,
  date_refunded TEXT,
  date_cancelled TEXT,
  last_capture_attempt TEXT,
  subtotal_cents INTEGER NOT NULL DEFAULT 0,
  credit_cents INTEGER NOT NULL DEFAULT 0,
  tax_cents INTEGER NOT NULL DEFAULT 0,
  total_cents INTEGER NOT NULL DEFAULT 0,
  status TEXT,
  payment_method TEXT,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_invoice_items (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  invoice_id INTEGER,
  client_id INTEGER,
  type TEXT,
  rel_id INTEGER,
  amount_cents INTEGER NOT NULL DEFAULT 0,
  due_date TEXT,
  payment_method TEXT,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_payments (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  client_id INTEGER,
  date TEXT,
  ts TEXT,
  gateway TEXT,
  amount_in_cents INTEGER NOT NULL DEFAULT 0,
  fees_cents INTEGER NOT NULL DEFAULT 0,
  amount_out_cents INTEGER NOT NULL DEFAULT 0,
  trans_id TEXT,
  invoice_id INTEGER,
  refund_id INTEGER,
  is_refund INTEGER NOT NULL DEFAULT 0 CHECK (is_refund IN (0, 1)),
  currency TEXT,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_cancel_requests (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  date TEXT,
  service_id INTEGER,
  reason TEXT,
  type TEXT,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_credit (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  client_id INTEGER,
  date TEXT,
  amount_cents INTEGER NOT NULL DEFAULT 0,
  rel_id INTEGER,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, source_id)
);

CREATE TABLE IF NOT EXISTS whmcs_servers (
  brand TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  name TEXT,
  monthly_cost_cents INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (brand, source_id)
);

-- Share of each invoice that belongs to a plan, rebuilt on every sync from the
-- invoice items. Payments and refunds are split across plans with it.
CREATE TABLE IF NOT EXISTS whmcs_invoice_plans (
  brand TEXT NOT NULL,
  invoice_id INTEGER NOT NULL,
  plan TEXT NOT NULL,
  product_id INTEGER,
  share REAL NOT NULL,
  PRIMARY KEY (brand, invoice_id, plan)
);

CREATE TABLE IF NOT EXISTS whmcs_sync_log (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  brand TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
  counts_json TEXT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_whmcs_payments_date ON whmcs_payments(brand, date);
CREATE INDEX IF NOT EXISTS idx_whmcs_payments_invoice ON whmcs_payments(brand, invoice_id);
CREATE INDEX IF NOT EXISTS idx_whmcs_payments_client ON whmcs_payments(brand, client_id);
CREATE INDEX IF NOT EXISTS idx_whmcs_invoices_client ON whmcs_invoices(brand, client_id);
CREATE INDEX IF NOT EXISTS idx_whmcs_invoices_status ON whmcs_invoices(status, due_date);
CREATE INDEX IF NOT EXISTS idx_whmcs_items_invoice ON whmcs_invoice_items(brand, invoice_id);
CREATE INDEX IF NOT EXISTS idx_whmcs_services_client ON whmcs_services(brand, client_id);
CREATE INDEX IF NOT EXISTS idx_whmcs_cancel_service ON whmcs_cancel_requests(brand, service_id);
CREATE INDEX IF NOT EXISTS idx_whmcs_credit_client ON whmcs_credit(brand, client_id);
CREATE INDEX IF NOT EXISTS idx_whmcs_sync_log_brand ON whmcs_sync_log(brand, id);
"""

# Server margins: what each server costs and which WHMCS services it carries.
# Labels, mapping values (domains, service ids), and notes can identify customers,
# so they live only here. Revenue is never stored; it is read from whmcs_services.
# brand_scope '' means every brand. Amounts are cents per month.
SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS margin_servers (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  vendor TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('dedicated', 'cloud_vm', 'pool', 'overhead')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retire_candidate', 'retired')),
  location TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS margin_server_costs (
  id INTEGER PRIMARY KEY,
  server_id INTEGER NOT NULL REFERENCES margin_servers(id) ON DELETE CASCADE,
  component TEXT NOT NULL,
  monthly_cost_cents INTEGER NOT NULL CHECK (monthly_cost_cents >= 0),
  effective TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  UNIQUE (server_id, component)
);

CREATE TABLE IF NOT EXISTS margin_mappings (
  id INTEGER PRIMARY KEY,
  server_id INTEGER NOT NULL REFERENCES margin_servers(id) ON DELETE CASCADE,
  rule_type TEXT NOT NULL CHECK (rule_type IN ('service_id', 'domain', 'whmcs_server_id', 'product_group', 'brand')),
  rule_value TEXT NOT NULL,
  allocation TEXT NOT NULL DEFAULT 'direct' CHECK (allocation IN ('direct', 'by_revenue')),
  brand_scope TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (rule_type, rule_value, brand_scope)
);

CREATE TABLE IF NOT EXISTS margin_overhead (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL UNIQUE,
  vendor TEXT NOT NULL DEFAULT '',
  monthly_cost_cents INTEGER NOT NULL CHECK (monthly_cost_cents >= 0),
  kind TEXT NOT NULL DEFAULT 'shared' CHECK (kind IN ('shared', 'not_this_business')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS margin_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_margin_costs_server ON margin_server_costs(server_id);
CREATE INDEX IF NOT EXISTS idx_margin_mappings_server ON margin_mappings(server_id);
"""

# Account scope and presentation fields. Each ALTER runs only when its column is
# missing, so a partly migrated file and a fresh file both end in the same shape.
# `type` keeps its meaning (cash = asset balance, liability = amount owed);
# `class` is the finer grouping and is derived from `type` for existing rows.
ACCOUNT_SCOPES = ("business", "personal", "excluded")
ACCOUNT_CLASSES = ("cash", "liability", "investment", "loan", "other")
V8_COLUMNS = (
    ("scope", "ALTER TABLE accounts ADD COLUMN scope TEXT NOT NULL DEFAULT 'business' "
              "CHECK (scope IN ('business','personal','excluded'))"),
    ("class", "ALTER TABLE accounts ADD COLUMN class TEXT NOT NULL DEFAULT 'other' "
              "CHECK (class IN ('cash','liability','investment','loan','other'))"),
    ("include_in_net_worth", "ALTER TABLE accounts ADD COLUMN include_in_net_worth INTEGER NOT NULL DEFAULT 1 "
                             "CHECK (include_in_net_worth IN (0, 1))"),
    ("sync_enabled", "ALTER TABLE accounts ADD COLUMN sync_enabled INTEGER NOT NULL DEFAULT 1 "
                     "CHECK (sync_enabled IN (0, 1))"),
    ("display_name", "ALTER TABLE accounts ADD COLUMN display_name TEXT"),
    ("nickname", "ALTER TABLE accounts ADD COLUMN nickname TEXT"),
    ("closed", "ALTER TABLE accounts ADD COLUMN closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0, 1))"),
    ("subtype", "ALTER TABLE accounts ADD COLUMN subtype TEXT"),
    ("created_at", "ALTER TABLE accounts ADD COLUMN created_at TEXT"),
)
SCHEMA_V8 = ";\n".join(ddl for _col, ddl in V8_COLUMNS) + """;
UPDATE accounts SET class = CASE type WHEN 'liability' THEN 'liability' ELSE 'cash' END WHERE class = 'other';
CREATE INDEX IF NOT EXISTS idx_accounts_scope ON accounts(scope);
"""

# Personal finance tables (prefix p_). Kept apart from the business rules and
# classifications, whose CHECKs are tied to the Schedule C categories. Amounts
# are integer cents and follow the ledger sign: positive is money into the account.
SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS p_categories (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  group_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'transfer', 'funding')),
  icon TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1)),
  is_system INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),
  created_at TEXT NOT NULL,
  UNIQUE (group_name, name)
);

CREATE TABLE IF NOT EXISTS p_rules (
  id INTEGER PRIMARY KEY,
  priority INTEGER NOT NULL DEFAULT 50,
  pattern TEXT NOT NULL,
  field TEXT NOT NULL DEFAULT 'any' CHECK (field IN ('name', 'merchant', 'any')),
  account_id TEXT REFERENCES accounts(id),
  amount_sign TEXT CHECK (amount_sign IN ('in', 'out')),
  min_amount REAL,
  max_amount REAL,
  category_id INTEGER NOT NULL REFERENCES p_categories(id),
  merchant_rename TEXT,
  tags TEXT,
  confidence REAL NOT NULL DEFAULT 0.9 CHECK (confidence >= 0 AND confidence <= 1),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  note TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'seed' CHECK (created_by IN ('seed', 'manual', 'agent'))
);

CREATE TABLE IF NOT EXISTS p_classifications (
  txn_id TEXT PRIMARY KEY REFERENCES transactions(id),
  category_id INTEGER REFERENCES p_categories(id),
  source TEXT NOT NULL CHECK (source IN ('rule', 'manual', 'agent', 'transfer')),
  rule_id INTEGER REFERENCES p_rules(id),
  merchant_display TEXT,
  confidence REAL NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
  note TEXT,
  transfer_pair TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p_splits (
  id INTEGER PRIMARY KEY,
  txn_id TEXT NOT NULL REFERENCES transactions(id),
  category_id INTEGER NOT NULL REFERENCES p_categories(id),
  amount_cents INTEGER NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS p_merchants (
  key TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  default_category_id INTEGER REFERENCES p_categories(id),
  logo_hint TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p_tags (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS p_txn_tags (
  txn_id TEXT NOT NULL REFERENCES transactions(id),
  tag_id INTEGER NOT NULL REFERENCES p_tags(id) ON DELETE CASCADE,
  PRIMARY KEY (txn_id, tag_id)
);

CREATE TABLE IF NOT EXISTS p_budgets (
  id INTEGER PRIMARY KEY,
  category_id INTEGER REFERENCES p_categories(id),
  group_name TEXT,
  month TEXT,
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  rollover INTEGER NOT NULL DEFAULT 0 CHECK (rollover IN (0, 1)),
  updated_at TEXT NOT NULL,
  CHECK ((category_id IS NULL) != (group_name IS NULL))
);

CREATE TABLE IF NOT EXISTS p_goals (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  target_cents INTEGER NOT NULL CHECK (target_cents > 0),
  target_date TEXT,
  account_id TEXT REFERENCES accounts(id),
  manual_current_cents INTEGER NOT NULL DEFAULT 0,
  monthly_contribution_cents INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p_recurring (
  id INTEGER PRIMARY KEY,
  series_key TEXT NOT NULL UNIQUE,
  merchant_key TEXT NOT NULL,
  category_id INTEGER REFERENCES p_categories(id),
  cadence TEXT NOT NULL CHECK (cadence IN ('weekly', 'biweekly', 'monthly', 'quarterly', 'annual')),
  typical_amount_cents INTEGER NOT NULL DEFAULT 0,
  last_amount_cents INTEGER NOT NULL DEFAULT 0,
  last_date TEXT,
  next_expected_date TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('bill', 'subscription', 'income')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled', 'ignored', 'confirmed')),
  user_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (user_confirmed IN (0, 1)),
  cadence_locked INTEGER NOT NULL DEFAULT 0 CHECK (cadence_locked IN (0, 1)),
  first_seen TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p_transfer_reviews (
  pair_key TEXT PRIMARY KEY,
  decision TEXT NOT NULL CHECK (decision IN ('confirmed', 'rejected')),
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_p_class_category ON p_classifications(category_id);
CREATE INDEX IF NOT EXISTS idx_p_rules_priority ON p_rules(active, priority, id);
CREATE INDEX IF NOT EXISTS idx_p_splits_txn ON p_splits(txn_id);
CREATE INDEX IF NOT EXISTS idx_p_txn_tags_tag ON p_txn_tags(tag_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_p_budgets_key ON p_budgets(ifnull(category_id, -1), ifnull(group_name, ''), ifnull(month, ''));
CREATE INDEX IF NOT EXISTS idx_p_goals_account ON p_goals(account_id);
CREATE INDEX IF NOT EXISTS idx_p_recurring_status ON p_recurring(status, next_expected_date);
"""

# Minimum payment due, due date, APR, and where each came from, for credit cards and
# loans. One row per account; nothing here touches the ledger or balance anchors.
SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS account_payment_terms (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id),
  min_payment_cents INTEGER CHECK (min_payment_cents IS NULL OR min_payment_cents >= 0),
  due_date TEXT CHECK (due_date IS NULL OR due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  apr TEXT,
  source TEXT NOT NULL,
  as_of TEXT,
  autopay TEXT NOT NULL DEFAULT 'unknown' CHECK (autopay IN ('yes', 'no', 'unknown')),
  estimated INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
  unverified INTEGER NOT NULL DEFAULT 0 CHECK (unverified IN (0, 1)),
  statement_balance_cents INTEGER,
  paid_on TEXT,
  payer_pattern TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL
);
"""

# Personal loan offers saved to compare on the Debt payoff page. Standalone: no
# foreign keys, nothing in the ledger refers to it, rows stay until deleted.
SCHEMA_V11 = """
CREATE TABLE IF NOT EXISTS debt_offers (
  id INTEGER PRIMARY KEY,
  lender TEXT NOT NULL CHECK (length(trim(lender)) > 0),
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  apr REAL NOT NULL CHECK (apr >= 0 AND apr <= 100),
  fee_pct REAL NOT NULL DEFAULT 0 CHECK (fee_pct >= 0 AND fee_pct <= 10),
  fee_from_proceeds INTEGER NOT NULL DEFAULT 1 CHECK (fee_from_proceeds IN (0, 1)),
  term_months INTEGER NOT NULL CHECK (term_months BETWEEN 1 AND 360),
  monthly_payment_cents INTEGER CHECK (monthly_payment_cents IS NULL OR monthly_payment_cents > 0),
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'grok', 'import')),
  notes TEXT,
  expires_on TEXT CHECK (expires_on IS NULL OR expires_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

MIGRATIONS = (
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
    (3, SCHEMA_V3),
    (4, SCHEMA_V4),
    (5, SCHEMA_V5),
    (6, SCHEMA_V6),
    (7, SCHEMA_V7),
    (8, SCHEMA_V8),
    (9, SCHEMA_V9),
    (10, SCHEMA_V10),
    (11, SCHEMA_V11),
)

# Keys the web UI is allowed to write. Values are plain text, never secrets.
SETTING_KEYS = frozenset({"reserve_cents"})


class HpgbooksError(Exception):
    """User-facing error that should not dump a traceback."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_path() -> str:
    return os.environ.get("HPBOOKS_DB") or get_config().db_path or DEFAULT_DB


def load_key() -> str:
    """Return the 64-hex SQLCipher key. Never include the key in exceptions."""
    env = os.environ.get("HPBOOKS_KEY")
    if env is not None and env.strip() != "":
        key = env.strip()
        if not KEY_RE.fullmatch(key):
            raise HpgbooksError("HPBOOKS_KEY must be 64 hex characters")
        return key.lower()

    path = os.environ.get("HPBOOKS_KEY_FILE") or get_config().key_file or DEFAULT_KEY_FILE
    if not os.path.exists(path):
        raise HpgbooksError(f"database key file is missing: {path}")
    mode = os.stat(path).st_mode & 0o777
    # Looser than 600 means any group/other bit, or owner execute.
    if mode & ~0o600:
        raise HpgbooksError(
            f"database key file permissions are too open ({mode:03o}); require 600 or stricter"
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError as exc:
        raise HpgbooksError(f"could not read database key file: {path}") from exc
    if not KEY_RE.fullmatch(key):
        raise HpgbooksError("database key file must contain 64 hex characters")
    return key.lower()


def to_cents(amount) -> int:
    """Convert a decimal amount (string or number) to integer cents."""
    try:
        quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise HpgbooksError(f"invalid amount {amount!r}") from exc
    return int(quantized * 100)


def cents_to_dollars(cents: int) -> str:
    """Plain dollars with sign and two decimals, no thousands separator."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    dollars, rem = divmod(cents, 100)
    return f"{sign}{dollars}.{rem:02d}"


def format_money(cents: int) -> str:
    """Dollars with thousands separators for terminal and PDF display."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    dollars, rem = divmod(cents, 100)
    return f"{sign}{dollars:,}.{rem:02d}"


def tighten_storage_permissions(path: str | None = None) -> None:
    """Make the DB file 0600 and its directory 0700.

    A shared sticky directory such as /tmp is left alone so a database path
    placed directly in it cannot lock other users out of the system temp dir.
    """
    target = Path(path or db_path())
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    mode = parent.stat().st_mode
    sticky_shared = bool(mode & 0o1000) and bool(mode & 0o002)
    if not sticky_shared:
        os.chmod(parent, 0o700)
    if target.exists():
        os.chmod(target, 0o600)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            os.chmod(sidecar, 0o600)


@contextmanager
def connect(readonly: bool = False):
    path = db_path()
    tighten_storage_permissions(path)
    key = load_key()
    conn = sqlcipher3.connect(path)
    try:
        # Hex is validated to [0-9a-f]{64} before it is placed in the PRAGMA.
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        del key
        conn.row_factory = sqlcipher3.Row
        if readonly:
            # query_only before any other pragma so a read cannot rewrite the file.
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA foreign_keys = ON")
        else:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = DELETE")
            _apply_migrations(conn)
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
        tighten_storage_permissions(path)


def _apply_migrations(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    have = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    for version, sql in MIGRATIONS:
        if version in have:
            continue
        if version == 2:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "transactions" in tables:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
                if "source" in cols:
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (version, now_iso()),
                    )
                    continue
        if version == 8:
            _migrate_v8(conn)
        else:
            conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, now_iso()),
        )


def _migrate_v8(conn) -> None:
    """Add the account scope columns that are missing. Ledger rows are not touched."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    for column, ddl in V8_COLUMNS:
        if column not in cols:
            conn.execute(ddl)
    if "class" not in cols:
        conn.execute(
            "UPDATE accounts SET class = CASE type WHEN 'liability' THEN 'liability' ELSE 'cash' END"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_scope ON accounts(scope)")


def seed_accounts(conn) -> None:
    """Insert or refresh the business accounts listed in the config.

    Only the seed's own columns are refreshed. scope, class, display name,
    nickname, and the net-worth/sync flags are user settings and are never reset.
    """
    for acct in ACCOUNTS:
        conn.execute(
            """
            INSERT INTO accounts (id, name, type, last4, institution, notes, scope, class, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'business', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              type = excluded.type,
              last4 = excluded.last4,
              institution = excluded.institution,
              notes = excluded.notes
            """,
            (
                acct["id"],
                acct["name"],
                acct["type"],
                acct["last4"],
                acct["institution"],
                acct["notes"],
                "liability" if acct["type"] == "liability" else "cash",
                now_iso(),
            ),
        )


def init_db() -> str:
    """Create the database, apply migrations, seed accounts and rules."""
    from hpgbooks.seed_rules import seed_rules

    with connect() as conn:
        _apply_migrations(conn)
        # Confirm the key unlocked the database (wrong key fails this read).
        conn.execute("SELECT version FROM schema_version").fetchall()
        seed_accounts(conn)
        added, skipped = seed_rules(conn)
        from hpgbooks.personal.seed import seed_personal

        seed_personal(conn)
    return f"initialized {db_path()} (rules added {added}, already present {skipped})"


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    """Read a settings value. Missing table (older read-only DB) returns default."""
    if key not in SETTING_KEYS:
        raise HpgbooksError("unknown setting")
    if not _table_exists(conn, "settings"):
        return default
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(conn, key: str, value: str, *, actor: str = "web") -> None:
    """Insert or update a setting and write the audit log when the value changes."""
    if key not in SETTING_KEYS:
        raise HpgbooksError("unknown setting")
    if len(value) > 64:
        raise HpgbooksError("setting value is too long")
    old = get_setting(conn, key)
    if old == value:
        return
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value = excluded.value,
          updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )
    audit(
        conn,
        "setting",
        field=key,
        old_value=old,
        new_value=value,
        actor=actor,
    )


def audit(
    conn,
    action: str,
    *,
    txn_id: str | None = None,
    rule_id: int | None = None,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    actor: str = "cli",
    note: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (ts, txn_id, rule_id, action, field, old_value, new_value, actor, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), txn_id, rule_id, action, field, old_value, new_value, actor, note),
    )


def account_name_map(conn) -> dict[str, str]:
    return {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM accounts")}


def short_account(name: str) -> str:
    """Short display name from the config (e.g. a bank name and last 4), else the name."""
    return get_config().short_account(name)


def short_id(txn_id: str | None) -> str:
    """Display prefix that stays unique enough to paste into `classify`."""
    txn_id = txn_id or ""
    return txn_id[:15] if txn_id.startswith("capone:") else txn_id[:8]
