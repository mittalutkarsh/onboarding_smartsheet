# onboarding-automation

Week-1 MVP of the **Smartsheet → Git onboarding automation**.

A Smartsheet row is a ticket: *"please onboard this repo."* This tool reads
rows marked **Ready**, generates the onboarding files, opens a GitHub PR for a
human to review, and writes the PR URL / status back to the same row.

```
Smartsheet row (Ready)
        ↓
Python reads + validates the row
        ↓
Clone target repo → create onboarding branch
        ↓
Render onboarding files from Jinja templates
        ↓
Commit → push → open PR (via gh CLI)
        ↓
Write PR URL + status back to the Smartsheet row
```

A human reviewer approves every PR. The automation **never auto-merges,
force-pushes, or deletes anything.**

## Scope

Week-1 only: `Smartsheet row → local script → Git branch → PR → Smartsheet
update`. No dashboard automation, no webhook server, no scheduled cron (the
Action ships with **manual `workflow_dispatch` only**; the 6-hourly schedule
from the spec is left commented out for Week-3).

## Layout

```
onboarding-automation/
  README.md
  requirements.txt
  .env.example
  src/
    main.py               # orchestrator + per-row error handling
    smartsheet_client.py  # read rows, column-ID<->title map, write-back
    github_client.py      # PR create/reuse/skip via `gh`
    repo_modifier.py      # clone, branch, commit, push (GitPython)
    templates.py          # render Jinja templates into the checkout
    validators.py         # required-field validation
  templates/
    workflow.yml.j2       # one deploy workflow per environment
    app-config.yml.j2
    policy-groups.yml.j2
    manifest.yml.j2
    onboarding.md.j2
  .github/workflows/
    sync-smartsheet-to-git.yml   # manual trigger (Week-1)
```

## Generated files (in the target repo)

For each Ready row the PR adds:

```
.github/workflows/deploy-<env>.yml   # one per environment (dev/test/stage/prod)
onboarding/app-config.yml
onboarding/policy-groups.yml
onboarding/manifest.yml
docs/onboarding.md
```

## Smartsheet tracker schema

Create a sheet named **Git Onboarding Tracker** with these columns (titles must
match exactly — the code maps column **titles ↔ IDs** in both directions):

| Column | Read/Write | Purpose |
|---|---|---|
| Team Name | read | Owning team (required) |
| GitHub Org | read | Target org (required) |
| GitHub Repo | read | Target repo (required) |
| App / Cookbook Name | read | App name (required) |
| Owner Email | read | Owner (required, validated) |
| Environments | read | Comma-separated, e.g. `dev,test,stage,prod` (required) |
| Onboarding Status | read + write | Only `Ready` rows are processed; set to `PR Created` / `Merged` / `Blocked` |
| Migration Type | read | *(read but unused in Week-1)* |
| Branch Name | read | Optional; defaults to `onboarding/<app-slug>` |
| PR URL | write | Set to the PR link |
| Last Sync Time | write | ISO-8601 UTC timestamp |
| Validation Status | write | `Pass` / `Fail` |
| Notes | write | e.g. "reused existing PR" |
| Error Message | write | Failure reason (cleared on success) |

## Setup

Requires **Python 3.11**, **git**, and the **GitHub CLI (`gh`)** installed and
authenticated (`gh auth login`), or `GITHUB_TOKEN` set in the environment.

```bash
cd onboarding-automation
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in the three tokens
```

Required environment variables (see `.env.example`):

- `SMARTSHEET_TOKEN` — Smartsheet API token
- `SMARTSHEET_SHEET_ID` — the tracker sheet ID (placeholder `[SHEET_ID]`)
- `GITHUB_TOKEN` — GitHub token with repo + PR scope

Org/repo/app come from each row, not config. Placeholders used in docs:
`[GITHUB_ORG]`, `[TARGET_REPO]`.

## Run

```bash
python src/main.py
```

The script:
1. Reads rows where `Onboarding Status = Ready`.
2. Validates required fields (fails the row with a reason if anything is missing).
3. Clones/reuses the repo, creates/reuses the onboarding branch.
4. Renders the onboarding files.
5. Commits, pushes, opens the PR.
6. Writes PR URL + status + timestamp back to the row.

Exit code is `0` when every row succeeds, `1` if any row failed (each failure is
still recorded on its own row), `2` on missing configuration.

## Idempotency & safety

Safe to re-run. Work is keyed on the sheet row via a deterministic branch name:

- **Branch** — if it already exists on the remote, it is reused, not recreated
  (`repo_modifier.ensure_branch`).
- **PR** — before creating, the tool lists PRs on the head branch across **all**
  states (`github_client.ensure_pr`). An open PR is reused; a **merged/closed PR
  is left untouched — never reopened or duplicated**.
- A clean re-run with no file changes produces no empty commit.
- Pushes are plain (never `--force`); nothing is ever deleted.

## Error handling

Every row runs inside its own `try/except` in `main.py`. On any failure the tool
writes `Validation Status = Fail`, `Onboarding Status = Blocked`, and a clear
`Error Message` back to that row, then continues to the next. One bad row never
aborts the batch.

## Notes on API usage

- **Smartsheet:** `GET /2.0/sheets/{id}` (read columns + rows),
  `PUT /2.0/sheets/{id}/rows` (write-back). Bearer-token auth.
- **GitHub:** PRs via the `gh` CLI (`gh pr list`, `gh pr create`) per spec §10.
  `gh` reads `GH_TOKEN`/`GITHUB_TOKEN` from the environment.

## Deviations from the spec (flagged)

1. Uses the **`gh` CLI** for PRs (spec §10 Option A); `PyGithub` is omitted from
   `requirements.txt`.
2. The Action ships **manual-only** (cron commented out) to honor the Week-1
   "no cron unless asked" boundary while keeping the spec's file present.
3. Added `onboarding.md.j2` because the task's render list includes
   `docs/onboarding.md` (not in the spec's template list).
4. `Migration Type` is read into the record but not branched on in Week-1.
```
