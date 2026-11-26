import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import pytest
from hourly_logic import (had_no_increase_in, settled_history, build_profile, expected_events,
                          choose_window, MIN_EXPECTED)

D = '2026-11-26'
def ts(h, m=0): return pd.Timestamp(f'{D} {h:02d}:{m:02d}')
def series(pairs): return [(ts(h), float(v)) for h, v in pairs]


# ---- A: reset-aware stall check
AR_REGS_1126 = [(3, 43), (4, 91), (5, 129), (6, 157), (7, 177), (8, 195), (9, 204), (10, 206),
                (11, 213), (12, 36), (13, 38), (14, 43), (15, 49)]

def old_rule(hist, now, hours):
    cutoff = now - pd.Timedelta(hours=hours)
    old = max((x for x in hist if x[0] <= cutoff), key=lambda x: x[0])
    return hist[-1][1] <= old[1]

def test_real_ar_restatement_no_longer_alerts():
    h = series(AR_REGS_1126)
    assert old_rule(h, ts(15), 4) is True               # the production bug: "was 213, still 49"
    stalled, old_v, latest_v, _ = had_no_increase_in(h, ts(15), 4, local_hour=12)
    assert stalled is False                              # post-reset series is climbing 36->49

def test_flat_after_a_reset_still_alerts():
    h = series([(10, 200), (11, 213), (12, 36), (13, 36), (14, 36), (15, 36), (16, 36)])
    stalled, old_v, latest_v, _ = had_no_increase_in(h, ts(16), 4, local_hour=12)
    assert stalled is True and old_v == 36 and latest_v == 36

def test_too_little_history_after_reset_cannot_judge():
    h = series([(10, 200), (11, 213), (12, 36), (13, 36)])
    stalled, *_ , had = had_no_increase_in(h, ts(13), 4, local_hour=12)
    assert stalled is False and had is False

def test_plain_flat_and_plain_increase():
    assert had_no_increase_in(series([(8, 5), (9, 5), (10, 5)]), ts(10), 2, local_hour=12)[0] is True
    assert had_no_increase_in(series([(8, 5), (9, 5), (10, 6)]), ts(10), 2, local_hour=12)[0] is False

def test_increase_then_flat_inside_window_is_not_a_stall():
    # climbed 5->6 at 9:00 then flat: window 2h from 8:00 saw an increase
    assert had_no_increase_in(series([(8, 5), (9, 6), (10, 6)]), ts(10), 2, local_hour=12)[0] is False

def test_grace_when_nothing_started():
    h = series([(0, 0), (1, 0), (2, 0)])
    assert had_no_increase_in(h, ts(2), 2, local_hour=2)[0] is False
    assert had_no_increase_in(h, ts(2), 2, local_hour=9)[0] is True


# ---- C: expected-events window
def test_choose_window_high_volume_keeps_base_and_sparse_is_none():
    assert choose_window(2, lambda w: 10 * w) == 2
    assert choose_window(2, lambda w: 0.2 * w) is None        # 0.2/h * 12h = 2.4 < 4.6
    assert choose_window(2, lambda w: 1.0 * w) == 4.6 or choose_window(2, lambda w: 1.0 * w) == 5.0

def test_fallback_when_no_profile_is_daily_average():
    assert expected_events(None, ts(10), 3, 48) == pytest.approx(6.0)   # 48/24*3

def _rec(day, hour, today):
    return {'run_at': f'2026-11-{day:02d}T{hour:02d}:00:00',
            'raw': {'project_totals': {'3': {'deposits': {'today': today, 'yesterday': 0},
                                             'clicks': {'today': 0, 'yesterday': 0}}}}}

def test_profile_learns_quiet_and_busy_hours_and_ignores_drops():
    recs = []
    for day in (20, 21, 22):
        v = 0
        for hr in range(0, 24):
            v += 10 if 12 <= hr <= 20 else 1          # busy afternoon, quiet otherwise
            recs.append(_rec(day, hr, v))
    prof = build_profile(recs, pd.Timestamp('2026-11-26').date())
    n, hm = prof[('3', 'deposits')]
    assert n == 3 and hm[15] == 10 and hm[3] == 1
    now = pd.Timestamp('2026-11-26 04:00')
    # 4h at night expects ~4 events, 4h in the afternoon ~40: same rule, different patience
    assert expected_events(prof[('3', 'deposits')], now, 4, 999) == pytest.approx(4.0)
    assert expected_events(prof[('3', 'deposits')], pd.Timestamp('2026-11-26 16:00'), 4, 999) == pytest.approx(40.0)

def test_profile_skips_counter_drops():
    recs = [_rec(20, 10, 100), _rec(20, 11, 10), _rec(20, 12, 15)]
    prof = build_profile(recs, pd.Timestamp('2026-11-26').date())
    assert prof[('3', 'deposits')][1] == {12: 5.0}


# ---- B + E at the main.py level
def _t(c=0, r=0, f=0, d=0, yc=0, yr=0, yf=0, yd=0):
    return {'clicks': {'today': c, 'yesterday': yc}, 'registrations': {'today': r, 'yesterday': yr},
            'ftds': {'today': f, 'yesterday': yf}, 'deposits': {'today': d, 'yesterday': yd}}

main = pytest.importorskip('main')

def _run(pid, hours_values, yday, now_hour):
    state, alerts = {}, []
    for h, d in hours_values:
        now = pd.Timestamp(f'{D} {h:02d}:00')
        alerts.clear()
        main.run_project_level_rules(state, now, pid, 'X', _t(d=d, yd=yday), 12, alerts)
    return alerts

def test_dat01_is_suppressed_when_own_rule_fires():
    a = _run(3, [(h, 50) for h in range(8, 16)], yday=400, now_hour=15)
    rules = [x['rule_id'] for x in a]
    assert 'DEP-01' in rules and 'DAT-01' not in rules

def test_argentina_is_checked_all_day_like_every_other_project():
    # the old special case skipped project 6 before 12:00 Athens; now a flat running total alerts at any hour
    flat = [(h, 50) for h in range(3, 11)]
    assert any(x['rule_id'] == 'DEP-01' for x in _run(6, flat, yday=400, now_hour=10))
    assert not hasattr(main, 'PROJECT_SETTLE_HOUR_ATHENS')


# ---- landing pages use the same profile-based windows
from hourly_logic import lp_entity, lp_windows

def test_lp_entity_normalises_ids():
    assert lp_entity(4, 394) == lp_entity('4', '394.0') == 'lp|4|394'

def _lp_rec(day, hour, clicks):
    return {'run_at': f'2026-11-{day:02d}T{hour:02d}:00:00',
            'raw': {'project_totals': {}, 'lp_totals': {'4': {'today_clicks': {'394': clicks}, 'today_regs': {'394': clicks // 10}}}}}

def test_lp_profile_is_learned_and_quiet_night_widens_the_window():
    recs = []
    for day in (20, 21, 22):
        v = 0
        for hr in range(24):
            v += 40 if 12 <= hr <= 20 else 2      # busy afternoon, very quiet night
            recs.append(_lp_rec(day, hr, v))
    prof = build_profile(recs, pd.Timestamp('2026-11-26').date())
    entry = prof[('lp|4|394', 'clicks')]
    assert entry[0] == 3 and entry[1][15] == 40 and entry[1][3] == 2
    afternoon = lp_windows(entry, pd.Timestamp('2026-11-26 16:00'), 400, 2, 6)
    night = lp_windows(entry, pd.Timestamp('2026-11-26 04:00'), 400, 2, 6)
    assert afternoon == (2.0, 6.0)                  # plenty expected: bases kept
    assert night[0] > afternoon[0]                  # 2 clicks/h at night: 4.6 expected needs 2.5h
    assert night[1] >= night[0]

def test_lp_too_sparse_returns_none_and_crit_never_below_warn():
    now = pd.Timestamp('2026-11-26 10:00')
    assert lp_windows(None, now, 8, 2, 6) is None            # 8/day = 0.33/h: even 12h expects only 4.0 < 4.6
    # 20/day = 0.83/h: warn needs 5.5h but 5.5h only expects 4.58, so 6h for both
    assert lp_windows(None, now, 20, 2, 6) == (6.0, 6.0)
    warn, crit = lp_windows(None, now, 60, 4, 6)             # 2.5/h: warn 2h of base 4 stays 4, crit 6
    assert (warn, crit) == (4.0, 6.0)
