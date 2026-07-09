/**
 * Cloudflare Worker: reliably trigger the "Deploy dashboard" GitHub workflow
 * on a schedule, so the hosted (anonymized) dashboard auto-refreshes 24/7 —
 * independent of any laptop, and without GitHub's unreliable `schedule:` cron.
 *
 * The Cron Trigger (see wrangler.toml / dashboard Triggers) invokes scheduled()
 * on an interval; it POSTs a workflow_dispatch to the GitHub API.
 *
 * Config (set in the Cloudflare dashboard or via wrangler):
 *   GH_REPO   (plain var)  e.g. "mittalutkarsh/onboarding_smartsheet"
 *   GH_TOKEN  (secret)     a GitHub token with Actions: read/write on that repo
 *
 * Security: use a FINE-GRAINED PAT limited to this one repo with only
 * "Actions" = Read and write. It is stored encrypted as a Worker secret.
 */

const WORKFLOW = "deploy-dashboard.yml";

async function dispatch(env) {
  const url = `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      // GitHub rejects requests without a User-Agent.
      "User-Agent": "onboarding-dashboard-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  // Success is HTTP 204 (No Content).
  if (res.status !== 204) {
    const body = await res.text();
    console.log(`dispatch failed: ${res.status} ${body}`);
  } else {
    console.log("dispatch ok (204)");
  }
  return res.status;
}

export default {
  // Fired by the Cron Trigger.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  // Optional status page (does NOT trigger a deploy — safe to be public).
  async fetch(request, env, ctx) {
    return new Response(
      "Onboarding dashboard cron worker is alive.\n" +
        `Repo: ${env.GH_REPO}\n` +
        "It triggers the Deploy dashboard workflow on its Cron schedule.\n",
      { headers: { "Content-Type": "text/plain" } }
    );
  },
};
