#!/usr/bin/env bash
# Privacy scan: fail if a tree that is about to be published contains personal data.
#
# usage: scripts/privacy_scan.sh [--allow-missing-local-lists] [--worktree] [DIR]
#
#   (no DIR)      scan the tracked tree of git HEAD (git archive HEAD)
#   --worktree    scan the working tree's tracked + untracked-not-ignored files
#   DIR           scan DIR as it is
#
# Checks (each finding prints as `file:line: [check] snippet`):
#   terms / ids / last4   local deny lists, read from ${HPGBOOKS_PRIVACY_DIR:-<repo>/config}:
#                           privacy-terms.local.txt  case-insensitive ripgrep regexes, # comments
#                           privacy-ids.local.txt    fixed strings (account / transaction ids)
#                           privacy-last4.local.txt  4-digit numbers, flagged next to account
#                                                    words or in a mask (x1234, *1234, ...1234)
#                         These files are gitignored and never committed. Missing lists exit 3
#                         unless --allow-missing-local-lists is passed.
#   email, phone, ipv4, tailnet, token, hex64, home-path   built-in generic checks
#   file                  files that must never be published (databases, keys, local config, ...)
#   gitleaks / trufflehog run when installed (trufflehog without live verification)
#
# scripts/privacy_allowlist.txt (or $HPGBOOKS_PRIVACY_ALLOWLIST) suppresses intentional false
# positives: `regex` lines matched against the text around a finding, and `file:<glob>` lines
# that allow an image or PDF.
#
# Exit: 0 clean, 1 findings, 2 usage error, 3 local deny lists missing.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOW_MISSING=0
MODE=head
TARGET=""
while (($#)); do
  case "$1" in
    --allow-missing-local-lists) ALLOW_MISSING=1 ;;
    --worktree) MODE=worktree ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    -*) echo "privacy_scan: unknown option $1" >&2; exit 2 ;;
    *) TARGET="$1"; MODE=dir ;;
  esac
  shift
done

command -v rg >/dev/null || { echo "privacy_scan: ripgrep (rg) is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "privacy_scan: python3 is required" >&2; exit 2; }

PRIV="${HPGBOOKS_PRIVACY_DIR:-${HPBOOKS_PRIVACY_DIR:-$ROOT/config}}"
TERMS="$PRIV/privacy-terms.local.txt"
IDS="$PRIV/privacy-ids.local.txt"
LAST4="$PRIV/privacy-last4.local.txt"
missing=()
for f in "$TERMS" "$IDS" "$LAST4"; do [[ -f "$f" ]] || missing+=("$f"); done
if ((${#missing[@]})); then
  {
    echo "################################################################"
    echo "privacy_scan: WARNING: local deny lists missing:"
    printf '    %s\n' "${missing[@]}"
    echo "Personal names, ids, and account numbers will NOT be checked."
    echo "Set HPGBOOKS_PRIVACY_DIR or create the files (see scripts/privacy_scan.sh)."
    echo "################################################################"
  } >&2
  ((ALLOW_MISSING)) || exit 3
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
case "$MODE" in
  head)
    mkdir "$TMP/tree"
    git -C "$ROOT" archive HEAD | tar -x -C "$TMP/tree" || { echo "privacy_scan: git archive failed" >&2; exit 2; }
    TREE="$TMP/tree" ;;
  worktree)
    mkdir "$TMP/tree"
    (cd "$ROOT" && git ls-files -z -c -o --exclude-standard | while IFS= read -r -d '' f; do
       [[ -e "$f" || -L "$f" ]] && printf '%s\0' "$f"; done | tar --null -T - -c) | tar -x -C "$TMP/tree" \
      || { echo "privacy_scan: worktree export failed" >&2; exit 2; }
    TREE="$TMP/tree" ;;
  dir)
    [[ -d "$TARGET" ]] || { echo "privacy_scan: no such directory: $TARGET" >&2; exit 2; }
    TREE="$(cd "$TARGET" && pwd)" ;;
esac

# Secret scanners write JSON reports that the checker below folds into its findings.
EXTERNAL="$TMP/external"
mkdir "$EXTERNAL"
if command -v gitleaks >/dev/null; then
  gitleaks dir "$TREE" --no-banner --redact --log-level error --exit-code 0 \
    --report-format json --report-path "$EXTERNAL/gitleaks.json" >/dev/null 2>"$TMP/gitleaks.err" \
    || { echo "privacy_scan: gitleaks failed:" >&2; cat "$TMP/gitleaks.err" >&2; exit 2; }
else
  echo "privacy_scan: warning: gitleaks not installed, skipping" >&2
fi
if command -v trufflehog >/dev/null; then
  # --no-verification: never send candidate secrets to third-party APIs.
  trufflehog filesystem "$TREE" --no-update --no-verification --json --log-level=-1 \
    >"$EXTERNAL/trufflehog.jsonl" 2>"$TMP/trufflehog.err" \
    || { echo "privacy_scan: trufflehog failed:" >&2; cat "$TMP/trufflehog.err" >&2; exit 2; }
else
  echo "privacy_scan: warning: trufflehog not installed, skipping" >&2
fi

python3 - "$TREE" "$TERMS" "$IDS" "$LAST4" "${HPGBOOKS_PRIVACY_ALLOWLIST:-${HPBOOKS_PRIVACY_ALLOWLIST:-$ROOT/scripts/privacy_allowlist.txt}}" "$EXTERNAL" <<'PY'
import fnmatch
import json
import os
import re
import subprocess
import sys

tree, terms_path, ids_path, last4_path, allow_path, external = sys.argv[1:7]
SNIP = 160  # max characters of context printed per finding
findings = []  # (relpath, line, check, context)


def read_list(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            s = raw.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def context(text, start, end):
    """The matched text with some surrounding characters, on one line."""
    if len(text) <= SNIP:
        return text.strip()
    pad = max(20, (SNIP - (end - start)) // 2)
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def add(rel, line, check, text, start=0, end=0):
    findings.append((rel, line, check, context(text, start, end)))


def rg_matches(args):
    """Yield (relpath, line_number, line_text, start, end) for every rg submatch."""
    cmd = ["rg", "--json", "--hidden", "--no-ignore", "--no-messages", *args, "--", "."]
    proc = subprocess.run(cmd, cwd=tree, capture_output=True, text=True)
    if proc.returncode > 1:
        sys.exit("privacy_scan: rg failed: " + proc.stderr.strip())
    for raw in proc.stdout.splitlines():
        msg = json.loads(raw)
        if msg.get("type") != "match":
            continue
        d = msg["data"]
        path = d["path"].get("text", "")
        line = d["lines"].get("text", "").rstrip("\n")
        rel = os.path.normpath(path)
        b = line.encode()
        for sm in d["submatches"]:
            # rg offsets are in bytes
            s = len(b[: sm["start"]].decode(errors="ignore"))
            e = len(b[: sm["end"]].decode(errors="ignore"))
            yield rel, d["line_number"], line, s, e


def pattern_file(name, lines):
    p = os.path.join(external, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return p


# --- a. local deny lists --------------------------------------------------------------
terms = read_list(terms_path)
if terms:
    for rel, n, line, s, e in rg_matches(["-i", "-f", pattern_file("terms.rx", terms)]):
        add(rel, n, "terms", line, s, e)

ids = read_list(ids_path)
if ids:
    for rel, n, line, s, e in rg_matches(["-F", "-f", pattern_file("ids.txt", ids)]):
        add(rel, n, "ids", line, s, e)

last4 = sorted({tok for row in read_list(last4_path) for tok in row.split() if re.fullmatch(r"\d{4}", tok)})
if last4:
    alt = "|".join(last4)
    mask = rf"(?:\b[xX]{{1,4}}|[*•#]+|…|\.\.\.)\s?(?:{alt})\b"
    for rel, n, line, s, e in rg_matches(["-e", mask]):
        add(rel, n, "last4-mask", line, s, e)
    acct_ctx = re.compile(
        r"account|acct|card|last_?4|mask|checking|savings|ending|credit|debit|bank|amex|visa|"
        r"mastercard|capital ?one|chase|bofa|paypal|short_name|register|"
        r"(?:\bx{1,2}|[•*#])\d{4}\b",
        re.I,
    )
    seen = set()
    for rel, n, line, s, e in rg_matches(["-e", rf"\b(?:{alt})\b"]):
        # account words within 80 characters of the number (minified bundles have huge lines)
        if acct_ctx.search(line[max(0, s - 80): e + 80]) and (rel, n) not in seen:
            seen.add((rel, n))
            add(rel, n, "last4", line, s, e)

# --- b. generic checks ------------------------------------------------------------------
OK_EMAIL = re.compile(
    r"@(?:[\w.-]+\.)?(?:example\.(?:com|org|net)|[\w-]+\.(?:example|test|invalid|localhost)|"
    r"users\.noreply\.github\.com)$",
    re.I,
)


def ip_ok(m):
    o = [int(x) for x in m.groups()]
    if any(x > 255 for x in o):
        return True  # not an address
    if o[0] == 127 or o == [0, 0, 0, 0]:
        return True
    if o[:3] in ([192, 0, 2], [198, 51, 100], [203, 0, 113]):
        return True
    return False


GENERIC = [
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
     lambda m: bool(OK_EMAIL.search(m.group(0)))),
    ("phone", r"(?<![\w.-])(?:\+?1[-. ])?(?:\([2-9]\d{2}\) ?|[2-9]\d{2}[-.])[2-9]\d{2}[-.]\d{4}(?![\w-])", None),
    ("ipv4", r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)", ip_ok),
    ("tailnet", r"\bts\.net\b", None),
    ("token", r"\b(?:gh[opsur]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|"
              r"xox[abpr]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16})\b", None),
    ("token", r"-{5}BEGIN [A-Z ]*PRIVATE KEY", None),
    ("token", r"\b[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}=*", None),
    ("hex64", r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])", None),
    ("home-path", r"/(?:home|Users)/[a-z][a-z0-9_-]*/", None),
]
for check, rx, ok in GENERIC:
    crx = re.compile(rx)
    # PCRE2 for look-arounds; Python re re-checks each match for the ok() filter
    for rel, n, line, s, e in rg_matches(["-P", "-e", rx]):
        m = crx.match(line, s)
        if ok and m and ok(m):
            continue
        add(rel, n, check, line, s, e)

# --- c. forbidden files ---------------------------------------------------------------
FORBIDDEN = [
    "data/*", "*/data/*", "exports/*", "*/exports/*", "sync/inbox/*", "config/local.toml",
    "*.local.*", "*.db", "*.db-*", "*.db.bak*", "*.sqlite*", "*.key", "*.secret", "*.pem",
    "id_rsa*", "*/id_rsa*", ".env*", "*/.env*", "*.log", "*.pid",
]
IMAGES = ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.heic", "*.pdf")
allow_rx, allow_files = [], []
for entry in read_list(allow_path):
    if entry.startswith("file:"):
        allow_files.append(entry[5:].strip())
    else:
        allow_rx.append(re.compile(entry))

for dirpath, dirnames, filenames in os.walk(tree):
    for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
        rel = os.path.relpath(os.path.join(dirpath, name), tree)
        low = rel.lower()
        if any(fnmatch.fnmatch(low, g) or fnmatch.fnmatch(name.lower(), g) for g in FORBIDDEN):
            findings.append((rel, 0, "file", "file must not be published"))
        elif any(fnmatch.fnmatch(low, g) for g in IMAGES) and not any(
            fnmatch.fnmatch(rel, g) for g in allow_files
        ):
            findings.append((rel, 0, "file", "image/PDF not in scripts/privacy_allowlist.txt"))

# --- d. external secret scanners ------------------------------------------------------
gl = os.path.join(external, "gitleaks.json")
if os.path.isfile(gl) and os.path.getsize(gl):
    for f in json.load(open(gl)) or []:
        rel = os.path.relpath(f.get("File", ""), tree) if os.path.isabs(f.get("File", "")) else f.get("File", "")
        findings.append((rel, f.get("StartLine", 0), "gitleaks:" + f.get("RuleID", "?"),
                         (f.get("Match") or f.get("Description") or "")[:SNIP]))
th = os.path.join(external, "trufflehog.jsonl")
if os.path.isfile(th):
    for raw in open(th):
        try:
            f = json.loads(raw)
        except ValueError:
            continue
        fs = ((f.get("SourceMetadata") or {}).get("Data") or {}).get("Filesystem") or {}
        if not fs:
            continue
        path = fs.get("file", "")
        rel = os.path.relpath(path, tree) if os.path.isabs(path) else path
        findings.append((rel, fs.get("line", 0), "trufflehog:" + str(f.get("DetectorName", "?")),
                         "(redacted) " + str(f.get("Redacted") or "")[:40]))

# --- e. allowlist + report ------------------------------------------------------------
shown = sorted({f for f in findings if not any(r.search(f[3]) for r in allow_rx)})
for rel, n, check, snippet in shown:
    print(f"{rel}:{n}: [{check}] {snippet}")
print(f"privacy_scan: {len(shown)} finding(s) in {tree}", file=sys.stderr)
sys.exit(1 if shown else 0)
PY
exit $?
