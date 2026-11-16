"""Deploy entrypoint for the daily health check on the Scripts Middleware VPS. Runs the real,
unmodified daily_health_check.main() pipeline (same file as Documents/traffic/health_check/
daily_health_check.py - copied in, not reimplemented, so every fix/finding baked into it ships
as-is) with three module-level globals overridden first, since main() itself is not
parameterized and these three are hardcoded Windows paths in the committed file:

  - OUT_DIR: where the Excel report is written
  - LOG_PATH (= OUT_DIR/run_log.txt): log()'s own log file
  - SENT_LOG_PATH (= OUT_DIR/telegram_sent_log.json): the Telegram idempotency guard's store

All three are plain module attributes read by name inside daily_health_check.py's own
functions at call time, so reassigning them here (before calling main()) is enough - no edit
to the copied file itself. Two things main() ALREADY handles gracefully on Linux, confirmed by
reading the file before this deploy, so no patch needed for them either:
  - notify_windows() wraps its subprocess.Popen(['powershell', ...]) call in try/except -
    PowerShell doesn't exist on this container, so it just logs "notification failed
    (non-fatal)" and moves on.
  - The final os.startfile(out_path) call is already wrapped in its own try/except.

Scheduled via cron/scheduler.cron at 10:00 server time (TZ=Europe/Athens) - NOT run
immediately on container start, unlike intraday_alerts/main.py's own entrypoint: an
immediate run here would fire a REAL Telegram send at whatever moment the container happens
to be deployed/redeployed, independent of the data-replication-lag reasoning that picked 10am
in the first place. Collection-only jobs (intraday_alerts) can safely run on deploy; a job
that actually sends to a live channel should only run on its intended schedule.
"""
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

import daily_health_check as dhc  # noqa: E402  (after DATA_DIR exists, before any dhc.* call)

dhc.OUT_DIR = DATA_DIR
dhc.LOG_PATH = os.path.join(DATA_DIR, 'run_log.txt')
dhc.SENT_LOG_PATH = os.path.join(DATA_DIR, 'telegram_sent_log.json')

dhc.main()
