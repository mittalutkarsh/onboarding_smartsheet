# Always-on dashboard refresh (Cloudflare Worker)

Triggers the **Deploy dashboard** GitHub workflow on a reliable schedule, 24/7,
independent of your laptop — replacing GitHub's flaky built-in `schedule:` cron.
Cloudflare's free plan includes Cron Triggers.

You'll set up two things: a **GitHub token** (scoped tight) and a **Worker**.

## 1. Create a scoped GitHub token

GitHub → **Settings → Developer settings → Fine-grained personal access tokens →
Generate new token**:
- **Resource owner:** your account
- **Repository access:** *Only select repositories* → `onboarding_smartsheet`
- **Permissions → Repository → Actions:** **Read and write**
- Generate and copy the token.

This token can *only* trigger Actions on this one repo — minimal blast radius.

## 2A. Deploy the Worker — Dashboard (no CLI, easiest)

1. [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages** →
   **Create** → **Create Worker**. Name it `onboarding-dashboard-cron`, **Deploy**.
2. **Edit code** → paste the contents of [`worker.js`](worker.js) → **Deploy**.
3. **Settings → Variables and Secrets:**
   - Add variable `GH_REPO` = `mittalutkarsh/onboarding_smartsheet` (plain text).
   - Add secret `GH_TOKEN` = the token from step 1 (**Encrypt**).
4. **Settings → Triggers → Cron Triggers → Add** → `*/5 * * * *` → save.

Done — every 5 minutes Cloudflare invokes the Worker, which triggers the deploy.

## 2B. Deploy the Worker — wrangler CLI (alternative)

```bash
npm install -g wrangler
cd cloudflare
wrangler login
wrangler secret put GH_TOKEN        # paste the token when prompted
wrangler deploy                     # uses wrangler.toml (cron + GH_REPO)
```

## 3. Verify

- Visit the Worker's URL (shown after deploy) → it should say *"…worker is
  alive."* (the URL itself does not trigger a deploy — safe/public).
- Within ~5 min, GitHub → **Actions → Deploy dashboard** shows new runs with
  event **schedule**/**workflow_dispatch** appearing on their own.
- Edit your Smartsheet, wait ~5 min, refresh the Pages dashboard — it updates
  with no manual action.

## Notes

- **Freshness:** Cloudflare cron min interval is 1 minute, so you can set
  `*/2 * * * *` or `* * * * *` for fresher updates. Each GitHub build still
  takes ~1 min + CDN, so end-to-end is roughly the cron interval + ~1–2 min.
- **Cost:** Cloudflare free plan covers this easily (Workers free tier =
  100k requests/day; this is ~288/day at 5-min).
- **Rotate** the token if it ever leaks; update the `GH_TOKEN` secret.
