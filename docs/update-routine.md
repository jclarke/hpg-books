# Daily update check routine

`bin/hpgbooks update check` exits **0** when the install is up to date, **1** when the public repository has a newer snapshot, and **2** when the check failed (no network, bad remote). A scheduled assistant can run it daily and stay quiet unless there is something to do.

## Routine prompt (Grok Bot or any scheduled assistant)

Schedule it once a day, with the working directory set to the install:

```text
Run `bin/hpgbooks update check` in the books install directory and look at its exit code.

- Exit 0: the books are up to date. Do nothing and send no message.
- Exit 1: an update is available. Send the owner one short message with the line the
  command printed (short sha, date, and subject of the new snapshot) and how to apply it:
  `bin/hpgbooks update apply` (dry run), then `bin/hpgbooks update apply --yes`, then
  `bin/hpgbooks-web stop && bin/hpgbooks-web start`. Do not apply it yourself.
- Exit 2: do nothing today. If it has failed three days in a row, send one message
  with the error line.

Never print or read config/local.toml, data/, or key files. Change nothing.
```

## Plain cron

```cron
15 7 * * *  cd /path/to/books && bin/hpgbooks update check >/dev/null 2>&1; [ $? -eq 1 ] && echo "books: update available" | mail -s "books update" you@example.com
```
