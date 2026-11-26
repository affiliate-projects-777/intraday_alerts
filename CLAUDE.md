# intraday_alerts — hourly intraday check AND the daily health check, both deployed here

Dockerized service, one container, two cron schedules: the hourly intraday collector
(`main.py`, `0 * * * *`) this repo started as, plus the daily health check (`run_daily.py`,
`0 10 * * *`) added 2026-11-16. Deployed on the Scripts Middleware VPS (Portainer, same
pattern as `gsheet_reports`/`monday-automation-bot`: a `Dockerfile` + `docker-compose.yml`,
GitOps-polled from this repo's `main` branch).

**Why the daily job lives in this repo, not its own:** it was briefly its own private
repo (`affiliate-projects-777/daily_health_check`) so Portainer could GitOps-pull it, but
that needed a GitHub PAT typed into Portainer's stack-creation form, and the browser
automation doing that kept getting blocked by what looked like a password-manager
extension intercepting the username/token fields. The user's own fix (2026-11-16): just
add the daily job's code to this already-working, already-public, already-deployed repo
instead - no new stack, no new auth, no new env vars (this stack's existing `REPLICA_*`/
`TELEGRAM_*` cover both jobs). The standalone `daily_health_check` repo and its
half-created Portainer stack were abandoned, not deleted - safe to remove once confirmed
unneeded.

The hourly collector's own section below is otherwise unchanged from before the merge.

This is the deployed counterpart to `Documents/traffic/health_check/intraday_check.py`
in the main `traffic` repo - same detection logic (`had_no_increase_in()`/
`append_snapshot()` are copied verbatim, not imported, since this is a separate repo),
but with its OWN direct DB connection instead of that repo's `pull_project()`/
SSH-tunneled `db.py`, because this VPS has direct network access to the replica
(confirmed via `affiliate-projects-777/gsheet_reports/helpers/db.py`'s own `REPLICA_*`
connection, which connects the same way with no tunnel).

## Telegram (added 2026-11-16)

Both jobs send to the SAME Telegram bot/chat (user's own choice: one chat for both
cadences) - `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, one pair of env vars, shared.

**Hourly (`main.py`):** every run that returns any alerts sends ONE message. An issue
that stays open resends every qualifying hour it's still open - deliberately no
NEW/ESCALATION/REMINDER dedup, the user's own choice, traded for simplicity over quieter
delivery. A run with zero alerts sends nothing. `send_telegram()`/`format_hourly_message()`
at the bottom of `main.py` - no idempotency guard: there is no "already sent this hour"
concept to guard against here, since resending every qualifying hour is the intended
behavior.

**Daily (`run_daily.py` -> `daily_health_check.main()`):** the real, unmodified production
pipeline - full NEW/ESCALATION/REMINDER notification logic, suppression, correlation,
idempotency guard (`data/telegram_sent_log.json`, one send per eval_date). See
"Daily health check" below for how it's wired into this repo.

## What this does NOT do

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
- Env vars (Portainer stack, not committed): `REPLICA_HOST`, `REPLICA_PORT`,
  `REPLICA_DATABASE`, `REPLICA_USER`, `REPLICA_PASSWORD` - same values as
  `gsheet_reports`' own `.env` on this VPS (same replica); `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID` - shared by both jobs.

## Daily health check (added 2026-11-16)

`health_check_engine.py`, `daily_health_check.py`, `alert_types.py`, `alert_render.py`,
`alert_mapping.py`, `alert_telegram.py` are copied **verbatim** from
`Documents/traffic/health_check/` (not reimplemented) - every fix/finding baked into them
ships as-is. If that project's files change, re-copy them here rather than hand-editing
these copies, or the two will drift.

**Re-synced 2026-11-26 (three-way merge, not a plain copy).** The two copies had drifted both
ways: this repo had the 2026-11-24 formula-link correlation commits (`FormulaPairNote`,
`formula_linked_rule_ids`) that `traffic` lacked, and `traffic` had the per-country project 4
split (`project_runs()`, `pull_project(country=)`), the `--v2` renderer (`alert_render_v2.py`),
and new 2026-11-26 guards. Before this, the container still ran project 4 as one lumped
"Brazino777.io" with a country-breakdown annotation, so project 4 alerts mostly landed under
"Not partner-specific" instead of reaching their AM. Verified by running both code trees
end to end for 2026-11-25 against the replica with Telegram stubbed: byte-identical messages.
New in `daily_health_check.py`: `wait_for_network()` (waits for DNS before pulling), an
"all projects failed" guard (sends a "NOT an all-clear" notice instead of an empty report),
and bot-token redaction in `log()`. `intraday_check.py` and `run_for_date.py` are local-only
tools and are not part of this deploy.

- `db.py` - a ~20-line shim exposing only `replica_run_query()`, the one function this
  pipeline actually calls from the real `customers/tqa_model/db.py` (confirmed by grep
  before writing it - that file's other functions and the VIEWS_*/AZINO_*/ADMIRAL_*/
  AZ_SSH_* env vars it needs just to import are all unused by this pipeline). Reuses this
  repo's own `REPLICA_*` vars - same replica `main.py` already connects to directly.
- `run_daily.py` - the deploy entrypoint. `daily_health_check.main()` hardcodes three
  Windows-path globals (`OUT_DIR`, `LOG_PATH`, `SENT_LOG_PATH`) at module level; this
  script overrides all three to point at `./data` (plain module-attribute reassignment,
  no edit to the copied file needed) before calling `main()`. Two other Windows-specific
  calls inside `main()` needed no patch, confirmed by reading the file first:
  `notify_windows()` already wraps its PowerShell call in try/except, and the final
  `os.startfile(out_path)` is already wrapped in its own try/except - both just log a
  non-fatal line on Linux.
- `cron/scheduler.cron`'s second line - `0 10 * * *` (once daily, 10:00 `TZ=Europe/
  Athens`, matching this project's own "wait for replication" convention). Deliberately
  **not** added to `entrypoint.sh`'s immediate-on-start run, unlike `main.py` - a job that
  actually sends to a live channel should only run on its intended schedule, not whenever
  someone redeploys the stack.
- `data/run_log.txt`, `data/telegram_sent_log.json`, `data/health_check_<date>.xlsx` -
  this job's own files inside the same bind-mounted `./data` volume `main.py` uses for
  `intraday_state.json`/`intraday_results.jsonl`. No name collisions between the two
  jobs' files.
- **First-deploy duplicate risk, flagged not hidden:** the idempotency log on this
  container starts empty - if a given day's digest was already sent from the dev machine
  before this container's first 10am run, that local send and this one don't share a log,
  so a second copy of that one day's message could go out. One-time transition risk, not
  ongoing.

## Known gap, caught while setting this up (2026-11-13)

`gsheet_reports/.gitignore` has a malformed line -
`helper/__pycache__/configs/service_account.json` - two gitignore patterns that were
meant to be two separate lines got concatenated into one, so `configs/service_account.json`
was never actually matched by either pattern and ended up committed and pushed to
`origin/main`. Not this repo's bug to fix, but worth knowing: every `.gitignore` written
for this org's repos should get one pattern per line, checked, not assumed correct from
how it reads.

## Hourly noise fix (2026-11-26, branch `hourly-noise-fix-2026-11-26`)

Replay of 13 days of real runs showed ~85% of hourly runs alerting, mostly noise. Logic now lives
in `hourly_logic.py` (pure, tested in `tests/test_hourly_logic.py`):
- **A** reset-aware stall check: a counter drop (Brazino777.ar is restated ~12:00 Athens) discards
  earlier history; stall = no strict increase between consecutive snapshots in the window.
- **B** DAT-01 fires only if the feed's own rule (CLK/REG/FTD/DEP-01) did not.
- **C** window = shortest >= base (<=12h) with >=4.6 expected events from the entity's own hourly
  profile (last 7 days of `intraday_results.jsonl`; falls back to yesterday/24 per hour with <3 days
  of history, e.g. project 4 countries until `raw.p4_country_totals` accumulates). Too sparse at
  12h -> no alert.
- **E** `PROJECT_SETTLE_HOUR_ATHENS = {6: 12}`: no project-6 checks before 12:00 Athens.
Replay (project-level, 11-18..11-26): 276 alert-hours -> 20. LP rules (CLK-17/REG-17) unchanged
apart from A. Not changed: per-AM routing, Telegram format, "resend every qualifying hour" (D).
