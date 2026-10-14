"""Site configuration: product name, businesses, categories, accounts, rules, WHMCS.

The file is TOML. It is read from $HPBOOKS_CONFIG when that is set, else from
config/local.toml in the repository when it exists, else built-in generic
defaults are used. config/config.example.toml documents every key.

Business tags, categories, and seeded accounts are read once per process: the
database CHECK constraints and module tables are built from them at import. A
change to those needs a restart (and a fresh database for new tags or
categories). Feature flags and WHMCS settings are read on each call through
get_config(), so tests can switch them with override().
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = REPO_ROOT / "config" / "local.toml"

# Tags and categories every install has. The business tags and the P&L
# category lists come from the config file.
SYSTEM_TAGS = ("owner_draw", "transfer", "needs_review")
REFUNDS = "Refunds"
OTHER_CATEGORIES = ("Owner Draw", "Transfer", "Uncategorized")

DEFAULT_CATEGORIES = {
    "revenue": ["Revenue - Sales", "Revenue - Services"],
    "cogs": ["Cost of Goods Sold"],
    "opex": [
        "Software & Licenses",
        "Payroll",
        "Contractors",
        "Professional Services",
        "Advertising",
        "Bank & Card Fees",
        "Payment Processing Fees",
        "Interest",
        "Insurance",
        "Rent",
        "Utilities",
        "Travel",
        "Meals",
        "Taxes & Licenses",
        "Office/Other",
    ],
}

DEFAULT_MONARCH_ALIASES = {"apple card": "apple card", "apple cash": "apple cash"}

DEFAULT_BUSINESSES = [
    {"slug": "general", "label": "General", "kind": "overhead", "color": "#64748b", "color_dark": "#94a3b8"},
]

ACCOUNT_TYPES = ("cash", "liability")
_TONE_BY_KIND = {"brand": "brand", "consulting": "violet", "overhead": "neutral"}
PAIR_WINDOWS = ("abs7", "after0_7")


class ConfigError(Exception):
    """The config file is unreadable or has an invalid value."""


@dataclass(frozen=True)
class Business:
    slug: str
    label: str
    kind: str = "business"
    color: str = ""
    color_dark: str = ""
    tone: str = ""
    revenue_category: str | None = None


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    type: str
    last4: str | None = None
    institution: str | None = None
    notes: str | None = None
    short_name: str | None = None
    short_name_prefix: str | None = None
    label: str | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransferPair:
    kind: str
    left_account: str
    left_pattern: str
    right_account: str
    right_pattern: str
    window: str = "abs7"
    classify_left: bool = False
    matched_note: str = "matched to transfer {txn_id}"
    unmatched_note: str = "transfer not matched to the other account"


@dataclass(frozen=True)
class WhmcsBrand:
    name: str
    server: str
    database: str = "whmcs"
    port: int = 3307
    secret: str = "whmcs.secret"


@dataclass(frozen=True)
class BankSide:
    """A books account compared with one WHMCS gateway in the gateway totals."""

    gateway: str
    account_role: str
    label: str
    match: str = ""


@dataclass(frozen=True)
class WhmcsConfig:
    ssh_user: str = "hpgbooks"
    ssh_key: str = "~/.ssh/hpbooks_whmcs"
    mysql_user: str = "books_ro"
    placeholder_credit_cents: int = 100_000_000
    revenue_category: str = ""  # empty: the first revenue category
    paypal_account_role: str = "paypal"
    brands: tuple[WhmcsBrand, ...] = ()
    bank_sides: tuple[BankSide, ...] = ()


@dataclass(frozen=True)
class PayerHint:
    """Where to look for a loan's payments: bank rows matching `pattern` pay the
    personal loan whose last 4 equals `match`, or whose name contains it."""

    match: str
    pattern: str


@dataclass(frozen=True)
class ImportersConfig:
    applecard_account_id: str = "manual-apple-card"
    cfna_last4: str | None = None
    monarch_aliases: dict = field(default_factory=lambda: dict(DEFAULT_MONARCH_ALIASES))


@dataclass(frozen=True)
class UpdateConfig:
    """Where `hpgbooks update check|apply` looks. A git remote named `remote` wins; else `repo` on GitHub."""

    repo: str = "jclarke/hpg-books"
    remote: str = "public"
    branch: str = "main"


@dataclass(frozen=True)
class Config:
    source: str
    base_dir: Path
    product_name: str = "Books"
    company_name: str = "My Business"
    wordmark: str = "Books"
    db_path: str | None = None
    key_file: str | None = None
    whmcs_enabled: bool = False
    margins_enabled: bool = False
    business_enabled: bool = True
    personal_enabled: bool = True
    businesses: tuple[Business, ...] = ()
    revenue_categories: tuple[str, ...] = ()
    cogs_categories: tuple[str, ...] = ()
    opex_categories: tuple[str, ...] = ()
    accounts: tuple[Account, ...] = ()
    transfer_pairs: tuple[TransferPair, ...] = ()
    rules_file: Path | None = None
    starter_rules: bool = True
    prior_csv: str | None = None
    prior_business: str | None = None
    prior_status: str = "business"
    prior_category_map: dict = field(default_factory=dict)
    whmcs: WhmcsConfig = field(default_factory=WhmcsConfig)
    payer_hints: tuple[PayerHint, ...] = ()
    importers: ImportersConfig = field(default_factory=ImportersConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)

    # --- derived -----------------------------------------------------------

    @property
    def business_slugs(self) -> tuple[str, ...]:
        return tuple(b.slug for b in self.businesses)

    @property
    def business_tags(self) -> tuple[str, ...]:
        return self.business_slugs + SYSTEM_TAGS

    @property
    def categories(self) -> tuple[str, ...]:
        return (
            self.revenue_categories
            + (REFUNDS,)
            + self.cogs_categories
            + self.opex_categories
            + OTHER_CATEGORIES
        )

    @property
    def business_filter_error(self) -> str:
        choices = ("all",) + self.business_slugs
        if len(choices) == 2:
            text = " or ".join(choices)
        else:
            text = ", ".join(choices[:-1]) + ", or " + choices[-1]
        return f"business must be {text}"

    @property
    def default_business(self) -> str:
        """The overhead business: where unassigned costs go. First 'overhead' kind, else the last business."""
        for business in self.businesses:
            if business.kind == "overhead":
                return business.slug
        return self.businesses[-1].slug

    def business(self, slug: str) -> Business | None:
        for business in self.businesses:
            if business.slug == slug:
                return business
        return None

    def account_for_role(self, role: str) -> str | None:
        """Id of the first configured account with this role, or None."""
        for account in self.accounts:
            if role in account.roles:
                return account.id
        return None

    def resolve_account(self, ref: str | None) -> str | None:
        """An account id from a config reference: an account id or a role name."""
        if not ref:
            return None
        for account in self.accounts:
            if account.id == ref:
                return account.id
        return self.account_for_role(ref)

    def short_account(self, name: str) -> str:
        """Short display name for an account name: exact name, then name prefix, else the name."""
        for account in self.accounts:
            if account.short_name and account.name == name:
                return account.short_name
        for account in self.accounts:
            if account.short_name and account.short_name_prefix and name.startswith(account.short_name_prefix):
                return account.short_name
        return name

    def whmcs_brand_names(self) -> tuple[str, ...]:
        return tuple(brand.name for brand in self.whmcs.brands)

    def public_payload(self, *, detail: bool) -> dict:
        """What the web UI needs to brand itself and hide disabled features."""
        out = {
            "product": self.product_name,
            "company": self.company_name,
            "wordmark": self.wordmark,
            "features": {
                "whmcs": self.whmcs_enabled,
                "margins": self.margins_enabled,
                "business": self.business_enabled,
                "personal": self.personal_enabled,
            },
        }
        if detail:
            out["businesses"] = [
                {
                    "slug": b.slug,
                    "label": b.label,
                    "kind": b.kind,
                    "color": b.color,
                    "color_dark": b.color_dark or b.color,
                    "tone": b.tone or _TONE_BY_KIND.get(b.kind, "neutral"),
                    "revenue_category": b.revenue_category,
                }
                for b in self.businesses
            ]
            out["default_business"] = self.default_business
            out["accounts"] = [
                {
                    "id": a.id,
                    "short_name": a.short_name or a.name,
                    "label": a.label or a.institution or a.short_name or a.name,
                    "type": a.type,
                    "roles": list(a.roles),
                }
                for a in self.accounts
            ]
            operating = self.account_for_role("operating")
            out["operating_account"] = next(
                (item for item in out["accounts"] if item["id"] == operating), None
            )
            whmcs = self.whmcs_enabled
            out["whmcs_brands"] = list(self.whmcs_brand_names()) if whmcs else []
            out["whmcs_bank_sides"] = (
                [{"gateway": s.gateway, "label": s.label} for s in self.whmcs.bank_sides] if whmcs else []
            )
        return out


# --- loading -----------------------------------------------------------------


def config_path() -> Path | None:
    """The file that would be loaded, or None for built-in defaults."""
    env = os.environ.get("HPBOOKS_CONFIG")
    if env is not None and env.strip() != "":
        path = Path(env).expanduser()
        return path if path.exists() else None
    return LOCAL_CONFIG if LOCAL_CONFIG.exists() else None


def _get(table: dict, key: str, kind, default, where: str):
    value = table.get(key, default)
    if value is None or value is default:
        return value
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"{where}.{key} must be {getattr(kind, '__name__', kind)}")
    return value


def _str_or_none(table: dict, key: str, where: str) -> str | None:
    value = table.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be text")
    return value


def _str_list(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where} must be a list of names")
    return tuple(value)


def _businesses(raw) -> tuple[Business, ...]:
    if raw is None:
        raw = DEFAULT_BUSINESSES
    if not isinstance(raw, list) or not raw:
        raise ConfigError("businesses must be a non-empty list of [[businesses]] tables")
    out = []
    for index, item in enumerate(raw):
        where = f"businesses[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a table")
        slug = _get(item, "slug", str, None, where)
        if not slug or not slug.replace("_", "").isalnum() or slug != slug.lower():
            raise ConfigError(f"{where}.slug must be lowercase letters, digits, or _")
        if slug in SYSTEM_TAGS or slug == "all":
            raise ConfigError(f"{where}.slug {slug!r} is reserved")
        out.append(
            Business(
                slug=slug,
                label=_get(item, "label", str, slug, where),
                kind=_get(item, "kind", str, "business", where),
                color=_get(item, "color", str, "", where),
                color_dark=_get(item, "color_dark", str, "", where),
                tone=_get(item, "tone", str, "", where),
                revenue_category=_str_or_none(item, "revenue_category", where),
            )
        )
    if len({b.slug for b in out}) != len(out):
        raise ConfigError("businesses have a repeated slug")
    return tuple(out)


def _accounts(raw) -> tuple[Account, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("accounts.business must be a list of [[accounts.business]] tables")
    out = []
    for index, item in enumerate(raw):
        where = f"accounts.business[{index}]"
        acct_id = _get(item, "id", str, None, where)
        name = _get(item, "name", str, None, where)
        if not acct_id or not name:
            raise ConfigError(f"{where} needs id and name")
        acct_type = _get(item, "type", str, "cash", where)
        if acct_type not in ACCOUNT_TYPES:
            raise ConfigError(f"{where}.type must be cash or liability")
        roles = item.get("roles", [])
        if isinstance(roles, str):
            roles = [roles]
        out.append(
            Account(
                id=acct_id,
                name=name,
                type=acct_type,
                last4=_str_or_none(item, "last4", where),
                institution=_str_or_none(item, "institution", where),
                notes=_str_or_none(item, "notes", where),
                short_name=_str_or_none(item, "short_name", where),
                short_name_prefix=_str_or_none(item, "short_name_prefix", where),
                label=_str_or_none(item, "label", where),
                roles=_str_list(roles, f"{where}.roles") if roles else (),
            )
        )
    if len({a.id for a in out}) != len(out):
        raise ConfigError("accounts.business has a repeated id")
    return tuple(out)


def _pairs(raw) -> tuple[TransferPair, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("transfer_pairs must be a list of [[transfer_pairs]] tables")
    out = []
    for index, item in enumerate(raw):
        where = f"transfer_pairs[{index}]"
        values = {}
        for key in ("kind", "left_account", "left_pattern", "right_account", "right_pattern"):
            value = _get(item, key, str, None, where)
            if not value:
                raise ConfigError(f"{where}.{key} is required")
            values[key] = value
        window = _get(item, "window", str, "abs7", where)
        if window not in PAIR_WINDOWS:
            raise ConfigError(f"{where}.window must be one of {', '.join(PAIR_WINDOWS)}")
        out.append(
            TransferPair(
                **values,
                window=window,
                classify_left=_get(item, "classify_left", bool, False, where),
                matched_note=_get(item, "matched_note", str, TransferPair.matched_note, where),
                unmatched_note=_get(item, "unmatched_note", str, TransferPair.unmatched_note, where),
            )
        )
    return tuple(out)


def _whmcs(raw) -> WhmcsConfig:
    raw = raw or {}
    where = "whmcs"
    brands = []
    for index, item in enumerate(raw.get("brands", []) or []):
        bw = f"whmcs.brands[{index}]"
        name = _get(item, "name", str, None, bw)
        server = _get(item, "server", str, None, bw)
        if not name or not server:
            raise ConfigError(f"{bw} needs name and server")
        brands.append(
            WhmcsBrand(
                name=name,
                server=server,
                database=_get(item, "database", str, "whmcs", bw),
                port=_get(item, "port", int, 3307, bw),
                secret=_get(item, "secret", str, WhmcsBrand.secret, bw),
            )
        )
    if len({b.name.lower() for b in brands}) != len(brands):
        raise ConfigError("whmcs.brands has a repeated name")
    sides = []
    for index, item in enumerate(raw.get("bank_sides", []) or []):
        sw = f"whmcs.bank_sides[{index}]"
        gateway = _get(item, "gateway", str, None, sw)
        role = _get(item, "account_role", str, None, sw)
        if not gateway or not role:
            raise ConfigError(f"{sw} needs gateway and account_role")
        sides.append(
            BankSide(
                gateway=gateway.lower(),
                account_role=role,
                label=_get(item, "label", str, gateway, sw),
                match=_get(item, "match", str, "", sw),
            )
        )
    if "bank_sides" not in raw:
        sides = [BankSide("paypal", "paypal", "PayPal account, deposits less refunds")]
    defaults = WhmcsConfig()
    return WhmcsConfig(
        ssh_user=_get(raw, "ssh_user", str, defaults.ssh_user, where),
        ssh_key=_get(raw, "ssh_key", str, defaults.ssh_key, where),
        mysql_user=_get(raw, "mysql_user", str, defaults.mysql_user, where),
        placeholder_credit_cents=_get(raw, "placeholder_credit_cents", int, defaults.placeholder_credit_cents, where),
        revenue_category=_get(raw, "revenue_category", str, defaults.revenue_category, where),
        paypal_account_role=_get(raw, "paypal_account_role", str, defaults.paypal_account_role, where),
        brands=tuple(brands),
        bank_sides=tuple(sides),
    )


def _payer_hints(raw) -> tuple[PayerHint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("payments.payer_hints must be a list of [[payments.payer_hints]] tables")
    out = []
    for index, item in enumerate(raw):
        where = f"payments.payer_hints[{index}]"
        match = _get(item, "match", str, None, where)
        pattern = _get(item, "pattern", str, None, where)
        if not match or not pattern:
            raise ConfigError(f"{where} needs match and pattern")
        out.append(PayerHint(match=match, pattern=pattern))
    return tuple(out)


def _importers(raw) -> ImportersConfig:
    raw = raw or {}
    where = "importers"
    defaults = ImportersConfig()
    last4 = _str_or_none(raw, "cfna_last4", where)
    if last4 is not None and not (len(last4) == 4 and last4.isdigit()):
        raise ConfigError("importers.cfna_last4 must be 4 digits")
    aliases = raw.get("monarch_aliases", defaults.monarch_aliases)
    if not isinstance(aliases, dict) or not all(isinstance(v, str) for v in aliases.values()):
        raise ConfigError("importers.monarch_aliases must be a table of names")
    return ImportersConfig(
        applecard_account_id=_get(raw, "applecard_account_id", str, defaults.applecard_account_id, where)
        or defaults.applecard_account_id,
        cfna_last4=last4,
        monarch_aliases={str(k): v for k, v in aliases.items()},
    )


def _update(raw) -> UpdateConfig:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("update must be a table")
    defaults = UpdateConfig()
    out = UpdateConfig(
        repo=_get(raw, "repo", str, defaults.repo, "update") or defaults.repo,
        remote=_get(raw, "remote", str, defaults.remote, "update") or defaults.remote,
        branch=_get(raw, "branch", str, defaults.branch, "update") or defaults.branch,
    )
    if out.repo.count("/") != 1 or not all(out.repo.split("/")):
        raise ConfigError("update.repo must be owner/name")
    return out


def build(data: dict, *, source: str = "defaults", base_dir: Path | None = None) -> Config:
    """A Config from parsed TOML (an empty dict gives the generic defaults)."""
    base_dir = base_dir or REPO_ROOT
    app = data.get("app", {}) or {}
    features = data.get("features", {}) or {}
    paths = data.get("paths", {}) or {}
    cats = data.get("categories", {}) or {}
    seed = data.get("seed", {}) or {}
    accounts = (data.get("accounts", {}) or {}).get("business")

    product = _get(app, "product_name", str, "Books", "app")
    whmcs_on = _get(features, "whmcs", bool, False, "features")
    margins_on = _get(features, "margins", bool, whmcs_on, "features") and whmcs_on
    business_on = _get(features, "business", bool, True, "features")
    personal_on = _get(features, "personal", bool, True, "features")
    if not business_on and not personal_on:
        raise ConfigError("features.business and features.personal cannot both be false")

    revenue = _str_list(cats.get("revenue", DEFAULT_CATEGORIES["revenue"]), "categories.revenue")
    cogs = _str_list(cats.get("cogs", DEFAULT_CATEGORIES["cogs"]), "categories.cogs") if cats.get("cogs", DEFAULT_CATEGORIES["cogs"]) else ()
    opex = _str_list(cats.get("opex", DEFAULT_CATEGORIES["opex"]), "categories.opex")
    if not revenue:
        raise ConfigError("categories.revenue needs at least one category")
    reserved = {REFUNDS, *OTHER_CATEGORIES}
    every = list(revenue) + list(cogs) + list(opex)
    if reserved & set(every):
        raise ConfigError(f"categories may not list {', '.join(sorted(reserved))}; they are built in")
    if len(set(every)) != len(every):
        raise ConfigError("categories have a repeated name")
    for name in every:
        if "'" in name:
            raise ConfigError(f"category {name!r} may not contain a quote")

    businesses = _businesses(data.get("businesses"))
    for business in businesses:
        if business.revenue_category and business.revenue_category not in revenue:
            raise ConfigError(f"businesses.{business.slug}.revenue_category is not a revenue category")

    whmcs = _whmcs(data.get("whmcs"))
    if not whmcs.revenue_category:
        whmcs = replace(whmcs, revenue_category=revenue[0])
    elif whmcs_on and whmcs.revenue_category not in revenue:
        raise ConfigError("whmcs.revenue_category is not a revenue category")

    rules_file = _str_or_none(seed, "rules_file", "seed")
    prior_map = seed.get("prior_category_map", {}) or {}
    if not isinstance(prior_map, dict):
        raise ConfigError("seed.prior_category_map must be a table")

    def _path(value: str | None) -> str | None:
        if not value:
            return None
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else REPO_ROOT / path)

    return Config(
        source=source,
        base_dir=base_dir,
        product_name=product,
        company_name=_get(app, "company_name", str, "My Business", "app"),
        wordmark=_get(app, "wordmark", str, product, "app"),
        db_path=_path(_str_or_none(paths, "db", "paths")),
        key_file=_path(_str_or_none(paths, "key_file", "paths")),
        whmcs_enabled=whmcs_on,
        margins_enabled=margins_on,
        business_enabled=business_on,
        personal_enabled=personal_on,
        businesses=businesses,
        revenue_categories=revenue,
        cogs_categories=cogs,
        opex_categories=opex,
        accounts=_accounts(accounts),
        transfer_pairs=_pairs(data.get("transfer_pairs")),
        rules_file=(base_dir / rules_file) if rules_file else None,
        starter_rules=_get(seed, "starter_rules", bool, True, "seed"),
        prior_csv=_str_or_none(seed, "prior_csv", "seed"),
        prior_business=_str_or_none(seed, "prior_business", "seed"),
        prior_status=_get(seed, "prior_status", str, "business", "seed"),
        prior_category_map={str(k): str(v) for k, v in prior_map.items()},
        whmcs=whmcs,
        payer_hints=_payer_hints((data.get("payments", {}) or {}).get("payer_hints")),
        importers=_importers(data.get("importers")),
        update=_update(data.get("update")),
    )


def warn_if_exposed(path: Path) -> bool:
    """Warn on stderr when a site config file is readable by group or others. True if it warned.

    The committed example and the test fixtures hold no private data and are skipped.
    """
    resolved = path.resolve()
    if resolved.name == "config.example.toml" or resolved.is_relative_to(REPO_ROOT / "tests"):
        return False
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if not mode & (stat.S_IRWXG | stat.S_IRWXO):
        return False
    print(
        f"hpgbooks: warning: {path} is readable by other users (mode {stat.S_IMODE(mode):o}); "
        f"run: chmod 600 {path}",
        file=sys.stderr,
    )
    return True


def load(path: Path | None) -> Config:
    if path is None:
        return build({})
    warn_if_exposed(path)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}") from exc
    return build(data, source=str(path), base_dir=path.parent)


_cached: Config | None = None


def get_config() -> Config:
    global _cached
    if _cached is None:
        _cached = load(config_path())
    return _cached


def reset() -> None:
    """Forget the cached config; the next get_config() reads the file again."""
    global _cached
    _cached = None


def override(**changes) -> Config:
    """Replace fields of the cached config (tests). Returns the new config."""
    global _cached
    _cached = replace(get_config(), **changes)
    return _cached


def whmcs_enabled() -> bool:
    return get_config().whmcs_enabled


def margins_enabled() -> bool:
    return get_config().margins_enabled


BUSINESS_DISABLED = "business mode is disabled (set features.business = true in config/local.toml)"
PERSONAL_DISABLED = "personal mode is disabled (set features.personal = true in config/local.toml)"
WHMCS_DISABLED = "WHMCS integration is disabled (set features.whmcs = true in config/local.toml)"


def load_rules_file(path: Path) -> list[tuple]:
    """Seed rules from a JSON list of objects, in file order, as seed_rules tuples."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read seed rules file {path}") from exc
    if isinstance(data, dict):
        data = data.get("rules")
    if not isinstance(data, list):
        raise ConfigError(f"{path} must hold a list of rules")
    out = []
    for index, item in enumerate(data):
        where = f"{path.name}[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be an object")
        try:
            out.append(
                (
                    int(item["priority"]),
                    str(item["pattern"]),
                    str(item.get("field", "any")),
                    item.get("account"),
                    item.get("sign"),
                    str(item["tag"]),
                    str(item["category"]),
                    float(item["confidence"]),
                    str(item.get("note", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{where} is missing priority, pattern, tag, category, or confidence") from exc
    return out
