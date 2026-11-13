# intraday_alerts — hourly intraday health-check data collector

Dockerized, cron-scheduled (hourly) service that checks whether clicks, registrations,
FTDs, or deposit events have stopped increasing for each project/landing-page, using a
"snapshot now, compare to N hours ago, same calendar day only" mechanism. Deployed on
the Scripts Middleware VPS (Portainer, same pattern as `gsheet_reports`/
`monday-automation-bot`: a `Dockerfile` + `docker-compose.yml`, GitOps-polled from this
repo's `main` branch).

This is the deployed counterpart to `Documents/traffic/health_check/intraday_check.py`
in the main `traffic` repo - same detection logic (`had_no_increase_in()`/
`append_snapshot()` are copied verbatim, not imported, since this is a separate repo),
but with its OWN direct DB connection instead of that repo's `pull_project()`/
SSH-tunneled `db.py`, because this VPS has direct network access to the replica
(confirmed via `affiliate-projects-777/gsheet_reports/helpers/db.py`'s own `REPLICA_*`
connection, which connects the same way with no tunnel).

## What this does NOT do

- **Does not send anything.** No Telegram, no webhook, no alert delivery of any kind -
  collection only. Each hourly run appends its results to
  `data/intraday_results.jsonl` for a human (or the traffic repo's simulator) to read
  later.
- **Does not cover bets/wins.** Confirmed in the traffic repo's own investigation
  (2026-11-13): `project_N_sessions` (the only source for bets/wins) is a once-daily
  batch job with zero rows for "today" until it lands - there is nothing to snapshot
  intraday for those two feeds with this replica's data.
- **Does not use `health_check_engine.py`'s `pull_project()`.** That function resolves
  project_id via `promo -> landing_id -> landings.project_id`; this repo instead joins
  `promo -> promo_materials -> project_id`, the same path `gsheet_reports` already uses
  for the same mapping. Both should agree for ordinary promos - not independently
  cross-checked against every promo type in this replica.

## Layout

- `main.py` - the whole thing: `fetch_project_totals()`/`fetch_lp_totals()` (direct SQL
  via SQLAlchemy, no ORM), `had_no_increase_in()`/`append_snapshot()` (pure, same logic
  as `intraday_check.py`), the seven rule evaluators (`CLK-01`, `CLK-17`, `REG-01`,
  `REG-17`, `FTD-01`, `DEP-01`, `DAT-01`), `run_once()` wiring it together.
- `cron/scheduler.cron` - `0 * * * *` (hourly, on the hour). `entrypoint.sh` also runs
  it once immediately on container start/deploy, so the first data point doesn't wait
  a full hour.
- `data/intraday_state.json` - rolling snapshot history per (project/LP, metric), kept
  to the last ~10 hours only (gitignored, lives in the `./data` volume mount).
- `data/intraday_results.jsonl` - append-only, one JSON line per hourly run: every
  collected raw value plus any alerts that run fired. This is the file meant to be
  read back (by a human, or later by the traffic repo's simulator) - kept indefinitely,
  never pruned by this script.
- `.env` (gitignored, not committed) needs: `REPLICA_HOST`, `REPLICA_PORT`,
  `REPLICA_DATABASE`, `REPLICA_USER`, `REPLICA_PASSWORD` - same values as
  `gsheet_reports`' own `.env` on this VPS (same replica).

## Known gap, caught while setting this up (2026-11-13)

`gsheet_reports/.gitignore` has a malformed line -
`helper/__pycache__/configs/service_account.json` - two gitignore patterns that were
meant to be two separate lines got concatenated into one, so `configs/service_account.json`
was never actually matched by either pattern and ended up committed and pushed to
`origin/main`. Not this repo's bug to fix, but worth knowing: every `.gitignore` written
for this org's repos should get one pattern per line, checked, not assumed correct from
how it reads.
