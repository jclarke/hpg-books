"""scripts/publish.sh against a throwaway private repo and a bare "public" remote.

The deny list, identities, and planted term are invented here. Nothing touches the real
repository, its remotes, or the real deny lists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_NAME = "Public Maintainer"
PUBLIC_EMAIL = "maintainer@example.test"
PRIVATE_EMAIL = "private-person@example.test"

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PUBLISH_", "HPGBOOKS_PRIVACY", "HPBOOKS_PRIVACY"))}
    gitconfig = tmp_path / "gitconfig"
    gitconfig.touch()
    env.update(
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        HPGBOOKS_PRIVACY_DIR=str(tmp_path / "priv"),
        PUBLISH_AUTHOR_NAME=PUBLIC_NAME,
        PUBLISH_AUTHOR_EMAIL=PUBLIC_EMAIL,
    )
    env.update(extra)
    return env


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True)
    return proc.stdout.strip()


def _commit(repo: Path, message: str, env: dict[str, str]) -> str:
    _git(repo, "add", "-A", env=env)
    _git(repo, "-c", "user.name=Private Person", "-c", f"user.email={PRIVATE_EMAIL}", "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


@pytest.fixture()
def setup(tmp_path):
    priv = tmp_path / "priv"
    priv.mkdir()
    (priv / "privacy-terms.local.txt").write_text("# fake deny list\nzebra ?finch\n")
    (priv / "privacy-ids.local.txt").write_text("acct-zz-9f3e\n")
    (priv / "privacy-last4.local.txt").write_text("9876\n")
    env = _env(tmp_path)

    repo = tmp_path / "private"
    (repo / "scripts").mkdir(parents=True)
    for name in ("publish.sh", "privacy_scan.sh", "privacy_allowlist.txt"):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    _git(tmp_path, "init", "-q", "-b", "work", str(repo), env=env)
    (repo / "README.md").write_text("# Demo\n\nA generic project.\n")
    first = _commit(repo, "private start", env)
    (repo / "notes.txt").write_text("work in progress\n")
    second = _commit(repo, "private follow-up", env)

    public = tmp_path / "public.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(public), env=env)
    _git(repo, "remote", "add", "public", str(public), env=env)
    return {"repo": repo, "public": public, "env": env, "private_commits": [first, second], "tmp": tmp_path}


def _publish(s, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(s["repo"] / "scripts" / "publish.sh"), *args],
        cwd=s["repo"], capture_output=True, text=True, env=env or s["env"], timeout=300,
    )


def _public_log(s) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(s["public"]), "rev-list", "refs/heads/main"],
        capture_output=True, text=True, env=s["env"],
    )
    return proc.stdout.split() if proc.returncode == 0 else []


def test_dry_run_does_not_push(setup):
    proc = _publish(setup)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "dry run" in proc.stdout
    assert "README.md" in proc.stdout
    assert _public_log(setup) == []


def test_push_creates_single_root_commit_with_head_tree(setup):
    s = setup
    proc = _publish(s, "--push", "--message", "First public release")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _public_log(s)
    assert len(log) == 1
    tip = log[0]
    pub = lambda *a: _git(s["public"], *a, env=s["env"])  # noqa: E731
    assert pub("rev-list", "--parents", "-n", "1", tip) == tip  # no parent
    assert pub("rev-parse", f"{tip}^{{tree}}") == _git(s["repo"], "rev-parse", "HEAD^{tree}", env=s["env"])
    assert pub("log", "-1", "--format=%an <%ae>|%cn <%ce>|%s", tip) == (
        f"{PUBLIC_NAME} <{PUBLIC_EMAIL}>|{PUBLIC_NAME} <{PUBLIC_EMAIL}>|First public release"
    )
    assert _git(s["repo"], "rev-parse", "refs/publish/public/main", env=s["env"]) == tip


def test_second_publish_adds_one_child_and_unchanged_tree_is_a_no_op(setup):
    s = setup
    assert _publish(s, "--push").returncode == 0
    first = _public_log(s)[0]
    (s["repo"] / "README.md").write_text("# Demo\n\nA generic project, now documented.\n")
    _commit(s["repo"], "private edit", s["env"])

    proc = _publish(s, "--push")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _public_log(s)
    assert len(log) == 2
    assert _git(s["public"], "rev-parse", f"{log[0]}^", env=s["env"]) == first
    assert _git(s["public"], "rev-parse", f"{log[0]}^{{tree}}", env=s["env"]) == _git(
        s["repo"], "rev-parse", "HEAD^{tree}", env=s["env"]
    )

    again = _publish(s, "--push")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "nothing to publish" in again.stdout
    assert len(_public_log(s)) == 2


def test_private_history_is_unreachable_from_public(setup):
    s = setup
    assert _publish(s, "--push").returncode == 0
    for sha in s["private_commits"] + [_git(s["repo"], "rev-parse", "HEAD", env=s["env"])]:
        missing = subprocess.run(
            ["git", "-C", str(s["public"]), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, env=s["env"],
        )
        assert missing.returncode != 0, f"private commit {sha} reached the public remote"
    emails = _git(s["public"], "log", "--format=%ae %ce", "main", env=s["env"])
    assert PRIVATE_EMAIL not in emails


def test_planted_forbidden_term_aborts(setup):
    s = setup
    (s["repo"] / "vendor.txt").write_text("Paid " + "Zebra" + " Finch Holdings\n")
    _commit(s["repo"], "oops", s["env"])
    proc = _publish(s, "--push")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "vendor.txt" in proc.stdout
    assert "ABORTED" in proc.stderr
    assert _public_log(s) == []


def test_missing_deny_lists_abort(setup):
    s = setup
    env = dict(s["env"], HPGBOOKS_PRIVACY_DIR=str(s["tmp"] / "nowhere"))
    proc = _publish(s, "--push", env=env)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert _public_log(s) == []


def test_refuses_without_publish_identity(setup):
    s = setup
    env = {k: v for k, v in s["env"].items() if not k.startswith("PUBLISH_")}
    _git(s["repo"], "config", "user.email", PRIVATE_EMAIL, env=env)
    proc = _publish(s, "--push", env=env)
    assert proc.returncode == 2
    assert "no publish identity" in proc.stderr
    assert _public_log(s) == []

    _git(s["repo"], "config", "publish.name", PUBLIC_NAME, env=env)
    _git(s["repo"], "config", "publish.email", PUBLIC_EMAIL, env=env)
    proc = _publish(s, "--push", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git(s["public"], "log", "-1", "--format=%ae", "main", env=env) == PUBLIC_EMAIL


def test_dirty_tree_needs_ref_and_export_only_writes_clean_tree(setup):
    s = setup
    (s["repo"] / "README.md").write_text("# Demo\n\nuncommitted\n")
    proc = _publish(s)
    assert proc.returncode == 2
    assert "uncommitted" in proc.stderr

    out = s["tmp"] / "export"
    proc = _publish(s, "--ref", "HEAD", "--export-only", str(out))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (out / "README.md").read_text() == "# Demo\n\nA generic project.\n"
    assert not (out / ".git").exists()
    assert _public_log(s) == []
