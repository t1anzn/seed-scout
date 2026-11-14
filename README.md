# Seed Scout

Finds freshly seed-funded startups hiring for Applied AI / ML roles. Graduate and
junior roles only (no senior, mid-level, lead or intern roles), in the UK and the
Netherlands only. Runs on GitHub Actions
every 3 hours, posts new roles to Discord, and shows everything on a dashboard.

## Setup (about 10 minutes)

1. **Create a GitHub repo** and upload everything in this folder, keeping the
   folder structure (`.github/workflows/seed-scout.yml` must stay at that path).
2. **Discord webhook**: in your Discord server, Channel settings > Integrations >
   Webhooks > New Webhook > Copy Webhook URL.
3. **Add the secret**: repo Settings > Secrets and variables > Actions >
   New repository secret, name `DISCORD_WEBHOOK_URL`, value = the webhook URL.
4. **Dashboard**: repo Settings > Pages > Source "Deploy from a branch",
   branch `main`, folder `/docs`. The dashboard appears at
   `https://<your-username>.github.io/<repo-name>/`.
5. **First run**: Actions tab > seed-scout > Run workflow. Check the log for
   "feed unavailable" lines.

## Running locally instead

    python3 seed_scout.py --selftest
    python3 seed_scout.py --json docs/jobs.json
    cd docs && python3 -m http.server 8000     # dashboard at http://localhost:8000

## Changing the filters

Top of `seed_scout.py`: `CORE_ROLE` / `ADJACENT_ROLE` (titles), `SENIOR`, `JUNIOR`,
`GRAD_SIGNAL`, `INTERN` and `MAX_YEARS_EXP` (level), `LOCATION_TIERS` (places), `FEEDS` (news sources),
`SLUG_OVERRIDES` (pin a company's job board when guessing misses it).

Saved / Applied / Dismissed marks on the dashboard are stored in your browser,
so they do not sync between your phone and laptop.
