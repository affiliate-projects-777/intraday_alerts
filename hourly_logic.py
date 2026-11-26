"""
Pure (no DB / no sqlalchemy) detection helpers for the hourly intraday check, added 2026-11-26
after a replay of 13 days of real runs (data/intraday_results.jsonl) showed the hourly alerts were
mostly noise. Kept separate from main.py so they can be unit-tested and replayed without a
database. Three fixes live here:

A. Reset-aware stall check (`had_no_increase_in`). The old check compared the latest value with
   a snapshot from `hours` ago and fired when latest <= old. Brazino777.ar's cumulative "today"
   counters are RESTATED during the day (e.g. 11-26: registrations climbed to 213 by 11:00
   Athens, then dropped to 36 at 12:00 when the batch re-aggregated). After a drop every later
   hour read "no increase (was 213, still 49)" until the new series passed 213 - dozens of
   false alerts. Now: a drop is a reset - history before the last drop is discarded, and a
   stall means "no strict increase between ANY consecutive snapshots in the window".

C. Expected-events window (`choose_window`/`expected_events`). The old window was
   24/yesterday_count*1.5 hours: for steady random arrivals that is a 22% chance (e^-1.5) of a
   spurious "stall" on every check, and it ignored that evenings/nights are quieter. Now the
   window is the shortest one (>= the rule's base, <= 12h) in which at least MIN_EXPECTED events
   were EXPECTED (~1% chance of an empty window, e^-4.6), where "expected" comes from this
   entity's own hourly profile over the last PROFILE_DAYS days (falling back to yesterday/24
   per hour while there is too little history). If even 12h cannot reach MIN_EXPECTED the
   entity is too sparse for an hourly stall rule and is not alerted on.
"""
import math
from collections import defaultdict

MIN_EXPECTED = 4.6        # e^-4.6 ~= 1% chance of an empty window at the normal rate
MAX_WINDOW_H = 12.0
WINDOW_STEP_H = 0.5
PROFILE_DAYS = 7
MIN_PROFILE_DAYS = 3

METRICS = ('clicks', 'registrations', 'ftds', 'deposits')


# ------------------------------------------------------------------ A: reset-aware stall check
def settled_history(history, as_of):
    """Same-calendar-day snapshots up to `as_of`, sorted, truncated at the LAST drop (a value
    lower than the one before it = the source restated/reset the counter)."""
    today = as_of.date()
    h = sorted((t, v) for t, v in history if t.date() == today and t <= as_of)
    last_drop = 0
    for i in range(1, len(h)):
        if h[i][1] < h[i - 1][1]:
            last_drop = i
    return h[last_drop:]


def had_no_increase_in(history, as_of, hours, grace_until_hour=5, local_hour=None):
    """Returns (stalled, old_value, latest_value, had_history) like the pre-2026-11-26 version,
    but stalled = no strict increase between any consecutive snapshots from the comparison
    point on, and history before the last counter drop is ignored (see module docstring, A)."""
    same_day = settled_history(history, as_of)
    if not same_day:
        return False, None, None, False
    latest_t, latest_v = same_day[-1]
    cutoff = as_of - _hours(hours)
    candidates = [(t, v) for t, v in same_day if t <= cutoff]
    if not candidates:
        return False, None, latest_v, False
    old_t, old_v = max(candidates, key=lambda tv: tv[0])
    check_hour = as_of.hour if local_hour is None else local_hour
    if old_v == 0 and grace_until_hour is not None and check_hour < grace_until_hour:
        return False, old_v, latest_v, True
    seg = [(t, v) for t, v in same_day if t >= old_t]
    increased = any(b[1] > a[1] for a, b in zip(seg, seg[1:]))
    return (not increased), old_v, latest_v, True


def _hours(h):
    import pandas as pd
    return pd.Timedelta(hours=float(h))


# ------------------------------------------------------------------ C: expected-events window
def build_profile(records, today, days=PROFILE_DAYS):
    """records: parsed lines of intraday_results.jsonl (oldest first). Returns
    {(entity, metric): (n_days, {athens_hour: mean events in that hour})} where entity is
    'pid' (whole project) or 'pid|CC' (a project-4 country). An increment observed at a run at
    hour H covers the hour before it, so it is bucketed under H. Only completed days before
    `today`, last `days` of them. Pairs where the counter dropped are skipped."""
    series = defaultdict(list)   # (entity, metric) -> [(datetime, value)]
    for rec in records:
        t = _parse(rec['run_at'])
        raw = rec.get('raw', {})
        for pid, m in raw.get('project_totals', {}).items():
            for f in METRICS:
                if f in m:
                    series[(str(pid), f)].append((t, float(m[f]['today'])))
        for cc, m in raw.get('p4_country_totals', {}).items():
            for f in METRICS:
                if f in m:
                    series[(f'4|{cc}', f)].append((t, float(m[f]['today'])))
    out = {}
    for key, pts in series.items():
        per_day = defaultdict(lambda: defaultdict(float))
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t1.date() != t0.date() or t1.date() >= today:
                continue
            if v1 < v0:
                continue
            per_day[t1.date()][t1.hour] += v1 - v0
        dates = sorted(per_day)[-days:]
        if not dates:
            continue
        hour_means = {}
        for hr in range(24):
            vals = [per_day[d][hr] for d in dates if hr in per_day[d]]
            if vals:
                hour_means[hr] = sum(vals) / len(vals)
        out[key] = (len(dates), hour_means)
    return out


def _parse(s):
    import pandas as pd
    return pd.Timestamp(s[:19])


def expected_events(profile_entry, now, window_h, yesterday_total):
    """Expected number of events in the `window_h` hours ending at `now`."""
    daily_avg_per_h = max(float(yesterday_total), 0.0) / 24.0
    if not profile_entry or profile_entry[0] < MIN_PROFILE_DAYS:
        return daily_avg_per_h * window_h
    hour_means = profile_entry[1]
    total, remaining, i = 0.0, float(window_h), 0
    while remaining > 1e-9:
        w = min(1.0, remaining)
        total += w * hour_means.get((now.hour - i) % 24, daily_avg_per_h)
        remaining -= w
        i += 1
    return total


def choose_window(base_h, expected_fn, min_expected=MIN_EXPECTED, cap_h=MAX_WINDOW_H):
    """Shortest window >= base_h (0.5h steps, <= cap_h) with expected_fn(window) >= min_expected,
    or None when even cap_h is too sparse for a meaningful stall alert."""
    w = float(base_h)
    while w <= cap_h + 1e-9:
        if expected_fn(w) >= min_expected:
            return w
        w += WINDOW_STEP_H
    return None
