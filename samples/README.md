# Sample tracker data

Test data for the **Git Onboarding Tracker**, covering every code path.

## Files

- **`git-onboarding-tracker.csv`** — import into Smartsheet via
  **File → Import → CSV → Import as new sheet**. Column headers match the
  schema titles exactly (the automation maps titles ↔ column IDs).
- **`sheet-fixture.json`** — the same data in the Smartsheet
  `GET /sheets/{id}` response shape, for offline testing without a live sheet.

## The 8 test rows and what each exercises

| Team | Status | What it tests |
|---|---|---|
| Payments Platform | **Ready** | Happy path — valid row, 4 environments → branch + PR created |
| Data Insights | **Ready** | Happy path with an explicit `Branch Name` + existing-repo migration |
| Mobile Web | **Ready** | Validation **Fail** — missing `Owner Email` → row Blocked with reason |
| Search Team | **Ready** | Validation **Fail** — invalid environment token `staging` (allowed: dev/test/stage/prod) |
| Identity | New | Skipped — only `Ready` rows are processed |
| Checkout | PR Created | Skipped — already has an open PR (write-back state) |
| Loyalty | Merged | Skipped — merged PR is never reopened |
| Inventory | Blocked | Skipped — previously failed row, retains its `Error Message` |

Running the automation against this data processes **4 Ready rows**: 2 succeed
(PRs opened) and 2 fail validation (Blocked, with a clear `Error Message`).
The other 4 rows are left untouched.

> Note: the two happy-path rows point at repos (`payments-service`,
> `insights-etl`) under the `example-org` placeholder. Point them at real repos
> you can push to before running against live GitHub, or use `sheet-fixture.json`
> for a dry read-only check.
