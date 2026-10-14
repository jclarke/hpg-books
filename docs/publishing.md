# Publishing a public copy (maintainers)

The working repository can hold private history: early commits with real names, notes, or data that were cleaned up later. Rewriting that history is error-prone. Instead, `scripts/publish.sh` publishes **snapshots**: each publish is one new commit containing the scanned tree of a private commit, whose only parent is the previous published commit. The public branch never contains a private commit, so nothing in the private history is reachable from it.

```
private:  a ── b ── c ── d ── e ── f        (your branch, never pushed publicly)
                    │              │
public:             P1 ─────────── P2       (public/main: one commit per publish)
```

## One-time setup

1. Add the public remote (an empty repository you created on your git host):

   ```bash
   git remote add public https://github.com/your-account/books.git
   ```

2. Choose the identity that appears in the public history. The script refuses to run without one, so your everyday `user.email` is never used by accident:

   ```bash
   git config publish.name  "Your Public Name"
   git config publish.email "you@users.noreply.github.com"
   ```

   (`PUBLISH_AUTHOR_NAME` / `PUBLISH_AUTHOR_EMAIL` override these for one run.)

3. Create the three local deny lists. They are gitignored (`config/*.local.*`) and are never published. Put them in `config/` or point `HPGBOOKS_PRIVACY_DIR` at another directory:

   | File | Content |
   |---|---|
   | `privacy-terms.local.txt` | One case-insensitive regex per line: your name, family names, company and brand names, vendors, street, city, hostnames, tailnet names |
   | `privacy-ids.local.txt` | Fixed strings: account ids, transaction ids, anything copied from real data |
   | `privacy-last4.local.txt` | The last 4 digits of your real account and card numbers (whitespace separated) |

   `#` starts a comment. Missing lists abort the scan, because a scan without them cannot catch personal names.

4. Install `ripgrep` (required). `gitleaks` and `trufflehog` are used when installed.

## Each publish

```bash
scripts/publish.sh            # dry run: export HEAD, scan, build the commit, show the diff --stat
scripts/publish.sh --push     # the same, then push it to public/main
```

What happens:

1. `git archive HEAD` into a temporary directory. HEAD must have no uncommitted changes to tracked files (or pass `--ref <commit>` to publish a specific commit as it is).
2. `scripts/privacy_scan.sh` runs on that tree with your deny lists and the generic checks (emails, phone numbers, IP addresses other than loopback and documentation ranges, tailnet names, tokens and private keys, 64-hex strings, home directory paths, forbidden files such as databases, keys, `config/local.toml`, and unlisted images). Any finding aborts and nothing is published.
3. The current `public/main` is fetched, if it exists.
4. A commit is built with plumbing (`git add` into a temporary index, `write-tree`, `commit-tree`) with the previous public commit as its only parent, or no parent for the first publish. Your branch, index, and working tree are not touched.
5. If the tree equals the public tip's tree, the script prints "nothing to publish" and stops.
6. With `--push`: `git push public <commit>:refs/heads/main` (never forced; a rejected push means someone else changed the public branch) and `refs/publish/public/main` records what was pushed.

Options: `--remote NAME` (default `public`), `--branch NAME` (default `main`), `--message TEXT` (default `Publish snapshot YYYY-MM-DD`), `--ref REF`, `--export-only DIR` (write the scanned tree to an empty directory and stop, for a manual look or another publishing route).

## Checking without publishing

```bash
scripts/privacy_scan.sh               # scan the tracked tree of HEAD
scripts/privacy_scan.sh --worktree    # scan what is in the working tree now (tracked + untracked, not ignored)
```

A false positive is better fixed at the source (build a test string from pieces, use `example.com` / `192.0.2.x` documentation values). When that is not possible, add a regex with a comment explaining why to `scripts/privacy_allowlist.txt`. Never put a personal term in the allowlist; it is published.

## Contributions from the public repository

Public commits are not ancestors of your private branch. To bring a public change in, apply it as a patch (`git fetch public && git cherry-pick <sha>` or `git am`), then publish as usual; the next snapshot includes it.
