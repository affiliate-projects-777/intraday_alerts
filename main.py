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

Per-project timezone for the "hasn't started yet" grace period (added 2026-11-17, user: "take
into consideration each project's timezone... during the night we do not expect much traffic"):
GRACE_UNTIL_HOUR now means 5am in EACH project's own local time, not this container's Athens
server time (TZ=Europe/Athens) - the "same calendar day"/window-width logic is untouched and
still runs on server time throughout; only the grace gate's hour comparison is localized per
project. See PROJECT_TIMEZONES/local_hour_for().

Project 4 (Brazino777.io) per-country split (added 2026-11-17, same day, user-confirmed country
list: "io is Argentina, Bolivia, Chile, Colombia, Ecuador and Venezuela"): a single project-level
check for project 4 would either average away each country's own real volume or apply the wrong
one's timezone to the rest, so CLK-01/REG-01/FTD-01/DEP-01/DAT-01 run ONCE PER COUNTRY for
project 4 instead - same floors/windows/grace as any other project, just computed from that
country's own today/yesterday totals and its own local hour. See P4_COUNTRIES/
P4_COUNTRY_TIMEZONES/fetch_p4_country_totals()/run_project_level_rules(). LP-level rules
(CLK-17/REG-17) are NOT split this way - a landing page has no country field joined here, so
project 4's LPs still fall back to this container's own Athens hour (unchanged).
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
# REG_17_WARN_H raised 3->4 on 2026-11-17 (user: "make sure that the alerts are correct...
# after 4h no regs in 4h instead of 3", following a live landing_394 investigation: clicks kept
# climbing (3.3k -> 8.7k+ in the window) while registrations sat flat all day (0->1 by 03:00,
# 1->3 sometime before 14:00, then flat through 16:00+) - a real, severe conversion break, not a
# false positive, but the user's own call was that 3h wasn't enough patience before WARNING.
REG_17_WARN_H, REG_17_CRIT_H, REG_17_MIN_CLICKS_IN_WINDOW = 4, 6, 30
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

# Per-project local timezone, for the grace period above ONLY - GRACE_UNTIL_HOUR means 5am in
# EACH project's own local time, not this container's Athens server time. Added 2026-11-17
# (user: "take into consideration each project's timezone... since during the night we do not
# expect much traffic") after realizing the grace check above used as_of.hour unconverted -
# this container runs on TZ=Europe/Athens (see this repo's own Dockerfile), so a flat "hour < 5"
# test was gating Brazil's/India's/etc's own night by Athens' clock, not theirs (e.g. Athens
# 05:00 in Nov/winter = Brazil ~00:00, well before Brazil's own early-morning quiet hours even
# start - the grace was covering the wrong hours for every non-Athens project). Country per
# project_id from this brand's own domain suffix - PROJECTS' comment above already treats
# .ar/.in/.py as single-country-by-project_id for the same reason; project_id=7 (.in) is assumed
# India (Asia/Kolkata) by that same ccTLD convention - not independently verified against a
# billing/country field, flag if wrong. project_id=4 (Brazino777.io) is deliberately NOT in this
# map: it spans 6 different countries internally (AR/BO/CL/CO/EC/VE - see the daily-check repo's
# P4_COUNTRIES) with no single correct local time, and this check has no per-country split
# (only project/LP level) to pick one correctly - it falls back to this container's own Athens
# hour unconverted, i.e. the exact pre-2026-11-17 behavior, until per-country segmentation is
# added here too.
PROJECT_TIMEZONES = {
    1: 'America/Sao_Paulo',               # Brazino777.bet.br - Brazil
    3: 'America/Mexico_City',             # Brazino777.mx - Mexico
    6: 'America/Argentina/Buenos_Aires',  # Brazino777.ar - Argentina
    7: 'Asia/Kolkata',                    # Brazino777.in - India (assumed, see above)
    10: 'America/Asuncion',               # Brazino777.py - Paraguay
}


def local_hour_for(now, tz_name):
    """Converts `now` (this container's naive wall-clock time, i.e. Athens - TZ=Europe/Athens)
    to `tz_name`'s own local hour, for the grace period's "is it night there" check. Returns
    Athens' own hour unconverted when tz_name is None (project 4's LP-level fallback - see
    PROJECT_TIMEZONES' comment)."""
    if tz_name is None:
        return now.hour
    aware = now.tz_localize('Europe/Athens') if now.tzinfo is None else now.tz_convert('Europe/Athens')
    return aware.tz_convert(tz_name).hour


# Project 4 (Brazino777.io) per-country split, added 2026-11-17 (user-confirmed: "io is
# Argentina, Bolivia, Chile, Colombia, Ecuador and Venezuela" - matches the traffic repo's own
# P4_COUNTRIES, not invented here). A single project-level check for project 4 would mix 6
# countries' volume/timezone together - CLK-01/REG-01/FTD-01/DEP-01/DAT-01 run once per country
# for this project instead, via run_project_level_rules() below. LP-level rules (CLK-17/REG-17)
# are NOT split this way - a landing page has no country field joined here, so project 4's LPs
# still fall back to this container's own Athens hour (local_hour_for(now, None)).
P4_COUNTRIES = ('AR', 'BO', 'CL', 'CO', 'EC', 'VE')
P4_COUNTRY_TIMEZONES = {
    'AR': 'America/Argentina/Buenos_Aires',
    'BO': 'America/La_Paz',
    'CL': 'America/Santiago',
    'CO': 'America/Bogota',
    'EC': 'America/Guayaquil',
    'VE': 'America/Caracas',
}


def fetch_p4_country_totals(engine, today, yesterday):
    """Project 4's clicks/registrations/FTDs/deposit-events by country, today vs yesterday -
    dedicated queries (promo_statistics_countries has country+promo_id directly;
    projects_statistics has a country column fetch_project_totals() doesn't select, same
    precedent as Documents/traffic/health_check/daily_health_check.py's own
    fetch_p4_country_breakdown()). Always returns all 6 countries (0.0 where a country had no
    rows that day), same shape fetch_project_totals() returns per project."""
    excl = ','.join(str(t) for t in EXCLUDED_TAG_IDS)
    countries_sql = ','.join(f"'{c}'" for c in P4_COUNTRIES)

    click_reg_q = text(f"""
        SELECT psc.country AS country, psc.date AS date,
               SUM(psc.clicks) AS clicks, SUM(psc.registrations) AS registrations
        FROM promo_statistics_countries psc
        JOIN promo p ON p.id = psc.promo_id
        JOIN promo_materials pm ON p.promo_material_id = pm.id
        WHERE pm.project_id = 4 AND psc.date IN (:today, :yesterday)
          AND psc.country IN ({countries_sql})
          AND p.partner_id NOT IN (SELECT partner_id FROM partner_tags_ref WHERE tag_id IN ({excl}))
        GROUP BY psc.country, psc.date
    """)
    ftd_dep_q = text(f"""
        SELECT country, date, SUM(is_first_depositor) AS ftds, SUM(number_of_deposits) AS deposits
        FROM projects_statistics
        WHERE project_id = 4 AND date IN (:today, :yesterday) AND country IN ({countries_sql})
          AND partner_id NOT IN (SELECT partner_id FROM partner_tags_ref WHERE tag_id IN ({excl}))
        GROUP BY country, date
    """)
    with engine.connect() as conn:
        cr_df = pd.read_sql(click_reg_q, conn, params={'today': today, 'yesterday': yesterday})
        fd_df = pd.read_sql(ftd_dep_q, conn, params={'today': today, 'yesterday': yesterday})

    def day_val(df, country, col, d):
        if df.empty:
            return 0.0
        row = df[(df['country'] == country) & (df['date'] == d)]
        return float(row[col].sum()) if not row.empty else 0.0

    return {
        c: {
            'clicks': {'today': day_val(cr_df, c, 'clicks', today), 'yesterday': day_val(cr_df, c, 'clicks', yesterday)},
            'registrations': {'today': day_val(cr_df, c, 'registrations', today), 'yesterday': day_val(cr_df, c, 'registrations', yesterday)},
            'ftds': {'today': day_val(fd_df, c, 'ftds', today), 'yesterday': day_val(fd_df, c, 'ftds', yesterday)},
            'deposits': {'today': day_val(fd_df, c, 'deposits', today), 'yesterday': day_val(fd_df, c, 'deposits', yesterday)},
        }
        for c in P4_COUNTRIES
    }


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
def had_no_increase_in(history, as_of, hours, grace_until_hour=GRACE_UNTIL_HOUR, local_hour=None):
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
    check_hour = as_of.hour if local_hour is None else local_hour
    if old_v == 0 and grace_until_hour is not None and check_hour < grace_until_hour:
        # Nothing has happened yet today as of the comparison point - "hasn't started" and
        # "dead" look identical until enough of the day has passed, so don't fire yet. Once
        # old_v > 0 (something ticked, then went flat) this grace no longer applies, at any
        # hour - see GRACE_UNTIL_HOUR's own comment for the real evidence behind this.
        # check_hour (added 2026-11-17): the project's own local hour, not necessarily
        # as_of.hour (this container's Athens server time) - see PROJECT_TIMEZONES/
        # local_hour_for() above.
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


def lp_dominant_partner_am(engine, landing_id, today, metric_col):
    """For one landing page, which partner drove the most of `metric_col` (clicks or
    registrations) today, and that partner's AM - added 2026-11-17 for per-AM chat routing (see
    AM_TELEGRAM_CHATS) on the CLK-17/REG-17 rules. Reuses
    daily_health_check.fetch_account_managers() (the exact same partner_tags lookup the daily
    pipeline already uses) rather than re-deriving AM resolution here. A landing page can carry
    many partners - confirmed live, landing_394 alone has 6 - this names only the DOMINANT one
    by today's volume, same "note the likely driver, don't claim exclusive ownership" precedent
    as the daily pipeline's own dominant_partner enrichment
    (health_check_engine.annotate_project_dominant_partner()). Returns (am_or_None, partner_id_
    or_None, share_pct, other_partners_count) - am is None when there's no traffic yet today on
    this LP, or the dominant partner has no AM tag."""
    q = text(f"""
        SELECT p.partner_id AS partner_id, SUM(ps.{metric_col}) AS v
        FROM promo_statistics ps
        JOIN promo p ON p.id = ps.promo_id
        WHERE p.landing_id = :landing_id AND ps.date = :today
        GROUP BY p.partner_id
        HAVING v > 0
        ORDER BY v DESC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={'landing_id': int(landing_id), 'today': today})
    if df.empty:
        return None, None, 0.0, 0
    total = float(df['v'].sum())
    top_partner = int(df.iloc[0]['partner_id'])
    share_pct = float(df.iloc[0]['v']) / total * 100.0 if total else 0.0
    other_count = len(df) - 1
    import daily_health_check as dhc
    am_map = dhc.fetch_account_managers([top_partner])
    am = am_map.get(top_partner)
    return am, top_partner, share_pct, other_count


# ------------------------------------------------------------- rule evaluators
def check_project_metric(state, now, pid, label, metric, value_today, window_hours, rule_id, metric_label,
                          note_extra='', local_hour=None, entity_key='Project', level='Project'):
    key = f'{pid}|{metric}|{entity_key}'
    hist = append_snapshot(state, key, now, value_today)
    triggered, old_v, latest_v, had_history = had_no_increase_in(hist, now, window_hours, local_hour=local_hour)
    if not triggered:
        return None
    return {
        'rule_id': rule_id, 'metric': metric_label, 'level': level, 'entity': label,
        'project_id': pid, 'value': latest_v, 'baseline': old_v, 'severity': 'CRITICAL',
        'note': f'no increase in >= {window_hours}h (was {old_v}, still {latest_v})' + note_extra,
    }


def run_project_level_rules(state, now, pid, label, t, local_hour, alerts, entity_key='Project', level='Project'):
    """Runs CLK-01/REG-01/FTD-01/DEP-01/DAT-01 for one project-level entity - a whole project for
    every project except 4, or (project 4 only, see P4_COUNTRIES) one of its countries, each with
    its own totals/local_hour/state history (`entity_key` keeps a country's snapshot history
    separate from every other country's and from a plain project's). Appends any fired alerts to
    `alerts` in place."""
    if t['clicks']['yesterday'] >= CLK_01_ELIGIBLE_CLICKS_YDAY:
        clk_window = dynamic_window(CLK_01_WINDOW_H, t['clicks']['yesterday'])
        a = check_project_metric(state, now, pid, label, 'clicks', t['clicks']['today'],
                                  clk_window, 'CLK-01', 'Clicks', local_hour=local_hour,
                                  entity_key=entity_key, level=level)
        if a: alerts.append(a)

    if t['registrations']['yesterday'] >= REG_01_ELIGIBLE_REGS_YDAY:
        reg_window = REG_01_WINDOW_HIGH_H if t['registrations']['yesterday'] < REG_01_LOW_FLOOR_YDAY else REG_01_WINDOW_LOW_H
        reg_window = dynamic_window(reg_window, t['registrations']['yesterday'])
        a = check_project_metric(state, now, pid, label, 'registrations', t['registrations']['today'],
                                  reg_window, 'REG-01', 'Registrations',
                                  f" (window={reg_window}h, yesterday's regs={t['registrations']['yesterday']:.0f})",
                                  local_hour=local_hour, entity_key=entity_key, level=level)
        if a: alerts.append(a)

    if t['ftds']['yesterday'] >= FTD_01_ELIGIBLE_FTDS_YDAY:
        ftd_window = FTD_01_WINDOW_HIGH_H if t['ftds']['yesterday'] < FTD_01_LOW_FLOOR_YDAY else FTD_01_WINDOW_LOW_H
        ftd_window = dynamic_window(ftd_window, t['ftds']['yesterday'])
        a = check_project_metric(state, now, pid, label, 'ftds', t['ftds']['today'],
                                  ftd_window, 'FTD-01', 'FTDs',
                                  f" (window={ftd_window}h, yesterday's FTDs={t['ftds']['yesterday']:.0f})",
                                  local_hour=local_hour, entity_key=entity_key, level=level)
        if a: alerts.append(a)

    if t['deposits']['yesterday'] >= DEP_01_ELIGIBLE_DEPOSITS_YDAY:
        dep_window = dynamic_window(DEP_01_WINDOW_H, t['deposits']['yesterday'])
        a = check_project_metric(state, now, pid, label, 'deposits', t['deposits']['today'],
                                  dep_window, 'DEP-01', 'Deposit events', local_hour=local_hour,
                                  entity_key=entity_key, level=level)
        if a: alerts.append(a)

    for feed in DAT_01_FEEDS:
        if t[feed]['yesterday'] < DAT_01_FEED_FLOORS[feed]:
            continue  # below this feed's own volume floor - "flat" is normal, not a signal
        feed_window = dynamic_window(DAT_01_WINDOW_H, t[feed]['yesterday'])
        key = f'{pid}|{feed}|{entity_key}'
        hist = append_snapshot(state, key, now, t[feed]['today'])
        triggered, old_v, latest_v, had_history = had_no_increase_in(hist, now, feed_window, local_hour=local_hour)
        if triggered:
            alerts.append({
                'rule_id': 'DAT-01', 'metric': f'Data freshness ({feed})', 'level': level,
                'entity': label, 'project_id': pid, 'value': latest_v, 'baseline': old_v,
                'severity': 'CRITICAL',
                'note': f'{feed} feed stale: no increase in >= {feed_window:.1f}h (was {old_v}, still {latest_v})',
            })


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
        # This project's own local hour, for the grace period only (added 2026-11-17) - see
        # PROJECT_TIMEZONES/local_hour_for() above. Computed for every project (including 4,
        # where it's Athens-unconverted) since the LP-level block below always uses it; project
        # 4's own project-level rules use their own per-country local_hour instead, below.
        local_hour = local_hour_for(now, PROJECT_TIMEZONES.get(pid))

        if pid == 4:
            # Project 4 (Brazino777.io): one country's worth of eligibility/window/grace per
            # country, not one project-wide average - see P4_COUNTRIES' own comment above.
            p4_totals = fetch_p4_country_totals(engine, today, yesterday)
            for country in P4_COUNTRIES:
                c_local_hour = local_hour_for(now, P4_COUNTRY_TIMEZONES[country])
                run_project_level_rules(state, now, pid, f'{label} ({country})', p4_totals[country],
                                         c_local_hour, alerts, entity_key=f'Project|{country}', level='Country')
        else:
            run_project_level_rules(state, now, pid, label, t, local_hour, alerts)

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
                trig_warn, old_v, latest_v, _ = had_no_increase_in(hist, now, clk17_warn_window, local_hour=local_hour)
                if trig_warn:
                    trig_crit, _, _, _ = had_no_increase_in(hist, now, clk17_crit_window, local_hour=local_hour)
                    window_shown = clk17_crit_window if trig_crit else clk17_warn_window
                    # Dominant-partner attribution, added 2026-11-17 for per-AM chat routing
                    # (see AM_TELEGRAM_CHATS) - who this LP's alert is "about them" for, since
                    # an LP itself can carry many partners (see lp_dominant_partner_am()'s own
                    # comment). Only queried when an alert is actually firing, not per LP checked.
                    am, dom_partner, dom_share, other_partners = lp_dominant_partner_am(engine, lid, today, 'clicks')
                    note = (f"LP had {lp_yday_c:.0f} clicks yesterday (eligible); "
                            f"no increase in >= {window_shown:.1f}h")
                    if dom_partner is not None:
                        note += (f" (mostly partner_{dom_partner}, {dom_share:.0f}% of today's clicks"
                                 + (f"; {other_partners} other partner(s) also on this LP" if other_partners else "") + ")")
                    alerts.append({
                        'rule_id': 'CLK-17', 'metric': 'Clicks', 'level': 'Landing Page',
                        'entity': lp_label, 'project_id': pid, 'value': latest_v, 'baseline': old_v,
                        'severity': 'CRITICAL' if trig_crit else 'WARNING',
                        'note': note, 'am': am, 'dominant_partner': dom_partner,
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
                        # Dominant-partner attribution (clicks, not registrations - clicks are
                        # the metric actually flowing on this LP; registrations are the broken
                        # one, so attributing by clicks names whose traffic isn't converting,
                        # not an arbitrary near-zero registrations split). See CLK-17's own
                        # comment above for the full reasoning.
                        am, dom_partner, dom_share, other_partners = lp_dominant_partner_am(engine, lid, today, 'clicks')
                        note = f'{c_delta:.0f} clicks in the last {reg17_warn_window:.1f}h, 0 registrations'
                        if dom_partner is not None:
                            note += (f" (mostly partner_{dom_partner}, {dom_share:.0f}% of today's clicks"
                                     + (f"; {other_partners} other partner(s) also on this LP" if other_partners else "") + ")")
                        alerts.append({
                            'rule_id': 'REG-17', 'metric': 'Registrations', 'level': 'Landing Page',
                            'entity': lp_label, 'project_id': pid, 'value': 0, 'baseline': c_delta,
                            'severity': 'CRITICAL' if still_zero_crit else 'WARNING',
                            'note': note, 'am': am, 'dominant_partner': dom_partner,
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


def send_telegram_to_chat(text, chat_id):
    """Same as send_telegram() but to an explicit chat_id instead of the shared
    TELEGRAM_CHAT_ID - added 2026-11-17 for per-AM chat routing (see AM_TELEGRAM_CHATS below)."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        logging.info("Telegram not configured (TELEGRAM_BOT_TOKEN missing) - skipping send")
        return False
    try:
        import requests
        resp = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              data={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}, timeout=15)
        if resp.status_code == 200:
            logging.info(f"Telegram message sent to chat {chat_id}")
            return True
        logging.error(f"Telegram send to chat {chat_id} failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        logging.error(f"Telegram send to chat {chat_id} failed (non-fatal): {e}")
        return False


# ================================================================= per-AM chat routing
# Added 2026-11-17 (user: "create a group of chats on telegram and have one chat for each AM to
# simulate exactly what they get"), same AM names/chat ids as the daily pipeline's own
# alert_telegram.py:AM_TELEGRAM_CHATS - kept as two separate dicts (not imported from one
# another) since this repo already duplicates daily_health_check.py verbatim rather than sharing
# code across the two jobs, per this repo's own established convention (see this file's header).
AM_TELEGRAM_CHATS = {
    'jackie': -5487009071,
    'Fotini': -5195990482,
    'Willard': -5544224465,
    'Christie': -5420792053,
}


def send_am_chat_messages(alerts, now):
    """Splits `alerts` per configured AM and sends each AM's own personal+global digest to their
    own Telegram chat - additive alongside the existing single shared-chat send in __main__, not
    a replacement for it. Personal = CLK-17/REG-17 alerts whose dominant partner (see
    lp_dominant_partner_am()) is that AM's own; global = every project-level alert
    (CLK-01/REG-01/FTD-01/DEP-01/DAT-01 - always market-wide, never personally attributed, same
    precedent as the daily pipeline never routing a PLATFORM-scope alert to one AM) plus any
    LP-level alert whose dominant partner has no AM tag (nowhere else for it to go). An AM with
    neither today sees nothing sent, same "zero alerts -> send nothing" rule this hourly job
    already follows for the shared chat."""
    global_alerts = [a for a in alerts if a.get('am') is None]
    for am, chat_id in AM_TELEGRAM_CHATS.items():
        personal = [a for a in alerts if a.get('am') == am]
        subset = personal + global_alerts
        if not subset:
            continue
        send_telegram_to_chat(format_hourly_message(subset, now), chat_id)


def format_hourly_message(alerts, now):
    """One message per hourly run, grouped by project - resent every hour an issue stays open
    (user's own choice, 2026-11-16: no new-vs-still-open distinction here, unlike the daily
    check's NEW/ESCALATION/REMINDER - every qualifying hour gets its own send).

    FIX (2026-11-18, user: "split the io per country and show me the alerts for each country as
    if they were different projects"): a Country-level row (project 4's own per-country
    CLK-01/REG-01/FTD-01/DEP-01/DAT-01, see run_project_level_rules()'s own `level='Country'`)
    now gets its OWN top-level group, keyed by its already-distinct entity label
    ("Brazino777.io (AR)") exactly like any other project's group - not nested one level down
    under a shared "Brazino777.io" header with that same label repeated a second time inside the
    alert line's own parenthetical, which is what grouping by project_id alone produced before.
    LP-level rows (CLK-17/REG-17) are unaffected - project 4's landing pages still have no
    country field (see run_once()'s own comment), so they still group under plain
    "Brazino777.io".

    FIX (2026-11-18, user: "the language in the hourly alerts should match the daily, now it is
    a bit robotic, we do not need DAT-01" - approved after a before/after sample was shown):
    lines no longer lead with the raw rule_id (daily's own render never shows one either, per
    RULES.md's "Telegram message format" section) - _humanize() turns each rule's already-
    computed `note` text into one plain sentence, same "SEVERITY - description" shape daily
    uses, CRITICAL bolded the same way. Reuses the exact numbers already in `note`/`metric`
    rather than re-deriving them from the raw alert dict, so this is a rendering change only -
    no detection logic touched."""
    import html as _html, re as _re

    FEED_LABEL = {'clicks': 'Clicks feed', 'registrations': 'Registrations feed',
                  'ftds': 'FTD feed', 'deposits': 'Deposits feed'}

    def _num(s):
        try:
            return f"{float(s.replace(',', '')):,.0f}"
        except ValueError:
            return s

    def _humanize(a):
        rid, note, metric, entity = a['rule_id'], a['note'], a['metric'], a['entity']
        if rid == 'DAT-01':
            m = _re.match(r'^(\w+) feed stale: no increase in >= ([\d.]+)h \(was ([\d.]+), still ([\d.]+)\)$', note)
            if m:
                feed, window, _old, latest = m.groups()
                label = FEED_LABEL.get(feed, f'{feed} feed')
                return f"{label} stopped updating: still showing {_num(latest)}, no change in over {float(window):.0f}h"
        elif rid == 'REG-17':
            m = _re.match(r'^([\d,]+) clicks in the last ([\d.]+)h, 0 registrations(.*)$', note)
            if m:
                clicks, window, extra = m.groups()
                return (f"Registrations on {entity}: {_num(clicks)} clicks in the last {float(window):.0f}h but "
                         f"0 sign-ups (likely a tracking break){extra}")
        elif rid == 'CLK-17':
            m = _re.match(r'^LP had ([\d,.]+) clicks yesterday \(eligible\); no increase in >= ([\d.]+)h(.*)$', note)
            if m:
                yday, window, extra = m.groups()
                return (f"Clicks on {entity}: flat for over {float(window):.0f}h "
                         f"(had {_num(yday)} clicks yesterday, so this page is normally active){extra}")
        else:
            m = _re.match(r'^no increase in >= ([\d.]+)h \(was [\d.]+, still ([\d.]+)\)(.*)$', note)
            if m:
                window, latest, extra = m.groups()
                ctx = ''
                extra_m = _re.search(r"yesterday'?s (\w+)=([\d.]+)", extra)
                if extra_m:
                    ctx = f" (yesterday this project had {_num(extra_m.group(2))} {extra_m.group(1)} by this time)"
                return f"{metric}: flat at {_num(latest)} for over {float(window):.0f}h{ctx}"
        return f"{metric}: {note}"  # fallback, unrecognized note shape - show it plain rather than hide it

    groups = {}  # group_label -> list[alert]
    for a in alerts:
        label = a['entity'] if a.get('level') == 'Country' else PROJECTS.get(a['project_id'], str(a['project_id']))
        groups.setdefault(label, []).append(a)
    lines = [f"<b>Hourly check — {now.strftime('%Y-%m-%d %H:%M')}</b>"]
    for label in sorted(groups):
        lines.append(f"\n<b>{_html.escape(label)}</b>")
        for a in sorted(groups[label], key=lambda x: (x['severity'] != 'CRITICAL', x['rule_id'])):
            sev = f"<b>{a['severity']}</b>" if a['severity'] == 'CRITICAL' else a['severity']
            lines.append(f"{sev} - {_html.escape(_humanize(a))}")
    return "\n".join(lines)


if __name__ == '__main__':
    try:
        found = run_once()
        for a in found:
            logging.warning(f"ALERT {a['rule_id']} {a['level']} {a['entity']}: {a['note']}")
        if found:
            now = datetime.now()
            send_telegram(format_hourly_message(found, now))
            send_am_chat_messages(found, now)
    except Exception as e:
        logging.error(f"Run failed: {e}")
        raise
