"""Starter personal categories and rules. Both seeds are idempotent and editable.

Rules name only public merchants and generic bank wording. Categories are
matched by (group, name), rules by (pattern, field, sign), so a re-seed adds
what is missing and never changes a row the user edited or disabled.
"""

from __future__ import annotations

from hpgbooks.db import now_iso

# group, kind, [names]; a group with no names is a single category of that name.
CATEGORY_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Income", "income", ("Paycheck", "Interest & dividends", "Refunds & reimbursements", "Other income")),
    ("Housing", "expense", ("Mortgage", "Rent", "HOA", "Home maintenance", "Property tax")),
    ("Utilities", "expense", ("Electric", "Gas & water", "Internet & cable", "Phone", "Trash")),
    ("Food", "expense", ("Groceries", "Dining out", "Coffee", "Alcohol")),
    ("Transportation", "expense", ("Gas & fuel", "Auto payment", "Auto insurance", "Parking & tolls", "Rideshare", "Auto maintenance")),
    ("Insurance", "expense", ("Home insurance", "Health insurance", "Life insurance")),
    ("Health", "expense", ("Doctor", "Pharmacy", "Dental", "Fitness")),
    ("Kids", "expense", ("Childcare", "School", "Activities", "Kids clothing")),
    ("Shopping", "expense", ("General", "Clothing", "Electronics", "Home goods")),
    ("Travel", "expense", ()),
    ("Entertainment", "expense", ()),
    ("Subscriptions", "expense", ()),
    ("Personal care", "expense", ()),
    ("Pets", "expense", ()),
    ("Gifts & donations", "expense", ()),
    ("Education", "expense", ()),
    ("Fees & interest", "expense", ()),
    ("Taxes", "expense", ()),
    ("Cash & ATM", "expense", ()),
    ("Transfers", "transfer", ("Credit card payment", "Savings transfer", "Loan payment", "Internal transfer")),
)
OWNER_DRAWS = ("Income", "Owner draws")
UNCATEGORIZED = ("Uncategorized", "Uncategorized")
COLORS = {
    "Income": "#15803d",
    "Housing": "#7c3aed",
    "Utilities": "#0369a1",
    "Food": "#c2410c",
    "Transportation": "#0f766e",
    "Insurance": "#4338ca",
    "Health": "#be123c",
    "Kids": "#a16207",
    "Shopping": "#9333ea",
    "Transfers": "#64748b",
    "Uncategorized": "#94a3b8",
}


def seed_categories(conn) -> int:
    added = 0
    sort = 0
    ts = now_iso()

    def insert(group: str, name: str, kind: str, system: int) -> None:
        nonlocal added, sort
        sort += 10
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO p_categories (name, group_name, kind, color, sort, is_system, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, group, kind, COLORS.get(group, ""), sort, system, ts),
        )
        added += cur.rowcount

    for group, kind, names in CATEGORY_GROUPS:
        for name in names or (group,):
            insert(group, name, kind, 0)
        if group == "Income":
            # Money the business sends to personal accounts. Funding, not earned income.
            insert(OWNER_DRAWS[0], OWNER_DRAWS[1], "funding", 1)
    insert(UNCATEGORIZED[0], UNCATEGORIZED[1], "expense", 1)
    return added


# pattern, category (group, name), sign or None, priority, confidence, note
STARTER_RULES: tuple[tuple, ...] = (
    # Income
    (r"PAYROLL|DIRECT DEP|DIR DEP|SALARY", ("Income", "Paycheck"), "in", 20, 0.9, "payroll / direct deposit"),
    (r"INTEREST (PAYMENT|PAID|EARNED)|INTEREST$|DIVIDEND", ("Income", "Interest & dividends"), "in", 20, 0.9, "interest and dividends"),
    (r"REFUND|RETURN CREDIT|REIMBURSE", ("Income", "Refunds & reimbursements"), "in", 30, 0.7, "refund"),
    # Transfers between personal accounts (the detector pairs the two legs)
    (r"PAYMENT THANK YOU|PAYMENT - THANK YOU|AUTOPAY PAYMENT|CARD PAYMENT|CRD PAYMENT|CREDIT CRD.*EPAY", ("Transfers", "Credit card payment"), None, 15, 0.85, "card payment"),
    (r"TRANSFER (TO|FROM) SAVINGS|TO SAVINGS|FROM SAVINGS|TRANSFER FROM CHECKING|TRANSFER TO CHECKING", ("Transfers", "Savings transfer"), None, 15, 0.85, "savings move"),
    (r"BROKERAGE TRANSFER|CONTRIBUTION FROM", ("Transfers", "Internal transfer"), None, 16, 0.8, "brokerage move"),
    (r"ACH PMT.*THANK YOU", ("Transfers", "Credit card payment"), "in", 15, 0.85, "card payment received (store card statements)"),
    (r"ACH DEPOSIT INTERNET TRANSFER|APPLE CASH PAYMENT", ("Transfers", "Credit card payment"), "in", 15, 0.85, "Apple Card payment received"),
    (r"PAYMENT RECEIVED", ("Transfers", "Loan payment"), "in", 16, 0.8, "loan payment received"),
    # Housing and loans
    (r"MORTGAGE|MTG PMT|MTG PYMT|HOME LOAN", ("Housing", "Mortgage"), "out", 20, 0.9, "mortgage"),
    (r"\bRENT\b|APARTMENTS", ("Housing", "Rent"), "out", 25, 0.7, "rent"),
    (r"\bHOA\b|HOMEOWNERS ASSOC", ("Housing", "HOA"), "out", 25, 0.85, "HOA"),
    (r"HOME DEPOT|LOWE'?S", ("Housing", "Home maintenance"), "out", 30, 0.8, "home improvement"),
    (r"AUTO PMT|AUTO LOAN|AUTO FINANCE", ("Transportation", "Auto payment"), "out", 20, 0.85, "auto loan"),
    # Utilities
    (r"DUKE ENERGY|DOMINION ENERGY|GEORGIA POWER|ELECTRIC", ("Utilities", "Electric"), "out", 30, 0.9, "electric"),
    (r"WATER|PIEDMONT NATURAL GAS|NATURAL GAS", ("Utilities", "Gas & water"), "out", 35, 0.75, "gas and water"),
    (r"COMCAST|XFINITY|SPECTRUM|AT&T U-VERSE|GOOGLE FIBER", ("Utilities", "Internet & cable"), "out", 30, 0.9, "internet and cable"),
    (r"VERIZON|T-MOBILE|AT&T|\bATT\b|CRICKET", ("Utilities", "Phone"), "out", 31, 0.85, "phone"),
    (r"WASTE MANAGEMENT|REPUBLIC SERVICES", ("Utilities", "Trash"), "out", 30, 0.85, "trash"),
    # Food
    (r"KROGER|PUBLIX|TRADER JOE|WHOLE FOODS|\bALDI\b|FOOD LION|HARRIS TEETER|INSTACART|SAFEWAY|WEGMANS|SPROUTS", ("Food", "Groceries"), "out", 30, 0.9, "groceries"),
    (r"STARBUCKS|DUNKIN|PEET'?S|DUTCH BROS", ("Food", "Coffee"), "out", 30, 0.9, "coffee"),
    (r"MCDONALD|CHIPOTLE|DOORDASH|GRUBHUB|UBER\s*EATS|PANERA|CHICK-FIL-A|TACO BELL|WENDY'?S|SUBWAY|DOMINO'?S|PIZZA", ("Food", "Dining out"), "out", 31, 0.85, "restaurants and delivery"),
    (r"TOTAL WINE|ABC STORE|LIQUOR", ("Food", "Alcohol"), "out", 30, 0.8, "alcohol"),
    # Transportation
    (r"\bSHELL\b|EXXON|MOBIL|CHEVRON|\bBP\b|WAWA|SHEETZ|QUIKTRIP|\bQT\b|CIRCLE K|SUNOCO|MARATHON|SPEEDWAY", ("Transportation", "Gas & fuel"), "out", 32, 0.85, "fuel"),
    (r"\bUBER\b|\bLYFT\b", ("Transportation", "Rideshare"), "out", 33, 0.8, "rideshare"),
    (r"PARKING|E-?ZPASS|SUNPASS|TOLL", ("Transportation", "Parking & tolls"), "out", 30, 0.8, "parking and tolls"),
    (r"JIFFY LUBE|FIRESTONE|MIDAS|DISCOUNT TIRE|AUTOZONE|O'?REILLY", ("Transportation", "Auto maintenance"), "out", 30, 0.8, "auto service"),
    (r"GEICO|PROGRESSIVE|STATE FARM|ALLSTATE|LIBERTY MUTUAL", ("Transportation", "Auto insurance"), "out", 30, 0.75, "auto insurance"),
    # Health
    (r"\bCVS\b|WALGREENS|RITE AID", ("Health", "Pharmacy"), "out", 30, 0.8, "pharmacy"),
    (r"PLANET FITNESS|\bYMCA\b|ORANGETHEORY|PELOTON", ("Health", "Fitness"), "out", 30, 0.85, "fitness"),
    # Subscriptions and entertainment
    (r"NETFLIX|SPOTIFY|\bHULU\b|DISNEY\s*\+|DISNEY\s*PLUS|YOUTUBE|MAX\.COM|HBO|PARAMOUNT\+|PEACOCK|APPLE\.COM/BILL|AUDIBLE|SIRIUSXM", ("Subscriptions", "Subscriptions"), "out", 25, 0.9, "streaming and subscriptions"),
    (r"AMC THEATRES|REGAL|TICKETMASTER|STEAM GAMES|PLAYSTATION|NINTENDO|XBOX", ("Entertainment", "Entertainment"), "out", 30, 0.8, "entertainment"),
    # Shopping
    (r"COSTCO|SAM'?S CLUB", ("Food", "Groceries"), "out", 34, 0.7, "warehouse club"),
    (r"AMAZON|AMZN|WALMART|WAL-MART|TARGET", ("Shopping", "General"), "out", 35, 0.7, "general merchandise"),
    (r"BEST BUY|APPLE STORE", ("Shopping", "Electronics"), "out", 35, 0.75, "electronics"),
    (r"OLD NAVY|\bGAP\b|NORDSTROM|\bTJ ?MAXX\b|MARSHALLS|ROSS STORES", ("Shopping", "Clothing"), "out", 35, 0.75, "clothing"),
    (r"IKEA|BED BATH|WAYFAIR|HOMEGOODS", ("Shopping", "Home goods"), "out", 35, 0.75, "home goods"),
    # Common chains and services seen on card statements (public names only)
    (r"PATREON|GOOGLE \*YT", ("Subscriptions", "Subscriptions"), "out", 26, 0.85, "Patreon memberships"),
    (r"ARBY'?S|BURGER KING|\bKFC\b|HARDEE'?S|DAIRY QUEEN|SONIC DRIVE|JERSEY MIKE|LITTLE CAESARS|PAPA JOHN|BOJANGLES|SMOOTHIE KING|WHICH WICH|MOE'?S \d{3,}|COOK ?OUT|KRISPY KREME|BUFFALO WILD", ("Food", "Dining out"), "out", 31, 0.8, "fast food and restaurant chains"),
    (r"MURPHY ?(EXPRESS|\d{4})|BUC-?EE'?S", ("Transportation", "Gas & fuel"), "out", 32, 0.8, "fuel stops"),
    (r"DOLLAR TREE|DOLLAR[- ]GENERAL", ("Shopping", "General"), "out", 35, 0.75, "dollar stores"),
    (r"PEDIATRIC DENT|DENTAL|DENTIST|ORTHODON", ("Health", "Dental"), "out", 30, 0.8, "dental"),
    (r"GOFUNDME|GOFNDME|GO FUND ME", ("Gifts & donations", "Gifts & donations"), "out", 30, 0.8, "fundraisers"),
    # Other groups
    (r"DELTA AIR|AMERICAN AIRLINES|UNITED AIRLINES|SOUTHWEST|AIRBNB|MARRIOTT|HILTON|EXPEDIA", ("Travel", "Travel"), "out", 30, 0.8, "travel"),
    (r"PETSMART|PETCO|CHEWY|VETERINAR", ("Pets", "Pets"), "out", 30, 0.85, "pets"),
    (r"GREAT CLIPS|SALON|BARBER", ("Personal care", "Personal care"), "out", 30, 0.75, "personal care"),
    (r"ATM WITHDRAWAL|ATM WDL|CASH WITHDRAWAL", ("Cash & ATM", "Cash & ATM"), "out", 25, 0.85, "cash"),
    (r"LATE FEE|OVERDRAFT|FOREIGN TRANSACTION FEE|INTEREST CHARGE|ANNUAL FEE", ("Fees & interest", "Fees & interest"), "out", 25, 0.85, "bank fees"),
    (r"IRS USATAXPYMT|STATE TAX|DEPT OF REVENUE", ("Taxes", "Taxes"), "out", 25, 0.85, "taxes"),
    # Person-to-person apps carry no category of their own: placeholders for review.
    (r"VENMO|ZELLE|CASH APP|SQUARE CASH", UNCATEGORIZED, None, 40, 0.3, "person-to-person; review"),
)


def category_id(conn, group: str, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM p_categories WHERE group_name = ? AND name = ?", (group, name)
    ).fetchone()
    if row is None:
        raise KeyError(f"{group}/{name}")
    return int(row[0])


def seed_personal_rules(conn) -> tuple[int, int]:
    """Insert missing starter rules. Returns (added, already present)."""
    seed_categories(conn)
    added = present = 0
    ts = now_iso()
    for pattern, (group, name), sign, priority, confidence, note in STARTER_RULES:
        exists = conn.execute(
            "SELECT 1 FROM p_rules WHERE pattern = ? AND field = 'any' AND ifnull(amount_sign, '') = ? AND account_id IS NULL",
            (pattern, sign or ""),
        ).fetchone()
        if exists:
            present += 1
            continue
        conn.execute(
            """
            INSERT INTO p_rules (priority, pattern, field, amount_sign, category_id, confidence, active, note, created_at, created_by)
            VALUES (?, ?, 'any', ?, ?, ?, 1, ?, ?, 'seed')
            """,
            (priority, pattern, sign, category_id(conn, group, name), confidence, note, ts),
        )
        added += 1
    return added, present


def seed_personal(conn) -> tuple[int, int]:
    seed_categories(conn)
    return seed_personal_rules(conn)


def ensure_seeded(conn) -> None:
    """Categories must exist before anything is classified. Rules are seeded on init."""
    row = conn.execute("SELECT COUNT(*) FROM p_categories").fetchone()
    if not row or int(row[0]) == 0:
        seed_personal(conn)
