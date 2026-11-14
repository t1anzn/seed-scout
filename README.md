# Seed Scout

Finds graduate and junior Applied AI / Machine Learning roles in the UK and the
Netherlands, startups first. No senior, mid-level, lead or intern roles.
Runs on GitHub Actions every 3 hours, posts new roles to Discord, and shows
everything on a dashboard.

## Where roles come from

| Source | What it adds | Needs |
|---|---|---|
| Arbeitnow | UK + European job boards | nothing |
| Hacker News "Who is hiring?" | startup posts that invite graduates | nothing |
| Startup job boards | companies from seed / Series A news, UK/NL YC companies, and `EXTRA_COMPANIES` | nothing |
| Reed | UK graduate-tagged roles | free key: https://www.reed.co.uk/developers |
| Adzuna | UK + Netherlands aggregator (once a day) | free keys: https://developer.adzuna.com |

## Setup

1. **Discord webhook**: Channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL.
2. **Secrets**: repo Settings > Secrets and variables > Actions > New repository secret.
   Add `DISCORD_WEBHOOK_URL`, and for the extra sources `REED_API_KEY`,
   `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`. Sources without a key are skipped.
3. **Permissions**: Settings > Actions > General > Workflow permissions > Read and write.
4. **Dashboard**: Settings > Pages > Deploy from a branch > `main` / `/docs`.
5. **Run**: Actions tab > seed-scout > Run workflow. The log lists how many roles
   each source returned and which sources were unreachable.

## Running locally

    python3 seed_scout.py --selftest
    python3 seed_scout.py --json docs/jobs.json
    cd docs && python3 -m http.server 8000     # dashboard at http://localhost:8000

## Changing the filters

Top of `seed_scout.py`: `CORE_ROLE` / `ADJACENT_ROLE` (titles), `SENIOR`, `JUNIOR`,
`GRAD_SIGNAL`, `INTERN`, `MAX_YEARS_EXP` (level), `LOCATION_TIERS` (places),
`REED_QUERIES` / `ADZUNA_QUERIES` (searches), `EXTRA_COMPANIES` (startups to
check every run), `AGENCY` (recruiters to drop), `SLUG_OVERRIDES` (pin a job board).

Saved / Applied / Dismissed marks on the dashboard are stored in your browser,
so they do not sync between your phone and laptop.
