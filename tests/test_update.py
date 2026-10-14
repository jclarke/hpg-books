"""`hpbooks update check|apply` against local repositories (no network) and a faked GitHub API."""

from __future__ import annotations

import io
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpbooks import config as hpconfig
from hpbooks import update
from hpbooks.config import UpdateConfig

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    # Never reach the real network: any GitHub call is a test failure unless a test replaces it.
    monkeypatch.setattr(update, "_github_get", lambda *a, **k: pytest.fail("network call"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    """An upstream repo, a bare 'public' remote, and an install cloned from it."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    commit(upstream, {".gitignore": "config/local.toml\ndata/\n", "README.md": "v1\n"}, "first")
    bare = tmp_path / "public.git"
    git(tmp_path, "clone", "-q", "--bare", str(upstream), str(bare))
    git(upstream, "remote", "add", "origin", str(bare))
    install = tmp_path / "install"
    git(tmp_path, "clone", "-q", "-o", "public", str(bare), str(install))
    (install / "config").mkdir()
    (install / "config" / "local.toml").write_text("# mine\n")
    (install / "data").mkdir()
    (install / "data" / "hpbooks.db").write_text("ledger")
    return SimpleNamespace(upstream=upstream, bare=bare, install=install)


def push_new(repos, files, message="second") -> str:
    sha = commit(repos.upstream, files, message)
    git(repos.upstream, "push", "-q", "origin", "main")
    return sha


def run(fn, root, **kw):
    return fn(SimpleNamespace(yes=kw.get("yes", False), build_ui=False), root=root)


def test_check_up_to_date(repos, capsys):
    assert run(update.cmd_check, repos.install) == update.UP_TO_DATE
    assert "up to date" in capsys.readouterr().out


def test_check_update_available(repos, capsys):
    sha = push_new(repos, {"README.md": "v2\n"}, "Publish snapshot 2026-10-07")
    assert run(update.cmd_check, repos.install) == update.AVAILABLE
    out = capsys.readouterr().out
    assert "update available" in out and sha[:7] in out and "Publish snapshot 2026-10-07" in out


def test_check_error_when_remote_unreachable(repos, capsys):
    git(repos.install, "remote", "set-url", "public", str(repos.install.parent / "missing.git"))
    assert run(update.cmd_check, repos.install) == update.ERROR


def test_check_same_tree_with_unrelated_history_is_up_to_date(repos):
    """The publishing clone: its HEAD has the published files but none of the public commits."""
    private = repos.install.parent / "private"
    private.mkdir()
    git(private, "init", "-q", "-b", "master")
    commit(private, {".gitignore": "config/local.toml\ndata/\n", "README.md": "v1\n"}, "private history")
    git(private, "remote", "add", "public", str(repos.bare))
    assert update.git_status(private, UpdateConfig()).available is False


def test_apply_needs_yes_and_preserves_local_files(repos, capsys):
    sha = push_new(repos, {"README.md": "v2\n", "web/src/x.ts": "x\n", "hpbooks/new.py": "\n"})
    head = git(repos.install, "rev-parse", "HEAD")
    assert run(update.cmd_apply, repos.install) == update.AVAILABLE
    assert git(repos.install, "rev-parse", "HEAD") == head
    assert "dry run" in capsys.readouterr().out

    assert run(update.cmd_apply, repos.install, yes=True) == update.UP_TO_DATE
    assert git(repos.install, "rev-parse", "HEAD") == sha
    assert (repos.install / "README.md").read_text() == "v2\n"
    assert (repos.install / "config" / "local.toml").read_text() == "# mine\n"
    assert (repos.install / "data" / "hpbooks.db").read_text() == "ledger"
    out = capsys.readouterr().out
    assert "bin/build-ui --no-install" in out and "bin/hpbooks-web stop" in out
    assert run(update.cmd_check, repos.install) == update.UP_TO_DATE


def test_apply_refuses_diverged_history(repos):
    push_new(repos, {"README.md": "v2\n"})
    commit(repos.install, {"local.txt": "mine\n"}, "local work")
    head = git(repos.install, "rev-parse", "HEAD")
    assert run(update.cmd_apply, repos.install, yes=True) == update.ERROR
    assert git(repos.install, "rev-parse", "HEAD") == head


def test_is_protected():
    patterns = update.ignore_patterns(hpconfig.REPO_ROOT)
    for path in ("config/local.toml", "config/privacy-terms.local.txt", "data/hpbooks.db", "data/x/y", "a/b.key", ".venv/bin/python", "web/node_modules/x/y.js"):
        assert update.is_protected(path, patterns), path
    for path in ("config/config.example.toml", "hpbooks/cli.py", "web/src/App.tsx", "README.md", "database.md"):
        assert not update.is_protected(path, patterns), path


def _zipball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("owner-repo-abc1234/", "")
        for name, text in files.items():
            archive.writestr(f"owner-repo-abc1234/{name}", text)
    return buf.getvalue()


def test_zip_install_check_and_apply(tmp_path, monkeypatch, capsys):
    root = tmp_path / "unzipped"
    (root / "config").mkdir(parents=True)
    (root / "config" / "local.toml").write_text("# mine\n")
    (root / "README.md").write_text("old\n")
    (root / ".gitignore").write_text("config/local.toml\ndata/\n")
    sha = "a" * 40
    monkeypatch.setattr(update, "api_tip", lambda cfg: update.Tip(sha, "2026-10-07", "Publish snapshot"))
    zipball = _zipball({"README.md": "new\n", "config/local.toml": "# upstream\n", "data/x.db": "x", "hpbooks/a.py": "\n"})
    monkeypatch.setattr(update, "download_zip", lambda cfg, ref: zipball)

    assert run(update.cmd_check, root) == update.AVAILABLE
    assert run(update.cmd_apply, root) == update.AVAILABLE
    assert (root / "README.md").read_text() == "old\n"
    assert run(update.cmd_apply, root, yes=True) == update.UP_TO_DATE
    assert (root / "README.md").read_text() == "new\n"
    assert (root / "hpbooks" / "a.py").exists()
    assert (root / "config" / "local.toml").read_text() == "# mine\n"
    assert not (root / "data").exists()
    assert (root / update.REVISION_FILE).read_text().strip() == sha
    assert run(update.cmd_check, root) == update.UP_TO_DATE


def test_update_config_defaults_and_override():
    assert hpconfig.build({}).update == UpdateConfig("jclarke/hpg-books", "public", "main")
    cfg = hpconfig.build({"update": {"repo": "someone/books", "branch": "stable"}})
    assert cfg.update == UpdateConfig("someone/books", "public", "stable")
    with pytest.raises(hpconfig.ConfigError):
        hpconfig.build({"update": {"repo": "nope"}})
