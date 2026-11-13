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
in intraday_state.json. Nothing is sent anywhere (no Telegram, no webhook) - this is
collection only, exactly as asked: "I will give you the output and you will add the
hourly alerts" tomorrow.
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
KEEP_HOURS = 10  # >= the longest window below (6h) plus margin for a missed/delayed run

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
REG_17_ELIGIBLE_CLICKS_YDAY, REG_17_ELIGIBLE_REGS_YDAY = 100, 10
FTD_01_WINDOW_LOW_H, FTD_01_WINDOW_HIGH_H, FTD_01_LOW_FLOOR_YDAY = 3, 6, 50
DEP_01_WINDOW_H = 2
DAT_01_WINDOW_H = 3
DAT_01_FEEDS = ('clicks', 'registrations', 'ftds', 'deposits')


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
    cutoff = ts - pd.Timedelta(hours=keep_hours)
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
def had_no_increase_in(history, as_of, hours):
    today = as_of.date()
    same_day = sorted((t, v) for t, v in history if t.date() == today and t <= as_of)
    if not same_day:
        return False, None, None, False
    latest_t, latest_v = same_day[-1]
    cutoff = as_of - pd.Timedelta(hours=hours)
    candidates = [(t, v) for t, v in same_day if t <= cutoff]
    if not candidates:
        return False, None, latest_v, False
    old_t, old_v = max(candidates, key=lambda tv: tv[0])
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

        a = check_project_metric(state, now, pid, label, 'clicks', t['clicks']['today'],
                                  CLK_01_WINDOW_H, 'CLK-01', 'Clicks')
        if a: alerts.append(a)

        reg_window = REG_01_WINDOW_HIGH_H if t['registrations']['yesterday'] < REG_01_LOW_FLOOR_YDAY else REG_01_WINDOW_LOW_H
        a = check_project_metric(state, now, pid, label, 'registrations', t['registrations']['today'],
                                  reg_window, 'REG-01', 'Registrations',
                                  f" (window={reg_window}h, yesterday's regs={t['registrations']['yesterday']:.0f})")
        if a: alerts.append(a)

        ftd_window = FTD_01_WINDOW_HIGH_H if t['ftds']['yesterday'] < FTD_01_LOW_FLOOR_YDAY else FTD_01_WINDOW_LOW_H
        a = check_project_metric(state, now, pid, label, 'ftds', t['ftds']['today'],
                                  ftd_window, 'FTD-01', 'FTDs',
                                  f" (window={ftd_window}h, yesterday's FTDs={t['ftds']['yesterday']:.0f})")
        if a: alerts.append(a)

        a = check_project_metric(state, now, pid, label, 'deposits', t['deposits']['today'],
                                  DEP_01_WINDOW_H, 'DEP-01', 'Deposit events')
        if a: alerts.append(a)

        for feed in DAT_01_FEEDS:
            key = f'{pid}|{feed}|Project'
            hist = append_snapshot(state, key, now, t[feed]['today'])
            triggered, old_v, latest_v, had_history = had_no_increase_in(hist, now, DAT_01_WINDOW_H)
            if triggered:
                alerts.append({
                    'rule_id': 'DAT-01', 'metric': f'Data freshness ({feed})', 'level': 'Project',
                    'entity': label, 'project_id': pid, 'value': latest_v, 'baseline': old_v,
                    'severity': 'CRITICAL',
                    'note': f'{feed} feed stale: no increase in >= {DAT_01_WINDOW_H}h (was {old_v}, still {latest_v})',
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
                trig_warn, old_v, latest_v, _ = had_no_increase_in(hist, now, CLK_17_WARN_H)
                if trig_warn:
                    trig_crit, _, _, _ = had_no_increase_in(hist, now, CLK_17_CRIT_H)
                    alerts.append({
                        'rule_id': 'CLK-17', 'metric': 'Clicks', 'level': 'Landing Page',
                        'entity': lp_label, 'project_id': pid, 'value': latest_v, 'baseline': old_v,
                        'severity': 'CRITICAL' if trig_crit else 'WARNING',
                        'note': f"LP had {lp_yday_c:.0f} clicks yesterday (eligible); "
                                f"no increase in >= {CLK_17_CRIT_H if trig_crit else CLK_17_WARN_H}h",
                    })

            if lp_yday_c >= REG_17_ELIGIBLE_CLICKS_YDAY and lp_yday_r >= REG_17_ELIGIBLE_REGS_YDAY:
                c_key = f'{pid}|clicks|{lp_label}'
                r_key = f'{pid}|registrations|{lp_label}'
                c_hist = append_snapshot(state, c_key, now, float(today_c.get(lid, 0.0)))
                r_hist = append_snapshot(state, r_key, now, float(today_r.get(lid, 0.0)))
                today_lw = now.date()
                cutoff_warn = now - pd.Timedelta(hours=REG_17_WARN_H)
                c_same_day = sorted((t2, v) for t2, v in c_hist if t2.date() == today_lw and t2 <= now)
                r_same_day = sorted((t2, v) for t2, v in r_hist if t2.date() == today_lw and t2 <= now)
                c_before = [(t2, v) for t2, v in c_same_day if t2 <= cutoff_warn]
                r_before = [(t2, v) for t2, v in r_same_day if t2 <= cutoff_warn]
                if c_before and r_before:
                    c_delta = c_same_day[-1][1] - max(c_before, key=lambda tv: tv[0])[1]
                    r_delta = r_same_day[-1][1] - max(r_before, key=lambda tv: tv[0])[1]
                    if c_delta >= REG_17_MIN_CLICKS_IN_WINDOW and r_delta == 0:
                        cutoff_crit = now - pd.Timedelta(hours=REG_17_CRIT_H)
                        r_before_crit = [(t2, v) for t2, v in r_same_day if t2 <= cutoff_crit]
                        still_zero_6h = bool(r_before_crit) and r_same_day[-1][1] == max(r_before_crit, key=lambda tv: tv[0])[1]
                        alerts.append({
                            'rule_id': 'REG-17', 'metric': 'Registrations', 'level': 'Landing Page',
                            'entity': lp_label, 'project_id': pid, 'value': 0, 'baseline': c_delta,
                            'severity': 'CRITICAL' if still_zero_6h else 'WARNING',
                            'note': f'{c_delta:.0f} clicks in the last {REG_17_WARN_H}h, 0 registrations',
                        })

    save_state(state)
    record = {'run_at': now.isoformat(), 'alerts': alerts, 'raw': raw_snapshot}
    append_result(record)
    logging.info(f"Run complete: {len(alerts)} alert(s) this hour")
    return alerts


if __name__ == '__main__':
    try:
        found = run_once()
        for a in found:
            logging.warning(f"ALERT {a['rule_id']} {a['level']} {a['entity']}: {a['note']}")
    except Exception as e:
        logging.error(f"Run failed: {e}")
        raise
