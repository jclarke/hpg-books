"""Initial classification rules. Loaded idempotently by pattern + field + account + sign.

A fresh install gets a small set of generic starter rules (payouts, card and
bank fees, interest, common SaaS, tax payments, card payments, ads, insurance,
travel) written for the default categories. A site can add
its own list, with account ids and business tags, in a JSON file named by
`[seed] rules_file` in the config; `starter_rules = false` drops the starters.

Two rules may share a pattern but differ by amount sign, so the idempotency
key includes amount_sign. Otherwise a re-seed would keep only the inflow rule
and book processor fees as revenue.
"""

from __future__ import annotations

import re

from hpgbooks.config import ConfigError, get_config, load_rules_file
from hpgbooks.db import BUSINESS_TAGS, CATEGORIES, HpgbooksError, audit, now_iso

# priority, pattern, field, account_id, amount_sign, tag, category, confidence, note
RuleSpec = tuple

# Placeholders resolved against the config: BUSINESS is the overhead business,
# REVENUE its revenue category (or the first one). A starter rule whose category the
# site does not have is skipped.
BUSINESS = "{business}"
REVENUE = "{revenue}"


def _r(
    priority: int,
    pattern: str,
    tag: str,
    category: str,
    confidence: float,
    note: str = "",
    field: str = "any",
    account: str | None = None,
    sign: str | None = None,
) -> tuple:
    return (priority, pattern, field, account, sign, tag, category, confidence, note)


STARTER_RULES: list[tuple] = [
    # --- card payments received (the bank side is paired by transfer matching) ---
    _r(10, r"PAYMENT\s*-?\s*THANK YOU|AUTOPAY PAYMENT", "transfer", "Transfer", 0.9,
       "card payment received", sign="in"),

    # --- payment processor payouts ---
    _r(15, r"STRIPE.*(TRANSFER|PAYOUT)", BUSINESS, REVENUE, 0.9, "Stripe payout", sign="in"),
    _r(15, r"\bSQUARE\b.*(DEPOSIT|TRANSFER|PAYOUT)|\bSQ DEPOSIT", BUSINESS, REVENUE, 0.9, "Square payout", sign="in"),
    _r(16, r"(?i)(^fee\b|paypal fee|stripe fee|transaction fee|processing fee)", BUSINESS, "Payment Processing Fees", 0.85,
       "payment processor fee", sign="out"),

    # --- bank and card fees, interest ---
    _r(26, r"MONTHLY (SERVICE |MAINTENANCE )?FEE|SERVICE CHARGE|OVERDRAFT|WIRE (TRANSFER )?FEE|FOREIGN TRANSACTION FEE",
       BUSINESS, "Bank & Card Fees", 0.85, "bank fee", sign="out"),
    _r(26, r"ANNUAL (MEMBERSHIP )?FEE", BUSINESS, "Bank & Card Fees", 0.85, "card annual fee", sign="out"),
    _r(27, r"INTEREST CHARGE|PURCHASE INTEREST|INTEREST CHARGED", BUSINESS, "Interest", 0.9, "card interest", sign="out"),

    # --- payroll and taxes ---
    _r(25, r"\bGUSTO\b|\bPAYCHEX\b|\bADP\b.*PAYROLL|INTUIT.*PAYROLL", BUSINESS, "Payroll", 0.9, "payroll provider", sign="out"),
    _r(33, r"IRS.*USATAXPYMT|IRS TREAS", BUSINESS, "Taxes & Licenses", 0.8, "IRS payment", sign="out"),
    _r(33, r"(DEPT|DEPARTMENT) (OF )?REVENUE|FRANCHISE TAX|SECRETARY OF STATE", BUSINESS, "Taxes & Licenses", 0.8,
       "state tax or filing fee", sign="out"),

    # --- common software and services ---
    _r(40, r"DIGITALOCEAN|\bLINODE\b|\bVULTR\b|\bHETZNER\b|^AWS$|AMAZON WEB SERVICES|AWS\.AMAZON", BUSINESS,
       "Software & Licenses", 0.85, "cloud services"),
    _r(45, r"\bGITHUB\b", BUSINESS, "Software & Licenses", 0.9, "GitHub"),
    _r(45, r"GOOGLE\s*\*?\s*(WORKSPACE|GSUITE)|\bGSUITE\b", BUSINESS, "Software & Licenses", 0.9, "Google Workspace"),
    _r(45, r"MICROSOFT\s*\*?\s*(365|OFFICE)", BUSINESS, "Software & Licenses", 0.85, "Microsoft 365"),
    _r(45, r"\bSLACK\b|\bZOOM\.US\b|\bDROPBOX\b|\bADOBE\b|\b1PASSWORD\b", BUSINESS, "Software & Licenses", 0.8,
       "common SaaS"),
    _r(45, r"QUICKBOOKS|\bQBOOKS\b|\bXERO\b", BUSINESS, "Software & Licenses", 0.9, "accounting software"),
    _r(48, r"NAMECHEAP|GODADDY|CLOUDFLARE|SQUARESPACE|\bWIX\.COM\b", BUSINESS, "Software & Licenses", 0.85,
       "domains and websites"),
    _r(50, r"OPENAI|CHATGPT|ANTHROPIC|CLAUDE\.AI", BUSINESS, "Software & Licenses", 0.85, "AI tools"),
    _r(52, r"GOOGLE\s*\*?\s*ADS|FACEBK|META ADS|LINKEDIN ADS", BUSINESS, "Advertising", 0.85, "online ads"),
    _r(55, r"\bINSURANCE\b|\bINS PREM", BUSINESS, "Insurance", 0.7, "insurance premium", sign="out"),
    _r(56, r"AIRLINES|\bAIRBNB\b|\bMARRIOTT\b|\bHILTON\b|\bLYFT\b|\bUBER\b(?!\s*\*?\s*EATS)", BUSINESS, "Travel", 0.7,
       "travel", sign="out"),
]


def _resolve(spec: tuple) -> tuple | None:
    """A starter rule with its placeholders filled in, or None if the config lacks its category."""
    cfg = get_config()
    business = cfg.default_business
    revenue = (cfg.business(business).revenue_category if cfg.business(business) else None) or cfg.revenue_categories[0]
    priority, pattern, field, account, sign, tag, category, confidence, note = spec
    tag = business if tag == BUSINESS else tag
    category = revenue if category == REVENUE else category
    if category not in CATEGORIES:
        return None
    return (priority, pattern, field, account, sign, tag, category, confidence, note)


def rule_specs() -> list[tuple]:
    """Seed rules for this site: the starters (unless turned off), then the config's rules file."""
    cfg = get_config()
    specs: list[tuple] = []
    if cfg.starter_rules:
        specs.extend(rule for rule in map(_resolve, STARTER_RULES) if rule is not None)
    if cfg.rules_file is not None:
        try:
            specs.extend(load_rules_file(cfg.rules_file))
        except ConfigError as exc:
            raise HpgbooksError(str(exc)) from exc
    for spec in specs:
        _check(spec)
    return specs


def _check(spec: tuple) -> None:
    priority, pattern, field, account, sign, tag, category, confidence, note = spec
    where = f"seed rule {pattern!r}"
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise HpgbooksError(f"{where}: bad pattern ({exc})") from exc
    if tag not in BUSINESS_TAGS:
        raise HpgbooksError(f"{where}: unknown business tag {tag!r}")
    if category not in CATEGORIES:
        raise HpgbooksError(f"{where}: unknown category {category!r}")
    if field not in ("name", "merchant", "any") or sign not in (None, "in", "out"):
        raise HpgbooksError(f"{where}: field must be name/merchant/any and sign in/out")


def _compile_all() -> None:
    for spec in STARTER_RULES:
        re.compile(spec[1], re.IGNORECASE)


_compile_all()


def seed_rules(conn, actor: str = "init") -> tuple[int, int]:
    """Insert any missing seed rule. Returns (added, already_present)."""
    added = 0
    present = 0
    for priority, pattern, field, account, sign, tag, category, confidence, note in rule_specs():
        existing = conn.execute(
            """
            SELECT id FROM rules
            WHERE pattern = ? AND field = ? AND ifnull(account_id, '') = ifnull(?, '')
              AND ifnull(amount_sign, '') = ifnull(?, '')
            """,
            (pattern, field, account, sign),
        ).fetchone()
        if existing:
            present += 1
            continue
        cur = conn.execute(
            """
            INSERT INTO rules (
              priority, pattern, field, account_id, amount_sign, min_amount, max_amount,
              business_tag, category, confidence, note, active, created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, 1, ?, 'rule')
            """,
            (priority, pattern, field, account, sign, tag, category, confidence, note, now_iso()),
        )
        audit(
            conn,
            "rule_create",
            rule_id=cur.lastrowid,
            field="pattern",
            new_value=pattern,
            actor=actor,
            note=f"seed {tag} / {category}",
        )
        added += 1
    return added, present
