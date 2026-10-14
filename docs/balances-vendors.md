# Balances and vendor merges

## Statement balances

`hpgbooks accounts` sums imported activity. That figure is not the bank or card balance, because the ledger has no opening balance before the books open (2026-01-01, fixed in `hpgbooks/balances.py`).

An anchor is a posted balance on a date. Cash is money in the account. A credit card or other liability is the amount owed (a credit balance is negative). The amount on the command line is dollars.

```bash
bin/hpgbooks balances set 0001 --balance 1234.56 --as-of 2026-10-01 --source statement --note "September statement"
bin/hpgbooks balances list
bin/hpgbooks balances history 0001
```

`--source` is `statement` (the default, including the Update balance form) or `finance` (the daily sync). The account can be an id, a last4, or a unique part of the name (`paypal`, `0001`, `checking`).

The real balance is the anchor plus active activity dated after `as_of_date`. The implied opening balance on 2026-01-01 is the anchor minus the posted activity from that date through `as_of_date`. The register starts at the opening balance, so the running balance after the last counted row equals the real balance.

Posted versus pending:

- An anchor is the posted balance at the end of `as_of_date`. Posted rows on that day are already inside it.
- Active rows after `as_of_date` count, whether they are pending or posted.
- Pending rows on or before `as_of_date` do not count. That day is already closed in the anchor, so those rows are not added again.
- Superseded rows never count. Rows before 2026-01-01 do not count.

An account can have many anchors. The one in use is the latest by `as_of_date`, then the time it was recorded. Recording an older statement does not replace a later one. If the newer anchor disagrees with the balance the previous anchor predicts — posted activity strictly after the previous date, through the newer date — that gap is the reconciliation difference. A zero gap reconciles to the newer anchor.

With no anchor, cash shows the imported activity sum, and a card shows that sum with the sign flipped so a positive number is the amount owed. The screen labels those figures as imported activity.

The Accounts page, each register, and the dashboard cash card use this balance. Cards are listed as amounts owed, separate from cash, with cash minus cards owed. Update balance records a statement anchor. Only business accounts appear.

## Vendor merges

One vendor often arrives under several spellings. A merge points those spellings at one display name. The ledger text is not rewritten. The vendors page, the expenses-by-vendor report (CSV, XLSX, and PDF), dashboard top vendors, global search, and the transactions vendor filter all apply the map when they run. Filtering by the canonical name, or by any spelling in the group, returns every merged row.

```bash
bin/hpgbooks vendors merge "ACME SOFTWARE INC" "ACME SOFTWARE LONG DESCRIPTOR" --into "Acme Software"
bin/hpgbooks vendors unmerge "ACME SOFTWARE INC"
bin/hpgbooks vendors rename "Acme Software" "Acme"
bin/hpgbooks vendors aliases
```

On the Vendors page, select two or more rows and choose Merge. The dialog shows the combined spend and the name that will be displayed. A merged vendor shows how many spellings it contains; expand it to unmerge one spelling. Rename changes the canonical name, and the spellings follow it. Possible duplicates are pairs that share a long token or a normalized prefix and are not already one vendor. Merging the same group into the same name again does not add another audit row.

Every balance update, merge, unmerge, and rename writes an `audit_log` row (`balance_set`, `vendor_merge`, `vendor_unmerge`, `vendor_rename`). The web forms require the CSRF token.
