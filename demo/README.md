# End-to-end demo

Run the **entire** onboarding automation — onboarding, PR creation, Smartsheet
write-back, PR merge, feedback loop, and the live dashboard — with **no tokens,
no GitHub account, and no Smartsheet sheet**.

```bash
cd onboarding-automation
python3 -m venv .venv && source .venv/bin/activate   # first time only
pip install -r requirements.txt                       # first time only

python demo/run_demo.py                 # run the flow and print sheet snapshots
python demo/run_demo.py --dashboard     # ...then serve the live dashboard (Ctrl+C to stop)
```

With `--dashboard`, open **http://127.0.0.1:8000** to see the results live.

## What it does

The demo runs the **real** `main.py` and `feedback.py` against throwaway local
stand-ins created in a temp directory:

| Real dependency | Demo stand-in |
|---|---|
| Smartsheet API | a local HTTP server seeded with 3 demo rows |
| GitHub repo | a local *bare* git repo (real clone/branch/commit/push) |
| `gh` CLI | `demo/fake_gh.py` (emulates `pr create` / `list` / `view`) |

Then it walks the story and prints a sheet snapshot after each step:

1. **Onboarding** — 2 valid `Ready` rows open PRs (branches pushed, files
   rendered); 1 invalid row (missing owner) is `Blocked` with a reason.
2. **Merge** — one PR is flipped to merged (simulating a human reviewer).
3. **Feedback** — that row becomes `Merged`; the still-open PR is refreshed.

Everything is written to a temp dir and can be deleted freely. This is demo
scaffolding — **not production code** and not wired into the app.

## Sample output

```
-- AFTER feedback: merged PR -> Merged; open PR refreshed --
Team              Status      Valid  PR / Error
Payments Platform Merged      Pass   https://github.com/demo-org/payments-service/pull/101
Data Insights     PR Created  Pass   https://github.com/demo-org/insights-etl/pull/102
Mobile Web        Blocked     Fail   Missing required field: Owner Email
```
