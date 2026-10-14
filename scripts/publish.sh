#!/usr/bin/env bash
# Publish a privacy-scanned snapshot of this repository to a second remote whose history
# never contains the private history: each publish is one new commit holding the exported
# tree, whose only parent is the previous published commit (the first one has none).
#
# usage: scripts/publish.sh [--push] [--remote NAME] [--branch NAME] [--message TEXT]
#                           [--ref REF] [--export-only DIR]
#
#   (default)          dry run: export, scan, build the commit, show what changed; push nothing
#   --push             push the new commit to <remote> <branch> (never forced)
#   --remote NAME      remote to publish to (default: public)
#   --branch NAME      branch on that remote (default: main)
#   --message TEXT     commit message (default: "Publish snapshot YYYY-MM-DD")
#   --ref REF          commit to publish (default: HEAD, which must have no uncommitted
#                      changes to tracked files)
#   --export-only DIR  export and scan into DIR (must not exist or be empty); no commit
#
# The tree is `git archive <ref>`, scanned by scripts/privacy_scan.sh with the local deny
# lists (${HPGBOOKS_PRIVACY_DIR:-config}/privacy-*.local.txt). Any finding, or a missing
# deny list, aborts. The author and committer are PUBLISH_AUTHOR_NAME / PUBLISH_AUTHOR_EMAIL,
# else `git config publish.name` / `publish.email`; with neither set the script refuses, so
# your everyday git identity is never written into the public history.
#
# Exit: 0 published, nothing to publish, or dry run done; 1 scan findings or a git error;
#       2 usage or setup error; 3 local deny lists missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUSH=0
REMOTE=public
BRANCH=main
MESSAGE=""
REF=""
EXPORT_DIR=""

die() { echo "publish: $*" >&2; exit 2; }
need_arg() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }

while (($#)); do
  case "$1" in
    --push) PUSH=1 ;;
    --remote) need_arg "$@"; REMOTE="$2"; shift ;;
    --branch) need_arg "$@"; BRANCH="$2"; shift ;;
    --message) need_arg "$@"; MESSAGE="$2"; shift ;;
    --ref) need_arg "$@"; REF="$2"; shift ;;
    --export-only) need_arg "$@"; EXPORT_DIR="$2"; shift ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) die "unknown argument $1 (see --help)" ;;
  esac
  shift
done
[[ -n "$EXPORT_DIR" && $PUSH -eq 1 ]] && die "--export-only and --push cannot be combined"

cd "$ROOT"
git rev-parse --git-dir >/dev/null 2>&1 || die "$ROOT is not a git repository"
GIT_DIR_ABS="$(cd "$(git rev-parse --git-dir)" && pwd)"

# --- what to publish ------------------------------------------------------------------
if [[ -z "$REF" ]]; then
  REF=HEAD
  git update-index -q --refresh >/dev/null 2>&1 || true
  if ! git diff --quiet HEAD -- || ! git diff --cached --quiet --; then
    die "tracked files have uncommitted changes; commit them, or pass --ref to publish a commit as it is"
  fi
fi
COMMIT="$(git rev-parse --verify --quiet "$REF^{commit}")" || die "not a commit: $REF"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
TREE_DIR="$TMP/tree"
mkdir "$TREE_DIR"
git archive "$COMMIT" | tar -x -C "$TREE_DIR" || { echo "publish: git archive failed" >&2; exit 1; }

# --- privacy scan ---------------------------------------------------------------------
# The scanner and deny lists come from this checkout; the allowlist is the one being
# published, so what is suppressed is visible in the public tree.
echo "publish: scanning $(git rev-parse --short "$COMMIT") ..." >&2
allow="$TREE_DIR/scripts/privacy_allowlist.txt"
[[ -f "$allow" ]] || allow=/dev/null
set +e
HPGBOOKS_PRIVACY_DIR="${HPGBOOKS_PRIVACY_DIR:-${HPBOOKS_PRIVACY_DIR:-$ROOT/config}}" HPGBOOKS_PRIVACY_ALLOWLIST="$allow" \
  "$ROOT/scripts/privacy_scan.sh" "$TREE_DIR"
scan=$?
set -e
case "$scan" in
  0) ;;
  1) echo "publish: ABORTED: the privacy scan found the items above; nothing was published" >&2; exit 1 ;;
  3) echo "publish: ABORTED: local deny lists are missing; nothing was published" >&2; exit 3 ;;
  *) echo "publish: ABORTED: the privacy scan failed (exit $scan)" >&2; exit 2 ;;
esac

if [[ -n "$EXPORT_DIR" ]]; then
  if [[ -e "$EXPORT_DIR" ]]; then
    [[ -d "$EXPORT_DIR" && -z "$(ls -A "$EXPORT_DIR")" ]] || die "$EXPORT_DIR exists and is not an empty directory"
  fi
  mkdir -p "$EXPORT_DIR"
  (cd "$TREE_DIR" && tar -c .) | tar -x -C "$EXPORT_DIR"
  echo "publish: exported a clean tree of $(git rev-parse --short "$COMMIT") to $EXPORT_DIR"
  exit 0
fi

# --- identity -------------------------------------------------------------------------
NAME="${PUBLISH_AUTHOR_NAME:-$(git config --get publish.name || true)}"
EMAIL="${PUBLISH_AUTHOR_EMAIL:-$(git config --get publish.email || true)}"
if [[ -z "$NAME" || -z "$EMAIL" ]]; then
  die "no publish identity. Set one for the public history, for example:
    git config publish.name  \"Your Public Name\"
    git config publish.email \"you@users.noreply.github.com\"
  (or PUBLISH_AUTHOR_NAME / PUBLISH_AUTHOR_EMAIL). Your git user.email is not used."
fi

# --- the remote's current tip ---------------------------------------------------------
git remote get-url "$REMOTE" >/dev/null 2>&1 || die "no remote named $REMOTE (git remote add $REMOTE <url>)"
PARENT=""
if git ls-remote --exit-code --heads "$REMOTE" "refs/heads/$BRANCH" >/dev/null 2>&1; then
  git fetch --quiet --no-tags "$REMOTE" "refs/heads/$BRANCH" || { echo "publish: fetch from $REMOTE failed" >&2; exit 1; }
  PARENT="$(git rev-parse FETCH_HEAD)"
  if git merge-base --is-ancestor "$PARENT" "$COMMIT" 2>/dev/null; then
    echo "publish: ABORTED: $REMOTE/$BRANCH is part of the private history; refusing to build on it" >&2
    exit 1
  fi
else
  echo "publish: $REMOTE/$BRANCH does not exist yet; this will be its first (root) commit" >&2
fi

# --- build the commit with plumbing (no checkout, no change to HEAD or the index) -------
export GIT_INDEX_FILE="$TMP/index"
git --git-dir="$GIT_DIR_ABS" --work-tree="$TREE_DIR" -C "$TREE_DIR" add --all --force .
NEW_TREE="$(git --git-dir="$GIT_DIR_ABS" write-tree)"
unset GIT_INDEX_FILE

if [[ -n "$PARENT" && "$NEW_TREE" == "$(git rev-parse "$PARENT^{tree}")" ]]; then
  echo "publish: nothing to publish: $REMOTE/$BRANCH already has this tree"
  exit 0
fi

[[ -n "$MESSAGE" ]] || MESSAGE="Publish snapshot $(date +%Y-%m-%d)"
parent_args=()
[[ -n "$PARENT" ]] && parent_args=(-p "$PARENT")
NEW="$(GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" \
       GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" \
       git commit-tree "$NEW_TREE" "${parent_args[@]}" -m "$MESSAGE")"

BASE="${PARENT:-$(git hash-object -t tree /dev/null)}"
echo "publish: $(git rev-parse --short "$COMMIT") -> commit $(git rev-parse --short "$NEW") \"$MESSAGE\" by $NAME <$EMAIL>"
echo "publish: changes against ${PARENT:+$REMOTE/$BRANCH}${PARENT:-an empty tree}:"
git --no-pager diff --stat=100 "$BASE" "$NEW"

if ((PUSH == 0)); then
  echo "publish: dry run; nothing pushed. Re-run with --push to publish."
  exit 0
fi

git push "$REMOTE" "$NEW:refs/heads/$BRANCH"
git update-ref "refs/publish/$REMOTE/$BRANCH" "$NEW"
echo "publish: pushed $(git rev-parse --short "$NEW") to $REMOTE/$BRANCH"
