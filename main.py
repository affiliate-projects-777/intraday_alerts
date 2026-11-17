"""
Hourly intraday health-check collector - the deployed counterpart to
Documents/traffic/health_check/intraday_check.py (same detection mechanism: snapshot
today's live cumulative value each hour, alert when it hasn't increased over the rule's
own window, same-calendar-day only). Runs standalone on this VPS (direct DB connection,
no dependency on the traffic repo's SSH-tunneled db.py) because this host has direct
network access to the brazpartners replica - confirmed by this org's own
gsheet_reports/helpers/db.py, which connects the same way with the same REPLICA_* vars.

Every run (hourly via cron/scheduler.cron, plus once immediately on container start via
entrypoint.sh) appends ONE line to /app/data/intraday_results.jsonl - every run's raw
collected values and any alerts fired that hour, kept indefinitely so a day's worth (or
more) of real hourly data accumulates for review, not just the rolling comparison state
in intraday_state.json.

Telegram sending added 2026-11-16 (user: "now i want through portainer to get the daily
and hourly alerts on the telegram bot"), same bot/chat as the daily check
(affiliate-projects-777/daily_health_check), per the user's own choice. Cadence is
deliberately simple, also the user's own choice: every hourly run that returns any
alerts sends one message, grouped by project - an issue that stays open resends every
qualifying hour for as long as it's open, with NO new-vs-still-open distinction (unlike
the daily check's NEW/ESCALATION/REMINDER eventing). A run with zero alerts sends nothing.
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
STATE_PATH = os.path.join(DATA_DIR, 'intraday_state.json')
RESULTS_PATH = os.path.join(DATA_DIR, 'intraday_results.jsonl')
KEEP_HOURS = 14  # >= the longest window below (the 12h dynamic-window cap) plus margin for a
                 # missed/delayed run - raised from 10 when DYNAMIC_WINDOW_CAP_H (12h) was added
                 # below; at 10 a capped 12h check could never see old-enough history to fire.

# Same project set/labels as Documents/traffic/health_check/daily_health_check.py's
# PROJECTS - duplicated here since this is a separate deployed repo, not an import.
PROJECTS = {
    1: 'Brazino777.bet.br',
    3: 'Brazino777.mx',
    4: 'Brazino777.io',
    6: 'Brazino777.ar',
    7: 'Brazino777.in',
    10: 'Brazino777.py',
}

# Same exclusion as health_check_engine.py's pull_project() (finding #25/#33/#41 in that
# project's AUDIT_HISTORY.md) - tag_id 1/33/34/37/41 are non-partner/test/legacy accounts.
EXCLUDED_TAG_IDS = (1, 33, 34, 37, 41)

# ------------------------------------------------------------- spec constants (same
# values as intraday_check.py - see that file's own header comment for the exact
# Google Sheet row citations)
CLK_01_WINDOW_H = 2
CLK_17_WARN_H, CLK_17_CRIT_H, CLK_17_ELIGIBLE_CLICKS_YDAY = 2, 6, 100
REG_01_WINDOW_LOW_H, REG_01_WINDOW_HIGH_H, REG_01_LOW_FLOOR_YDAY = 2, 4, 100
REG_17_WARN_H, REG_17_CRIT_H, REG_17_MIN_CLICKS_IN_WINDOW = 3, 6, 30
# REG_17_ELIGIBLE_REGS_YDAY raised 2026-11-16 (finding: 85% of hourly runs fired an alert,
# most of it noise) - confirmed real: with a 10/day floor, a LP can legitimately have a
# 3-hour gap with zero registrations just from normal spacing, not a broken handoff. 30/day
# (roughly one every 2-3h on average) makes a genuine 3h gap a real deviation, not noise -
# not independently re-validated against a longer real-history backtest.
REG_17_ELIGIBLE_CLICKS_YDAY, REG_17_ELIGIBLE_REGS_YDAY = 100, 30
FTD_01_WINDOW_LOW_H, FTD_01_WINDOW_HIGH_H, FTD_01_LOW_FLOOR_YDAY = 3, 6, 50
DEP_01_WINDOW_H = 2
DAT_01_WINDOW_H = 3
DAT_01_FEEDS = ('clicks', 'registrations', 'ftds', 'deposits')

# Eligibility floors added 2026-11-16 (finding: 85% of hourly runs fired an alert, almost
# all of it noise) - confirmed live on the real deployed data: Brazino777.py/.bet.br/.in
# (this project's own lowest-volume projects) fired CLK-01/DEP-01/DAT-01 on nearly every
# single hourly run, because CLK-01/DEP-01/DAT-01 had NO volume floor at all - "flat for
# 2-3h" is just normal for a project that gets a trickle of traffic, not an incident.
CLK_01_ELIGIBLE_CLICKS_YDAY = 100
DEP_01_ELIGIBLE_DEPOSITS_YDAY = 10
DAT_01_FEED_FLOORS = {'clicks': CLK_01_ELIGIBLE_CLICKS_YDAY, 'registrations': 5,
                       'deposits': DEP_01_ELIGIBLE_DEPOSITS_YDAY}  # 'ftds' added below, once
                       # FTD_01_ELIGIBLE_FTDS_YDAY exists - was a separate hardcoded "2" before
                       # the 2026-11-17 floor raise, which could have drifted from that constant;
                       # referencing it here instead makes that impossible going forward.
# REG-01/FTD-01 already widen their window on a low-volume day (2h->4h, 3h->6h) - but a
# WIDER window doesn't help when yesterday's volume is literally 0 (confirmed live: several
# of the same noisy alerts above read "yesterday's regs=0"/"yesterday's FTDs=0" in their own
# note) - no window width makes "flat" meaningful against a baseline of zero. These are a
# second, separate eligibility floor on top of the existing window-width adaptation, not a
# replacement for it.
REG_01_ELIGIBLE_REGS_YDAY = 5
# Raised 2->5 on 2026-11-17 (real production evidence, first full day live): Brazino777.in's
# FTD-01/DAT-01(ftds) fired for 8 straight hours, every day, because yesterday's FTDs=2 means
# the window formula is already at its 12h cap - the metric is structurally too lumpy at this
# volume for ANY same-day window to tell "normal spacing" from "dead". .in's own FTD volume
# ranges ~2-5/day (seen both in this window), so 5 excludes it on its thinnest days and still
# checks it, properly patience-scaled, on its fuller ones - not an invented number, the
# project's own real range.
FTD_01_ELIGIBLE_FTDS_YDAY = 5
DAT_01_FEED_FLOORS['ftds'] = FTD_01_ELIGIBLE_FTDS_YDAY

# "Hasn't started yet" grace period, added 2026-11-17 (real production evidence): 3 separate
# projects (including Brazino777.io, a healthy 533-regs/day project) fired REG-01/DEP-01 at
# 02:00 reading "was 0.0, still 0.0" - the dynamic window above only accounts for how LOW a
# day's volume is, not WHEN in the day it typically starts. A comparison baseline of exactly 0
# means literally nothing has happened yet today by that point - indistinguishable from "dead"
# until enough of the day has passed, regardless of how much yesterday's volume justifies a
# short window. Confirmed by this project's own earlier per-project/metric measurement: every
# project's clicks/registrations/FTDs/deposits are reliably nonzero by hour 3-4 server time on
# an ordinary day - 5 gives a margin past that. Only applies when the comparison baseline is
# exactly 0 (nothing started yet); once something has ticked and then gone flat, the existing
# dynamic window keeps applying at any hour.
GRACE_UNTIL_HOUR = 5

# Dynamic patience window added 2026-11-16 (same change, same evidence, as
# Documents/traffic/health_check/intraday_check.py's own identical addition - this repo
# duplicates that file's detection logic rather than importing it, see this file's own header).
# The floors above answer "is this entity worth watching at all"; this answers "how long is a
# flat reading actually normal for THIS entity's own pace" - a project with 2 FTDs/day has an
# ordinary ~12h gap between FTDs, so a flat 3h window is noise any time of day, not just
# overnight. Confirmed on the real 72-run collection in data/intraday_results.jsonl: re-filtering
# the same window with this on top of the floors took 106 open episodes down to 23.
DYNAMIC_WINDOW_SAFETY = 1.5
DYNAMIC_WINDOW_CAP_H = 12


def dynamic_window(base_hours, yesterday_count):
    """Scales a fixed check window up when yesterday's volume for this metric/entity was low
    enough that a flat reading for `base_hours` is just this entity's normal pace, not a stall.
    Never goes below `base_hours`; capped at DYNAMIC_WINDOW_CAP_H so a genuinely dead feed still
    alerts well within a day."""
    if yesterday_count <= 0:
        return max(base_hours, DYNAMIC_WINDOW_CAP_H)
    scaled = 24.0 / yesterday_count * DYNAMIC_WINDOW_SAFETY
    return max(base_hours, min(scaled, DYNAMIC_WINDOW_CAP_H))


def get_engine():
    host = os.environ['REPLICA_HOST']
    port = os.environ['REPLICA_PORT']
    database = os.environ['REPLICA_DATABASE']
    user = os.environ['REPLICA_USER']
    password = os.environ['REPLICA_PASSWORD']
    return create_engine(f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}")


# ------------------------------------------------------------- state store
def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, encoding='utf-8') as f:
        return json.load(f)


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def append_snapshot(state, key, ts, value, keep_hours=KEEP_HOURS):
    hist = state.get(key, [])
    hist.append([ts.isoformat(), float(value)])
    cutoff = ts - pd.Timedelta(hours=float(keep_hours))
    hist = [[t, v] for t, v in hist if pd.Timestamp(t) >= cutoff]
    state[key] = hist
    return [(pd.Timestamp(t), v) for t, v in hist]


def append_result(record):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RESULTS_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, default=str) + '\n')


# ------------------------------------------------------------- pure check logic
# (identical to intraday_check.py's own had_no_increase_in() - see that file for the
# full reasoning on the same-calendar-day guard)
def had_no_increase_in(history, as_of, hours, grace_until_hour=GRACE_UNTIL_HOUR):
    today = as_of.date()
    same_day = sorted((t, v) for t, v in history if t.date() == today and t <= as_of)
    if not same_day:
        return False, None, None, False
    latest_t, latest_v = same_day[-1]
    cutoff = as_of - pd.Timedelta(hours=float(hours))
    candidates = [(t, v) for t, v in same_day if t <= cutoff]
    if not candidates:
        return False, None, latest_v, False
    old_t, old_v = max(candidates, key=lambda tv: tv[0])
    if old_v == 0 and grace_until_hour is not None and as_of.hour < grace_until_hour:
        # Nothing has happened yet today as of the comparison point - "hasn't started" and
        # "dead" look identical until enough of the day has passed, so don't fire yet. Once
        # old_v > 0 (something ticked, then went flat) this grace no longer applies, at any
        # hour - see GRACE_UNTIL_HOUR's own comment for the real evidence behind this.
        return False, old_v, latest_v, True
    return (latest_v <= old_v), old_v, latest_v, True


# ------------------------------------------------------------- data pull (direct SQL,
# no pull_project()/db.py - this host reaches the replica directly, confirmed by
# gsheet_reports/helpers/db.py's own REPLICA_* connection in this same org)
def fetch_project_totals(engine, today, yesterday):
    """Project-level clicks/registrations (promo_statistics via promo->promo_materials,
    same join gsheet_reports/helpers/db.py uses for project_id) and FTDs/deposit-events
    (projects_statistics, which already carries project_id directly)."""
    excl = ','.join(str(t) for t in EXCLUDED_TAG_IDS)

    clicks_q = text(f"""
        SELECT pm.project_id AS project_id, ps.date AS date,
               SUM(ps.clicks) AS clicks, SUM(ps.registrations) AS registrations
        FROM promo_statistics ps
        JOIN promo p ON p.id = ps.promo_id
        JOIN promo_materials pm ON p.promo_material_id = pm.id
        WHERE ps.date IN (:today, :yesterday)
          AND p.partner_id NOT IN (SELECT partner_id FROM partner_tags_ref WHERE tag_id IN ({excl}))
        GROUP BY pm.project_id, ps.date
    """)
    deposits_q = text(f"""
        SELECT project_id, date,
               SUM(is_first_depositor) AS ftds, SUM(number_of_deposits) AS deposits
        FROM projects_statistics
        WHERE date IN (:today, :yesterday)
          AND partner_id NOT IN (SELECT partner_id FROM partner_tags_ref WHERE tag_id IN ({excl}))
        GROUP BY project_id, date
    """)
    with engine.connect() as conn:
        clicks_df = pd.read_sql(clicks_q, conn, params={'today': today, 'yesterday': yesterday})
        deposits_df = pd.read_sql(deposits_q, conn, params={'today': today, 'yesterday': yesterday})

    totals = {}
    for pid in PROJECTS:
        def day_val(df, col, d):
            row = df[(df['project_id'] == pid) & (df['date'] == d)]
            return float(row[col].sum()) if not row.empty else 0.0
        totals[pid] = {
            'clicks': {'today': day_val(clicks_df, 'clicks', today), 'yesterday': day_val(clicks_df, 'clicks', yesterday)},
            'registrations': {'today': day_val(clicks_df, 'registrations', today), 'yesterday': day_val(clicks_df, 'registrations', yesterday)},
            'ftds': {'today': day_val(deposits_df, 'ftds', today), 'yesterday': day_val(deposits_df, 'ftds', yesterday)},
            'deposits': {'today': day_val(deposits_df, 'deposits', today), 'yesterday': day_val(deposits_df, 'deposits', yesterday)},
        }
    return totals


def fetch_lp_totals(engine, pid, today, yesterday):
    """Landing-page-level clicks/registrations for one project (promo.landing_id)."""
    excl = ','.join(str(t) for t in EXCLUDED_TAG_IDS)
    q = text(f"""
        SELECT p.landing_id AS landing_id, ps.date AS date,
               SUM(ps.clicks) AS clicks, SUM(ps.registrations) AS registrations
        FROM promo_statistics ps
        JOIN promo p ON p.id = ps.promo_id
        JOIN promo_materials pm ON p.promo_material_id = pm.id
        WHERE pm.project_id = :pid AND ps.date IN (:today, :yesterday)
          AND p.landing_id IS NOT NULL
          AND p.partner_id NOT IN (SELECT partner_id FROM partner_tags_ref WHERE tag_id IN ({excl}))
        GROUP BY p.landing_id, ps.date
    """)
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={'pid': pid, 'today': today, 'yesterday': yesterday})
    today_c = df[df['date'] == today].set_index('landing_id')['clicks'] if not df.empty else pd.Series(dtype=float)
    today_r = df[df['date'] == today].set_index('landing_id')['registrations'] if not df.empty else pd.Series(dtype=float)
    yday_c = df[df['date'] == yesterday].set_index('landing_id')['clicks'] if not df.empty else pd.Series(dtype=float)
    yday_r = df[df['date'] == yesterday].set_index('landing_id')['registrations'] if not df.empty else pd.Series(dtype=float)
    return today_c, today_r, yday_c, yday_r


# ------------------------------------------------------------- rule evaluators
def check_project_metric(state, now, pid, label, metric, value_today, window_hours, rule_id, metric_label, note_extra=''):
    key = f'{pid}|{metric}|Project'
    hist = append_snapshot(state, key, now, value_today)
    triggered, old_v, latest_v, had_history = had_no_increase_in(hist, now, window_hours)
    if not triggered:
        return None
    return {
        'rule_id': rule_id, 'metric': metric_label, 'level': 'Project', 'entity': label,
        'project_id': pid, 'value': latest_v, 'baseline': old_v, 'severity': 'CRITICAL',
        'note': f'no increase in >= {window_hours}h (was {old_v}, still {latest_v})' + note_extra,
    }


def run_once():
    now = pd.Timestamp.now()
    today = now.normalize().date()
    yesterday = today - timedelta(days=1)
    state = load_state()
    alerts = []
    raw_snapshot = {}

    engine = get_engine()
    totals = fetch_project_totals(engine, today, yesterday)
    raw_snapshot['project_totals'] = totals

    for pid, label in PROJECTS.items():
        t = totals[pid]

        if t['clicks']['yesterday'] >= CLK_01_ELIGIBLE_CLICKS_YDAY:
            clk_window = dynamic_window(CLK_01_WINDOW_H, t['clicks']['yesterday'])
            a = check_project_metric(state, now, pid, label, 'clicks', t['clicks']['today'],
                                      clk_window, 'CLK-01', 'Clicks')
            if a: alerts.append(a)

        if t['registrations']['yesterday'] >= REG_01_ELIGIBLE_REGS_YDAY:
            reg_window = REG_01_WINDOW_HIGH_H if t['registrations']['yesterday'] < REG_01_LOW_FLOOR_YDAY else REG_01_WINDOW_LOW_H
            reg_window = dynamic_window(reg_window, t['registrations']['yesterday'])
            a = check_project_metric(state, now, pid, label, 'registrations', t['registrations']['today'],
                                      reg_window, 'REG-01', 'Registrations',
                                      f" (window={reg_window}h, yesterday's regs={t['registrations']['yesterday']:.0f})")
            if a: alerts.append(a)

        if t['ftds']['yesterday'] >= FTD_01_ELIGIBLE_FTDS_YDAY:
            ftd_window = FTD_01_WINDOW_HIGH_H if t['ftds']['yesterday'] < FTD_01_LOW_FLOOR_YDAY else FTD_01_WINDOW_LOW_H
            ftd_window = dynamic_window(ftd_window, t['ftds']['yesterday'])
            a = check_project_metric(state, now, pid, label, 'ftds', t['ftds']['today'],
                                      ftd_window, 'FTD-01', 'FTDs',
                                      f" (window={ftd_window}h, yesterday's FTDs={t['ftds']['yesterday']:.0f})")
            if a: alerts.append(a)

        if t['deposits']['yesterday'] >= DEP_01_ELIGIBLE_DEPOSITS_YDAY:
            dep_window = dynamic_window(DEP_01_WINDOW_H, t['deposits']['yesterday'])
            a = check_project_metric(state, now, pid, label, 'deposits', t['deposits']['today'],
                                      dep_window, 'DEP-01', 'Deposit events')
            if a: alerts.append(a)

        for feed in DAT_01_FEEDS:
            if t[feed]['yesterday'] < DAT_01_FEED_FLOORS[feed]:
                continue  # below this feed's own volume floor - "flat" is normal, not a signal
            feed_window = dynamic_window(DAT_01_WINDOW_H, t[feed]['yesterday'])
            key = f'{pid}|{feed}|Project'
            hist = append_snapshot(state, key, now, t[feed]['today'])
            triggered, old_v, latest_v, had_history = had_no_increase_in(hist, now, feed_window)
            if triggered:
                alerts.append({
                    'rule_id': 'DAT-01', 'metric': f'Data freshness ({feed})', 'level': 'Project',
                    'entity': label, 'project_id': pid, 'value': latest_v, 'baseline': old_v,
                    'severity': 'CRITICAL',
                    'note': f'{feed} feed stale: no increase in >= {feed_window:.1f}h (was {old_v}, still {latest_v})',
                })

        try:
            today_c, today_r, yday_c, yday_r = fetch_lp_totals(engine, pid, today, yesterday)
        except Exception as e:
            logging.error(f"[project {pid}] LP-level fetch failed: {e}")
            continue
        raw_snapshot.setdefault('lp_totals', {})[pid] = {
            'today_clicks': today_c.to_dict(), 'today_regs': today_r.to_dict(),
            'yday_clicks': yday_c.to_dict(), 'yday_regs': yday_r.to_dict(),
        }

        for lid in today_c.index:
            lp_label = f'landing_{int(lid)}'
            lp_yday_c = float(yday_c.get(lid, 0.0))
            lp_yday_r = float(yday_r.get(lid, 0.0))

            if lp_yday_c >= CLK_17_ELIGIBLE_CLICKS_YDAY:
                key = f'{pid}|clicks|{lp_label}'
                hist = append_snapshot(state, key, now, float(today_c.get(lid, 0.0)))
                # Dynamic-scaled 2026-11-17, same formula/reasoning as the project-level rules
                # (see dynamic_window()'s own docstring) - an LP right at the 100-click
                # eligibility floor shouldn't get the same 2h/6h patience as one doing
                # thousands/day. In practice this floor is already high enough that neither
                # window moves for any LP currently eligible (24/100*1.5=0.36h, nowhere near
                # either base) - consistent now, with headroom if the floor is ever lowered.
                clk17_warn_window = dynamic_window(CLK_17_WARN_H, lp_yday_c)
                clk17_crit_window = dynamic_window(CLK_17_CRIT_H, lp_yday_c)
                trig_warn, old_v, latest_v, _ = had_no_increase_in(hist, now, clk17_warn_window)
                if trig_warn:
                    trig_crit, _, _, _ = had_no_increase_in(hist, now, clk17_crit_window)
                    window_shown = clk17_crit_window if trig_crit else clk17_warn_window
                    alerts.append({
                        'rule_id': 'CLK-17', 'metric': 'Clicks', 'level': 'Landing Page',
                        'entity': lp_label, 'project_id': pid, 'value': latest_v, 'baseline': old_v,
                        'severity': 'CRITICAL' if trig_crit else 'WARNING',
                        'note': f"LP had {lp_yday_c:.0f} clicks yesterday (eligible); "
                                f"no increase in >= {window_shown:.1f}h",
                    })

            if lp_yday_c >= REG_17_ELIGIBLE_CLICKS_YDAY and lp_yday_r >= REG_17_ELIGIBLE_REGS_YDAY:
                c_key = f'{pid}|clicks|{lp_label}'
                r_key = f'{pid}|registrations|{lp_label}'
                c_hist = append_snapshot(state, c_key, now, float(today_c.get(lid, 0.0)))
                r_hist = append_snapshot(state, r_key, now, float(today_r.get(lid, 0.0)))
                # Dynamic-scaled 2026-11-17, same reasoning as CLK-17 above - scaled by this
                # LP's own yesterday registrations (the metric REG-17 is actually waiting on),
                # not its clicks. Floor (regs>=30) already keeps this moot in practice
                # (24/30*1.5=1.2h, under both bases) - consistency/headroom, same as CLK-17.
                reg17_warn_window = dynamic_window(REG_17_WARN_H, lp_yday_r)
                reg17_crit_window = dynamic_window(REG_17_CRIT_H, lp_yday_r)
                today_lw = now.date()
                cutoff_warn = now - pd.Timedelta(hours=float(reg17_warn_window))
                c_same_day = sorted((t2, v) for t2, v in c_hist if t2.date() == today_lw and t2 <= now)
                r_same_day = sorted((t2, v) for t2, v in r_hist if t2.date() == today_lw and t2 <= now)
                c_before = [(t2, v) for t2, v in c_same_day if t2 <= cutoff_warn]
                r_before = [(t2, v) for t2, v in r_same_day if t2 <= cutoff_warn]
                if c_before and r_before:
                    c_delta = c_same_day[-1][1] - max(c_before, key=lambda tv: tv[0])[1]
                    r_delta = r_same_day[-1][1] - max(r_before, key=lambda tv: tv[0])[1]
                    if c_delta >= REG_17_MIN_CLICKS_IN_WINDOW and r_delta == 0:
                        cutoff_crit = now - pd.Timedelta(hours=float(reg17_crit_window))
                        r_before_crit = [(t2, v) for t2, v in r_same_day if t2 <= cutoff_crit]
                        still_zero_crit = bool(r_before_crit) and r_same_day[-1][1] == max(r_before_crit, key=lambda tv: tv[0])[1]
                        alerts.append({
                            'rule_id': 'REG-17', 'metric': 'Registrations', 'level': 'Landing Page',
                            'entity': lp_label, 'project_id': pid, 'value': 0, 'baseline': c_delta,
                            'severity': 'CRITICAL' if still_zero_crit else 'WARNING',
                            'note': f'{c_delta:.0f} clicks in the last {reg17_warn_window:.1f}h, 0 registrations',
                        })

    save_state(state)
    record = {'run_at': now.isoformat(), 'alerts': alerts, 'raw': raw_snapshot}
    append_result(record)
    logging.info(f"Run complete: {len(alerts)} alert(s) this hour")
    return alerts


def send_telegram(text):
    """Same contract as Documents/traffic/health_check/daily_health_check.py's own
    send_telegram(): True only on an actual 200 from Telegram, logs and returns False
    otherwise (not configured, HTTP error, or an exception) - never raises, never retries."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        logging.info("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing) - skipping send")
        return False
    try:
        import requests
        resp = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              data={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}, timeout=15)
        if resp.status_code == 200:
            logging.info("Telegram message sent")
            return True
        logging.error(f"Telegram send failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        logging.error(f"Telegram send failed (non-fatal): {e}")
        return False


def format_hourly_message(alerts, now):
    """One message per hourly run, grouped by project - resent every hour an issue stays open
    (user's own choice, 2026-11-16: no new-vs-still-open distinction here, unlike the daily
    check's NEW/ESCALATION/REMINDER - every qualifying hour gets its own send)."""
    import html as _html
    by_project = {}
    for a in alerts:
        by_project.setdefault(a['project_id'], []).append(a)
    lines = [f"<b>Hourly check — {now.strftime('%Y-%m-%d %H:%M')}</b>"]
    for pid in sorted(by_project, key=lambda p: PROJECTS.get(p, str(p))):
        label = PROJECTS.get(pid, str(pid))
        lines.append(f"\n<b>{_html.escape(label)}</b>")
        for a in sorted(by_project[pid], key=lambda x: (x['severity'] != 'CRITICAL', x['rule_id'])):
            entity = a['entity']
            entity_part = f" ({_html.escape(entity)})" if entity != label else ""
            lines.append(f"[{a['severity']}] {a['rule_id']} - {_html.escape(a['metric'])}{entity_part}\n"
                          f"    {_html.escape(a['note'])}")
    return "\n".join(lines)


if __name__ == '__main__':
    try:
        found = run_once()
        for a in found:
            logging.warning(f"ALERT {a['rule_id']} {a['level']} {a['entity']}: {a['note']}")
        if found:
            send_telegram(format_hourly_message(found, datetime.now()))
    except Exception as e:
        logging.error(f"Run failed: {e}")
        raise
