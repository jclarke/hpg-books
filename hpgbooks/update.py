"""`hpgbooks update check|apply`: compare this install with the public snapshot repo and fast-forward to it.

Upstream is the git remote named `[update] remote` (default "public") when this is a git
clone that has it; otherwise https://github.com/<[update] repo>.git on `[update] branch`.
An install that is not a git clone (an unpacked zip) is compared through the GitHub API
using the revision recorded in .hpgbooks-revision, and updated by overlaying a zip of the
branch.

The public branch is built by scripts/publish.sh: snapshot commits that share no history
with the private branch they came from. So a clone counts as up to date when its HEAD is,
contains, or has the same tree as the remote tip, or when the remote tip is the last
snapshot this clone published itself (refs/publish/<remote>/<branch>).

Files git ignores (config/local.toml, config/*.local.*, data/, keys, .venv, ...) are never
written: a fast-forward only touches tracked files, and the zip overlay skips every path
the ignore patterns match.

Exit codes: 0 up to date (or applied), 1 an update is available (check, or apply without
--yes), 2 error.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from hpgbooks.config import REPO_ROOT, UpdateConfig, get_config

UP_TO_DATE, AVAILABLE, ERROR = 0, 1, 2
REVISION_FILE = ".hpgbooks-revision"
# compat: zip installs from before the rename recorded the revision here
LEGACY_REVISION_FILE = ".hpbooks-revision"

# Never written by an update, whatever .gitignore says. Matched like .gitignore lines.
ALWAYS_PROTECTED = (
    "config/local.toml",
    "config/*.local.*",
    "data/",
    "exports/",
    "sync/inbox/",
    ".venv/",
    "node_modules/",
    "*.db",
    "*.db-*",
    "*.sqlite*",
    "*.key",
    "*.secret",
    "*.pem",
    "id_rsa*",
    ".env",
    ".env.*",
    ".git/",
    REVISION_FILE,
    LEGACY_REVISION_FILE,
)


class UpdateError(Exception):
    """Upstream could not be reached or the update cannot be applied safely."""


@dataclass(frozen=True)
class Tip:
    sha: str
    date: str = ""
    subject: str = ""

    @property
    def short(self) -> str:
        return self.sha[:7]

    def describe(self) -> str:
        parts = [self.short]
        if self.date:
            parts.append(self.date)
        if self.subject:
            parts.append(f'"{self.subject}"')
        return " ".join(parts)


@dataclass(frozen=True)
class Status:
    available: bool
    tip: Tip
    local: str  # local revision (sha) or "" when unknown
    reason: str  # why it is (not) up to date
    mode: str  # "git" or "zip"
    source: str  # remote name or URL
    can_fast_forward: bool = False


# --- git helpers (tests monkeypatch nothing here; they use local repositories) ------------


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
    )
    if check and proc.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def _out(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.strip()


def is_git_clone(root: Path) -> bool:
    if not (root / ".git").exists() or shutil.which("git") is None:
        return False
    proc = _git(root, "rev-parse", "--show-toplevel", check=False)
    return proc.returncode == 0 and Path(proc.stdout.strip()).resolve() == root.resolve()


def _has_remote(root: Path, name: str) -> bool:
    return _git(root, "remote", "get-url", name, check=False).returncode == 0


def repo_url(cfg: UpdateConfig) -> str:
    return f"https://github.com/{cfg.repo}.git"


def git_source(root: Path, cfg: UpdateConfig) -> str:
    """The remote name when this clone has it, else the repository URL."""
    return cfg.remote if _has_remote(root, cfg.remote) else repo_url(cfg)


def _fetch(root: Path, source: str, branch: str) -> str:
    """Fetch the upstream branch; returns its sha. Updates <remote>/<branch> when source is a remote."""
    if source.startswith(("https://", "http://", "git@", "ssh://", "file://", "/")):
        _git(root, "fetch", "--quiet", "--no-tags", source, f"refs/heads/{branch}")
        return _out(root, "rev-parse", "FETCH_HEAD")
    _git(root, "fetch", "--quiet", "--no-tags", source, f"+refs/heads/{branch}:refs/remotes/{source}/{branch}")
    return _out(root, "rev-parse", f"refs/remotes/{source}/{branch}")


def _ls_remote(root: Path, source: str, branch: str) -> str:
    proc = _git(root, "ls-remote", "--exit-code", source, f"refs/heads/{branch}", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise UpdateError(f"could not read {branch} from {source}: {proc.stderr.strip() or 'branch not found'}")
    return proc.stdout.split()[0]


def _tip_info(root: Path, sha: str) -> Tip:
    sep = "\x1f"
    text = _out(root, "log", "-1", f"--format=%H{sep}%cs{sep}%s", sha)
    full, date, subject = (text.split(sep) + ["", ""])[:3]
    return Tip(full, date, subject)


def _is_ancestor(root: Path, a: str, b: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def git_status(root: Path, cfg: UpdateConfig) -> Status:
    source = git_source(root, cfg)
    head = _out(root, "rev-parse", "HEAD")
    remote_sha = _ls_remote(root, source, cfg.branch)

    def status(available: bool, reason: str, tip: Tip | None = None, ff: bool = False) -> Status:
        return Status(available, tip or Tip(remote_sha), head, reason, "git", source, ff)

    if remote_sha == head:
        return status(False, "HEAD is the upstream tip")
    published = _git(root, "rev-parse", "--verify", "--quiet", f"refs/publish/{cfg.remote}/{cfg.branch}", check=False)
    if published.returncode == 0 and published.stdout.strip() == remote_sha:
        return status(False, "the upstream tip is the last snapshot published from this clone")

    fetched = _fetch(root, source, cfg.branch)
    tip = _tip_info(root, fetched)
    if _is_ancestor(root, fetched, head):
        return status(False, "HEAD already contains the upstream tip", tip)
    if _out(root, "rev-parse", f"{fetched}^{{tree}}") == _out(root, "rev-parse", "HEAD^{tree}"):
        return status(False, "HEAD has the same files as the upstream tip", tip)
    return status(True, "upstream has changes", tip, ff=_is_ancestor(root, head, fetched))


# --- GitHub API (non-git installs) ---------------------------------------------------------


def _github_get(path: str, accept: str = "application/vnd.github+json") -> bytes:
    """GET api.github.com/<path>, through `gh` when it is installed (it holds the login), else urllib."""
    if shutil.which("gh"):
        proc = subprocess.run(
            ["gh", "api", "-H", f"Accept: {accept}", path], capture_output=True, env={**os.environ, "GH_PROMPT_DISABLED": "1"}
        )
        if proc.returncode == 0:
            return proc.stdout
    request = urllib.request.Request(f"https://api.github.com/{path}", headers={"Accept": accept, "User-Agent": "hpgbooks-update"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except OSError as exc:
        raise UpdateError(f"GitHub request {path} failed: {exc}") from exc


def api_tip(cfg: UpdateConfig) -> Tip:
    try:
        data = json.loads(_github_get(f"repos/{cfg.repo}/commits/{cfg.branch}"))
        commit = data["commit"]
        return Tip(data["sha"], commit["committer"]["date"][:10], commit["message"].splitlines()[0])
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise UpdateError(f"unexpected GitHub reply for {cfg.repo}@{cfg.branch}") from exc


def download_zip(cfg: UpdateConfig, sha: str) -> bytes:
    return _github_get(f"repos/{cfg.repo}/zipball/{sha}", accept="application/octet-stream")


def installed_revision(root: Path) -> str:
    for name in (REVISION_FILE, LEGACY_REVISION_FILE):
        try:
            return (root / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def zip_status(root: Path, cfg: UpdateConfig) -> Status:
    tip = api_tip(cfg)
    local = installed_revision(root)
    source = f"github.com/{cfg.repo}@{cfg.branch}"
    if local and local == tip.sha:
        return Status(False, tip, local, "installed revision is the upstream tip", "zip", source)
    reason = "upstream has changes" if local else f"installed revision unknown (no {REVISION_FILE})"
    return Status(True, tip, local, reason, "zip", source)


def check_status(root: Path = REPO_ROOT, cfg: UpdateConfig | None = None) -> Status:
    cfg = cfg or get_config().update
    return git_status(root, cfg) if is_git_clone(root) else zip_status(root, cfg)


# --- protection ----------------------------------------------------------------------------


def ignore_patterns(root: Path) -> list[str]:
    patterns = list(ALWAYS_PROTECTED)
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith(("#", "!")):
            patterns.append(line)
    return patterns


def is_protected(path: str, patterns: list[str]) -> bool:
    """True when a repo-relative path (posix) matches an ignore pattern, or sits under a matching directory."""
    parts = path.strip("/").split("/")
    for pattern in patterns:
        directory = pattern.endswith("/")
        pat = pattern.strip("/")
        anchored = "/" in pat
        for depth in range(1, len(parts) + 1):
            if directory and depth == len(parts):
                continue  # "data/" protects what is inside data, and data itself as a directory
            prefix = "/".join(parts[:depth])
            if anchored:
                if fnmatch.fnmatchcase(prefix, pat):
                    return True
            elif fnmatch.fnmatchcase(parts[depth - 1], pat):
                return True
    return False


# --- apply ---------------------------------------------------------------------------------


def _changed_files(root: Path, old: str, new: str) -> list[str]:
    text = _out(root, "diff", "--name-only", old, new)
    return [line for line in text.splitlines() if line]


def _after_apply(changed: list[str], root: Path, build_ui: bool) -> int:
    web = [p for p in changed if p.startswith("web/")]
    if web:
        print(f"web/ changed ({len(web)} files). The built UI in hpgbooks/static/app comes with the update;")
        if build_ui:
            print("rebuilding it here: bin/build-ui --no-install")
            proc = subprocess.run([str(root / "bin" / "build-ui"), "--no-install"], cwd=root)
            if proc.returncode != 0:
                print("hpgbooks: bin/build-ui failed; the committed build is still in place", file=sys.stderr)
        else:
            print("  to rebuild it from source here: bin/build-ui --no-install")
    if "requirements.txt" in changed:
        print("requirements.txt changed: .venv/bin/pip install -r requirements.txt")
    if any(p.startswith(("hpgbooks/", "web/")) for p in changed):
        print("restart the web server to load it: bin/hpgbooks-web stop && bin/hpgbooks-web start")
    return UP_TO_DATE


def apply_git(root: Path, cfg: UpdateConfig, status: Status, *, yes: bool, build_ui: bool) -> int:
    if not status.can_fast_forward:
        print(
            f"hpgbooks: cannot fast-forward: HEAD {status.local[:7]} is not an ancestor of upstream {status.tip.short}.\n"
            "  This clone has its own history (for example the private clone that publishes the snapshots,\n"
            "  or local commits). Merge or rebase by hand: git fetch "
            f"{status.source} {cfg.branch} && git log HEAD..FETCH_HEAD",
            file=sys.stderr,
        )
        return ERROR
    changed = _changed_files(root, status.local, status.tip.sha)
    patterns = ignore_patterns(root)
    clash = [p for p in changed if is_protected(p, patterns)]
    if clash:
        print(f"hpgbooks: refusing to update: upstream changes protected paths: {', '.join(clash)}", file=sys.stderr)
        return ERROR
    commits = _out(root, "log", "--oneline", f"{status.local}..{status.tip.sha}").splitlines()
    print(f"{len(commits)} new commit(s), {len(changed)} file(s) changed:")
    for line in commits[:20]:
        print(f"  {line}")
    if not yes:
        print("dry run: nothing changed. Re-run with --yes to fast-forward (git merge --ff-only).")
        return AVAILABLE
    _git(root, "update-index", "-q", "--refresh", check=False)
    if _git(root, "diff", "--quiet", "HEAD", "--", check=False).returncode != 0 or _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
        print("hpgbooks: tracked files have uncommitted changes; commit or stash them first", file=sys.stderr)
        return ERROR
    _git(root, "merge", "--ff-only", "--quiet", status.tip.sha)
    print(f"updated {status.local[:7]} -> {status.tip.short}; config/local.toml, data/, and keys are untracked and were not touched")
    return _after_apply(changed, root, build_ui)


def overlay_zip(root: Path, data: bytes, patterns: list[str]) -> tuple[list[str], list[str]]:
    """Write the zip's files over root, skipping protected paths. Returns (written, skipped)."""
    written, skipped = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [info for info in archive.infolist() if not info.is_dir()]
        # GitHub zipballs hold everything under one "<owner>-<repo>-<sha>/" directory.
        tops = {info.filename.split("/", 1)[0] for info in names}
        strip = len(tops) == 1 and all("/" in info.filename for info in names)
        root_resolved = root.resolve()
        for info in names:
            rel = info.filename.split("/", 1)[1] if strip else info.filename
            if not rel or rel.endswith("/"):
                continue
            target = (root / rel).resolve()
            if not target.is_relative_to(root_resolved) or ".." in Path(rel).parts:
                raise UpdateError(f"unsafe path in archive: {info.filename}")
            if is_protected(rel, patterns):
                skipped.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".hpgbooks-update")
            with archive.open(info) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(tmp, mode)
            os.replace(tmp, target)
            written.append(rel)
    return written, skipped


def apply_zip(root: Path, cfg: UpdateConfig, status: Status, *, yes: bool, build_ui: bool) -> int:
    if not yes:
        print(f"dry run: would download {status.source} at {status.tip.short} and overlay it on {root},")
        print("  skipping gitignored paths (config/local.toml, data/, keys, .venv, ...). Re-run with --yes.")
        return AVAILABLE
    data = download_zip(cfg, status.tip.sha)
    written, skipped = overlay_zip(root, data, ignore_patterns(root))
    (root / REVISION_FILE).write_text(status.tip.sha + "\n", encoding="utf-8")
    print(f"updated to {status.tip.short}: wrote {len(written)} file(s), skipped {len(skipped)} protected path(s)")
    print("  files removed upstream are not deleted here")
    return _after_apply(written, root, build_ui)


# --- commands ------------------------------------------------------------------------------


def _report(status: Status) -> None:
    if status.available:
        local = status.local[:7] if status.local else "unknown"
        print(f"update available from {status.source}: {status.tip.describe()} (installed: {local}; {status.reason})")
    else:
        print(f"up to date with {status.source} ({status.tip.short}): {status.reason}")


def cmd_check(args, root: Path = REPO_ROOT) -> int:
    try:
        status = check_status(root)
    except UpdateError as exc:
        print(f"hpgbooks: update check failed: {exc}", file=sys.stderr)
        return ERROR
    _report(status)
    if status.mode == "git" and status.source.startswith("https://"):
        print(f"hint: git remote add {get_config().update.remote} {status.source}", file=sys.stderr)
    return AVAILABLE if status.available else UP_TO_DATE


def cmd_apply(args, root: Path = REPO_ROOT) -> int:
    cfg = get_config().update
    try:
        status = check_status(root, cfg)
        _report(status)
        if not status.available:
            return UP_TO_DATE
        apply = apply_git if status.mode == "git" else apply_zip
        return apply(root, cfg, status, yes=args.yes, build_ui=args.build_ui)
    except (UpdateError, zipfile.BadZipFile, OSError) as exc:
        print(f"hpgbooks: update failed: {exc}", file=sys.stderr)
        return ERROR


def add_parser(sub) -> None:
    update = sub.add_parser("update", help="check for and apply updates from the public repository")
    update_sub = update.add_subparsers(dest="update_cmd", required=True)
    check = update_sub.add_parser("check", help="exit 0 up to date, 1 update available, 2 error")
    check.set_defaults(func=cmd_check)
    apply = update_sub.add_parser("apply", help="fast-forward to the upstream tip (dry run without --yes)")
    apply.add_argument("--yes", action="store_true", help="actually apply; without it nothing changes")
    apply.add_argument("--build-ui", action="store_true", help="run bin/build-ui --no-install when web/ changed")
    apply.set_defaults(func=cmd_apply)
