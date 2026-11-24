# -*- coding: utf-8 -*-
"""
Maps the EXISTING engine/attribution output (the same `all_alerts` DataFrame
build_telegram_message() in daily_health_check.py already assembles - one row per actionable
rule_id/entity, post attribute_alerts()/apply_suppression()/daily_notifications(), with
project_id/project_label columns added by the caller) into the typed Alert records
alert_render.py consumes.

Detection (health_check_engine.py) and attribution/suppression (daily_health_check.py's
attribute_alerts()/apply_suppression(), called by the caller BEFORE this module) are untouched -
this module only re-shapes their already-computed output. See NOT_PROVIDED at the bottom of
this file for the deliverable-5 note: fields the current data can't confidently fill, and this
module's explicit choice not to guess them.
"""
import re
from typing import Dict, List, Optional

import pandas as pd

from alert_types import (
    Alert, AlertType, BaselineType, Confidence, Dimension, DimensionType,
    EscalationReason, HistoryFlag, IncidentLine, Lifecycle, Metric, PartnerRef,
    Scope, Severity, StreakData, BreadthData, BreadthItem, DeviationData, Unit,
    AttributionNote, CustomerConcentrationNote, SharedAssetNote, HistoryNote,
    TopItemsNote, DeclaredPairNote, FormulaPairNote, LagNote, PLATFORM, UNASSIGNED,
)

# ================================================================= per-rule_id dispatch tables
# Same prefix-dispatch pattern daily_health_check.py's _metric_label()/_baseline_label()/
# _is_percentage_metric() already use - ported here rather than re-derived, since detection's
# own per-rule shapes (what a value/baseline/pct column actually means for rule X) are the one
# piece of domain knowledge this module must get right to not misrender a real number.

_UNIT_BY_PREFIX = {
    'CLK': Unit.COUNT, 'REG': Unit.COUNT, 'FTD': Unit.COUNT, 'DEP': Unit.COUNT,
    'GGR': Unit.MONEY, 'NGR': Unit.MONEY, 'FGR': Unit.MONEY, 'ADM': Unit.MONEY,
    'BON': Unit.MONEY, 'WDR': Unit.MONEY, 'DPS': Unit.MONEY, 'WAG': Unit.MONEY,
    'FDP': Unit.MONEY, 'AFD': Unit.MONEY,
    'RTP': Unit.RATE, 'STD': Unit.RATE, 'C2R': Unit.RATE, 'R2F': Unit.RATE,
    'FBN': Unit.RATE, 'FBT': Unit.RATE, 'GEO': Unit.RATE, 'BAB': Unit.RATE,
    'NGG': Unit.RATIO,
    # FIX (2026-11-13, user feedback: "4,005.8% is complicated, keep the absolute number and
    # the % of change"): ENG-01/02 is a turnover multiple (bets can be many times deposits), not
    # a 0-100ish percentage like the other RATE metrics - MULTIPLE displays it as "40.1x"
    # instead of "4,005.8%". See the matching /100 unscale where value/baseline are built below
    # (health_check_engine.py's ratio_sum_baseline() stores it *100, same as every RATE metric).
    'ENG': Unit.MULTIPLE,
    # RET-01/02 (finding #149, "retention is on depositors"): a plain 0-100% retention rate,
    # same shape as RTP/STD. ROA-01: same bare-multiple reasoning as ENG above - program profit
    # can be a multiple of investment either direction, not a 0-100ish percentage.
    'RET': Unit.RATE,
    'ROA': Unit.MULTIPLE,
}

_BASELINE_TYPE_OVERRIDE = {
    # mirrors _baseline_label()'s own exceptions list exactly (daily_health_check.py) - most
    # rules really do compare to a rolling 7-day average (AVG_7D, the dict's default elsewhere).
    'STD-01': BaselineType.PRIOR_WEEK,
    'STD-02': BaselineType.EXPECTED,  # "the market rate" - a peer comparison, not history; the
                                       # task's own BaselineType enum has no "peer" option, so
                                       # this is the closest fit, flagged in NOT_PROVIDED below.
    'NGG-01': BaselineType.PRIOR_WEEK,
    'DEP-02': BaselineType.PRIOR_WEEK,
    # finding #149: RET-01 compares to the prior completed WEEK, ROA-01 to the 7 days before -
    # both genuinely "last week's figure". RET-02 compares to the prior completed MONTH, which
    # needs its own wording (see BaselineType.PRIOR_MONTH) - "last week's figure" would be wrong.
    'RET-01': BaselineType.PRIOR_WEEK,
    'RET-02': BaselineType.PRIOR_MONTH,
    'ROA-01': BaselineType.PRIOR_WEEK,
}
_BASELINE_TYPE_PREFIX_OVERRIDE = {
    'RTP': BaselineType.EXPECTED, 'BAB': BaselineType.EXPECTED, 'GEO': BaselineType.EXPECTED,
}

# FIX (2026-11-13, user: "we send alerts as it is, but the real target, the normal, is
# 96-97%"): RTP-01/02/03's own trigger thresholds (90/93/99/100 in health_check_engine.py -
# explicitly marked "(placeholder)" in that code's own emit() note text) decide whether an
# alert fires, untouched here ("we send alerts as it is"). But using that same placeholder as
# the DISPLAYED comparison point is misleading - "87.3% vs. an expected 90.0%" reads as if 90%
# were the real business target, when it's only the alert floor. Only the number shown (and the
# "N% drop/rise" computed from it) changes, to the real target the user gave - the alert logic
# itself is untouched.
_RTP_NORMAL_TARGET = 96.5  # midpoint of the stated 96-97% normal range

# FIX (caught cross-checking real 2026-11-11 data): CLK-18/CLK-19/FTD-18 (LP-level) also CHECK
# breadth as their trigger condition, but - confirmed directly in daily_health_check.py's own
# comment on finding #68 - "the LP-level breadth rules (CLK-18/FTD-18)... pass the partner's/
# LP's own real metric value as value/baseline, unaffected" (unlike CLK-13/REG-13/FTD-13 at
# Partner level, where "FIX 9" deliberately repurposes value/baseline into COUNTS OF TRACKING
# LINKS). A real alert (partner_11570, landing page 406) confirmed this live: classifying
# CLK-18 as BREADTH rendered "Clicks: 211/502 tracking links down" - nonsense, since 211/502
# are real click counts, not a 211-of-502-links breadth count. Only the three Partner-level
# rule_ids belong here.
_BREADTH_RULE_IDS = {'CLK-13', 'REG-13', 'FTD-13'}
_STREAK_RULE_IDS = {'R2F-15', 'C2R-15'}


def _unit_for(rule_id: str) -> Unit:
    prefix = rule_id.partition('-')[0]
    if prefix not in _UNIT_BY_PREFIX:
        raise ValueError(f"alert_mapping: no Unit mapped for rule_id prefix {prefix!r} "
                          f"(rule_id={rule_id!r}) - add it to _UNIT_BY_PREFIX rather than "
                          f"guessing; see this module's NOT_PROVIDED note.")
    return _UNIT_BY_PREFIX[prefix]


def _baseline_type_for(rule_id: str, baseline: Optional[float], pct=None) -> BaselineType:
    # FIX (caught cross-checking real 2026-11-11 data): FGR-01's baseline can be a real,
    # non-null number that is still not a meaningful comparison point (its own trigger is
    # "trailing 7-day sum <= 0" - the baseline can be exactly 0.0, a valid float, not None/NaN).
    # The engine's own pctchg() (health_check_engine.py) is the authoritative signal for "this
    # comparison is undefined" - it replaces a zero baseline with NaN before dividing, so `pct`
    # comes back NaN in exactly this case (the same signal daily_health_check.py's own
    # _alert_phrase() checks via `pd_isna(pct)`) - checking `baseline is None` alone missed it
    # and rendered "(-115 vs 0 7d avg)" instead of "(no baseline)".
    if baseline is None or (isinstance(baseline, float) and pd.isna(baseline)):
        return BaselineType.NONE
    if pct is None or (isinstance(pct, float) and pd.isna(pct)):
        return BaselineType.NONE
    if rule_id in _BASELINE_TYPE_OVERRIDE:
        return _BASELINE_TYPE_OVERRIDE[rule_id]
    prefix = rule_id.partition('-')[0]
    return _BASELINE_TYPE_PREFIX_OVERRIDE.get(prefix, BaselineType.AVG_7D)


def _metric_for(row) -> Metric:
    from daily_health_check import _metric_label  # reuse the existing, already-audited labels
    raw_metric = row['metric']
    if row['level'] == 'Project/Traffic':
        # FIX (caught cross-checking real data): the engine embeds the traffic-type segment
        # directly into the metric string at this level (e.g. STD-01's "FTD-to-STD (Push/Pop)",
        # CLK-04..11's "Clicks (Push/Pop)") - confirmed live, this rendered as "Returning-player
        # rate (Push/Pop) · Push/Pop" because _dimension_for() ALSO attaches a SEGMENT dimension
        # from the same traffic_type fact. Strip the embedded suffix back off here so the
        # dimension is the only place it appears, matching the fixture's clean separation.
        raw_metric = re.sub(r'\s*\([^()]*\)\s*$', '', raw_metric)
    label = _metric_label(raw_metric)
    return Metric(key=row['rule_id'], label=label, unit=_unit_for(row['rule_id']))


def _dimension_for(row) -> Dimension:
    level = row['level']
    entity = row['entity']
    num = entity.split('_', 1)[1] if '_' in str(entity) else entity
    if level == 'GUID':
        return Dimension(DimensionType.TRACKING_LINK, str(num), row.get('traffic_type') or None)
    if level == 'Landing Page':
        return Dimension(DimensionType.LANDING_PAGE, str(num))
    if level == 'Project/Traffic':
        return Dimension(DimensionType.SEGMENT, None, str(entity))
    return Dimension(DimensionType.NONE)


def _lifecycle_for(row):
    """Returns (Lifecycle, lifecycle_day)."""
    et = row.get('event_type')
    if et == 'ESCALATION':
        return Lifecycle.ESCALATED, None
    if et == 'REMINDER':
        days = row.get('open_days')
        day = int(days) if days is not None and not pd.isna(days) else None
        return Lifecycle.RECURRING, day
    return Lifecycle.NEW, None


def _severity_for(row) -> Severity:
    return Severity.CRITICAL if row['severity'] == 'CRITICAL' else Severity.WARNING


def _partner_ref(pid, partner_names: Dict[int, str]) -> Optional[PartnerRef]:
    if pid is None or (isinstance(pid, float) and pd.isna(pid)):
        return None
    pid = int(pid)
    raw = partner_names.get(pid, f"partner_{pid}")
    # Segment is what's currently in parentheses: "gentoo (Push/Pop LatAm)" -> name "gentoo",
    # segment "Push/Pop LatAm" - per the task's own §1 instruction, verbatim.
    m = re.match(r'^(.*\S)\s*\(([^()]+)\)\s*$', raw)
    if m:
        return PartnerRef(id=pid, name=m.group(1), segment=m.group(2))
    return PartnerRef(id=pid, name=raw)


_HIT_GUIDS_RE = re.compile(r'guid_(\S+) (-?\d+)%')
_MORE_RE = re.compile(r'\+(\d+) more')


def _top_items_from_note(note_text: str) -> Optional[TopItemsNote]:
    """Parses the engine's free-text "hit GUIDs: guid_X -Y%, guid_Z -W%, +N more" note into
    structured items. FRAGILE BY CONSTRUCTION - the engine (health_check_engine.py's
    run_partner_guid_lp_volume(), "FIX 9") only ever exposes this as text, not as a column of
    (guid, pct) pairs; see NOT_PROVIDED below. Returns None if the pattern isn't found."""
    if not note_text or 'hit GUIDs:' not in str(note_text):
        return None
    tail = str(note_text).split('hit GUIDs:', 1)[1]
    items = tuple(BreadthItem(f"guid_{gid}", float(pct)) for gid, pct in _HIT_GUIDS_RE.findall(tail))
    if not items:
        return None
    m = _MORE_RE.search(tail)
    more = int(m.group(1)) if m else 0
    return TopItemsNote(items=items, more=more)


def _history_note_for_streak(row, metric_label: str) -> HistoryNote:
    days = row.get('open_days')
    # CRITICAL severity for R2F-15/C2R-15 means "never converted in the full lookback" (the
    # engine's own severity_for_run()/TIERS ceiling for these two rule_ids) - WARNING means it's
    # still within the open lookback window, not yet confirmed as a full-window absence. See
    # run_zero_conversion()'s own two-branch note text in daily_health_check.py's _alert_phrase()
    # for the exact same distinction, ported here as the HistoryFlag/lookback_days choice.
    if row['severity'] == 'CRITICAL':
        from daily_health_check import BUFFER_DAYS
        return HistoryNote(metric_label=metric_label, lookback_days=BUFFER_DAYS)
    return HistoryNote(metric_label=metric_label, lookback_days=None)


def _notes_for_row(row, partner_names: Dict[int, str]) -> List:
    notes = []

    attribution = str(row.get('attribution', ''))
    if attribution.startswith('lp-dominant'):
        # FIX (2026-11-12): attribute_alerts()'s lp-dominant share changed meaning - it's now
        # each partner's share of the LP's own click MOVE on the alert date (who actually
        # moved), not their share of the LP's total click volume (who's normally biggest) -
        # see that function's own docstring. The regex/threshold here are unchanged: still the
        # top partner's share and the partner count, still hedged below 60%.
        m = re.search(r'(\d+)% of (\d+) partners', attribution)
        if m:
            share_pct, n = int(m.group(1)), int(m.group(2))
            if share_pct < 60:
                notes.append(SharedAssetNote(partner_count=n, own_share=share_pct / 100))

    solo_p, solo_s = row.get('solo_dominant_partner'), row.get('solo_dominant_share')
    if solo_p is not None and not pd.isna(solo_p):
        notes.append(AttributionNote(partners=(_partner_ref(solo_p, partner_names),),
                                      share=abs(float(solo_s)), confidence=Confidence.CONFIRMED))

    top3 = row.get('top3_dominant_partners')
    if top3 and not pd.isna(top3):
        pids = [int(p) for p in re.findall(r'partner_(\d+)', str(top3))]
        share = row.get('top3_dominant_share')
        notes.append(AttributionNote(
            partners=tuple(_partner_ref(p, partner_names) for p in pids),
            share=abs(float(share)) if share is not None and not pd.isna(share) else 1.0,
            confidence=Confidence.CONFIRMED))

    dom_p, dom_s = row.get('dominant_partner'), row.get('dominant_partner_share')
    if dom_p is not None and not pd.isna(dom_p):
        notes.append(AttributionNote(partners=(_partner_ref(dom_p, partner_names),),
                                      share=abs(float(dom_s)), confidence=Confidence.LIKELY))

    hint_p, hint_s = row.get('ratio_hint_partner'), row.get('ratio_hint_share')
    if hint_p is not None and not pd.isna(hint_p):
        # FIX (2026-11-13, user: "you can check how the whole ratio moved, not just numerator"):
        # apply_correlation()'s annotate_ratio_numerator_hint() now also checks the DENOMINATOR
        # side (ratio_hint_confirmed) - when removing this customer from both sides explains
        # >=70% of the day's move, this is real two-sided evidence, not just a numerator-
        # concentration guess, so it's rendered as CONFIRMED rather than always POSSIBLE.
        confirmed = bool(row.get('ratio_hint_confirmed', False))
        notes.append(AttributionNote(
            partners=(_partner_ref(hint_p, partner_names),), share=abs(float(hint_s)),
            confidence=Confidence.CONFIRMED if confirmed else Confidence.POSSIBLE))

    top_items = _top_items_from_note(row.get('note'))
    if top_items:
        notes.append(top_items)

    # FIX (2026-11-13, user-directed format revision, option B - "complete as the previous
    # one"): entity_linked_rule_ids (§c, declared static pairs) and prior_day_* (§e, one-day
    # lag) have been computed by apply_correlation() since the project started but were never
    # read anywhere in this typed-record renderer (AUDIT_HISTORY.md finding #144's own gap,
    # confirmed live against partner_8074's real CLK-12/R2F-15 pair on 2026-11-11). Local import,
    # same reason BUFFER_DAYS is imported this way above - daily_health_check imports this
    # module at its own top level, so a module-level import here would be circular.
    from daily_health_check import _rule_id_label

    linked_rules = row.get('entity_linked_rule_ids')
    if linked_rules and not pd.isna(linked_rules):
        rids = [r.strip() for r in str(linked_rules).split(',') if r.strip()]
        labels = tuple(dict.fromkeys(_rule_id_label(r) for r in rids))
        if labels:
            notes.append(DeclaredPairNote(linked_labels=labels))

    formula_rules = row.get('formula_linked_rule_ids')
    if formula_rules and not pd.isna(formula_rules):
        f_rids = [r.strip() for r in str(formula_rules).split(',') if r.strip()]
        f_labels = tuple(dict.fromkeys(_rule_id_label(r) for r in f_rids))
        if f_labels:
            notes.append(FormulaPairNote(
                metric_label=_rule_id_label(str(row.get('rule_id'))),
                linked_labels=f_labels))

    prior_rule = row.get('prior_day_rule_ids')
    if prior_rule and not pd.isna(prior_rule):
        prior_metric = row.get('prior_day_metric')
        trigger_label = (str(prior_metric) if prior_metric and not pd.isna(prior_metric)
                          else _rule_id_label(str(prior_rule)))
        shared_pid = row.get('prior_day_shared_partner')
        shared_ref = (_partner_ref(int(shared_pid), partner_names)
                      if shared_pid is not None and not pd.isna(shared_pid) else None)
        notes.append(LagNote(trigger_label=trigger_label, shared_partner=shared_ref))

    return notes


def _owner_for(row, am_map: Dict[int, str]) -> str:
    pid = row.get('partner_id')
    if pid is None or pd.isna(pid):
        return PLATFORM
    return am_map.get(int(pid), UNASSIGNED)


def _brands_for(row) -> tuple:
    return (row['project_label'],)


def _geo_mix_for(row, p4_breakdown_dict: Optional[dict]) -> Optional[dict]:
    return p4_breakdown_dict if row.get('project_id') == 4 and p4_breakdown_dict else None


def _single_alert_from_row(row, am_map, partner_names, p4_breakdown_dict, date) -> Alert:
    rule_id = row['rule_id']
    severity = _severity_for(row)
    lifecycle, lifecycle_day = _lifecycle_for(row)
    owner = _owner_for(row, am_map)
    pid = row.get('partner_id')
    partner = _partner_ref(pid, partner_names) if (pid is not None and not pd.isna(pid)) else None
    scope = Scope.PARTNER if partner is not None else Scope.BRAND
    metric = _metric_for(row)
    notes = tuple(_notes_for_row(row, partner_names))

    if rule_id in _STREAK_RULE_IDS:
        days = row.get('open_days')
        flag = HistoryFlag.NEVER_IN_LOOKBACK if row['severity'] == 'CRITICAL' else HistoryFlag.NONE
        from daily_health_check import BUFFER_DAYS
        streak = StreakData(
            streak_days=int(days) if days is not None and not pd.isna(days) else 0,
            clicks_per_day=float(row['baseline']) if row['baseline'] is not None else 0.0,
            lookback_days=BUFFER_DAYS, history_flag=flag)
        notes = notes + (_history_note_for_streak(row, metric.label),)
        return Alert(date=date, owner=owner, scope=scope, brands=_brands_for(row),
                     partner=partner, severity=severity, lifecycle=lifecycle,
                     lifecycle_day=lifecycle_day, type=AlertType.STREAK, metric=metric,
                     streak=streak, notes=notes, geo_mix=_geo_mix_for(row, p4_breakdown_dict))

    if rule_id in _BREADTH_RULE_IDS:
        n_dropped, n_eligible = row['value'], row['baseline']
        breadth = BreadthData(affected=int(n_dropped) if n_dropped is not None else 0,
                               total=int(n_eligible) if n_eligible is not None else 0)
        return Alert(date=date, owner=owner, scope=scope, brands=_brands_for(row),
                     partner=partner, severity=severity, lifecycle=lifecycle,
                     lifecycle_day=lifecycle_day, type=AlertType.BREADTH, metric=metric,
                     breadth=breadth, notes=notes, geo_mix=_geo_mix_for(row, p4_breakdown_dict))

    deviation = _deviation_data_for(row, metric.unit)
    return Alert(date=date, owner=owner, scope=scope, brands=_brands_for(row), partner=partner,
                 dimension=_dimension_for(row), severity=severity, lifecycle=lifecycle,
                 lifecycle_day=lifecycle_day, type=AlertType.DEVIATION, metric=metric,
                 deviation=deviation, notes=notes, geo_mix=_geo_mix_for(row, p4_breakdown_dict))


def _deviation_data_for(row, unit: Unit) -> DeviationData:
    """Shared by _single_alert_from_row() and _incident_from_cluster() so the two RTP/MULTIPLE
    display overrides below never drift out of sync between a plain alert and an incident line
    for the same rule_id."""
    rule_id = row['rule_id']
    baseline_type = _baseline_type_for(rule_id, row['baseline'], row.get('pct'))
    value = float(row['value'])
    dev_baseline = float(row['baseline']) if baseline_type != BaselineType.NONE else None
    if rule_id.startswith('RTP-') and dev_baseline is not None:
        dev_baseline = _RTP_NORMAL_TARGET
    if unit == Unit.MULTIPLE:
        # health_check_engine.py's ratio_sum_baseline() stores every ratio *100 (the same
        # convention every RATE metric uses) - unscale back to a plain multiple (40.06, not
        # 4005.8) now that this metric displays as "40.1x" rather than a percentage.
        value /= 100
        if dev_baseline is not None:
            dev_baseline /= 100
    return DeviationData(value=value, baseline=dev_baseline, baseline_type=baseline_type)


def _incident_from_cluster(rows: List, am_map, partner_names, p4_breakdown_dict, date) -> Alert:
    """rows: the cluster's member DataFrame rows (same grouping key as daily_health_check.py's
    render_rows()/group_key() - project_id + linked_dominant_partner), highest severity first."""
    rows = sorted(rows, key=lambda r: 0 if r['severity'] == 'CRITICAL' else 1)
    first = rows[0]
    severity = _severity_for(first)
    lifecycle, lifecycle_day = _lifecycle_for(first)
    owner = PLATFORM  # every real cluster seen is Project-level (correlate_alerts() is Project-only)
    lines = []
    for r in rows:
        m = _metric_for(r)
        lines.append(IncidentLine(metric=m, deviation=_deviation_data_for(r, m.unit)))
    lines = tuple(lines)
    dom_p = first.get('linked_dominant_partner')
    shares = tuple((_metric_for(r), abs(float(r.get('linked_dominant_share') or 0))) for r in rows)
    notes = (CustomerConcentrationNote(partner=_partner_ref(dom_p, partner_names), shares_by_metric=shares),)
    return Alert(date=date, owner=owner, scope=Scope.BRAND, brands=_brands_for(first),
                 severity=severity, lifecycle=lifecycle, lifecycle_day=lifecycle_day,
                 type=AlertType.INCIDENT, lines=lines, notes=notes,
                 geo_mix=_geo_mix_for(first, p4_breakdown_dict))


# TODO (out of scope for this change, per the task's own instruction): "traffic shifted between
# links" merge rule. Confirmed live in the real 2026-11-11 data this module was validated
# against - partner_8074/ConvertoBR collapsed 92% in Brazino777.ar's own CLK-12 the same day its
# CLK-16 spiked 275% on a DIFFERENT tracking link in Brazino777.io. Today these map to two
# separate Alert objects (correctly, per current scope) even though they may be one real event
# (traffic moved between brands/links, not two independent problems). A future pass could detect
# this pattern (same partner, same day, one metric collapsing while a sibling link/brand spikes)
# and merge it into one INCIDENT-shaped Alert the way a `linked_rule_ids` cluster already is -
# deliberately NOT attempted here.
def map_alerts(df: pd.DataFrame, am_map: Dict[int, str], partner_names: Dict[int, str],
               p4_breakdown_dict: Optional[dict], date: str) -> List[Alert]:
    """df: the actionable, post-attribution/post-suppression rows for one eval_date, across all
    projects - exactly `all_alerts` as build_telegram_message() already assembles it, with
    `project_id`/`project_label` columns. Groups `linked_rule_ids` clusters into one INCIDENT
    each (same grouping key daily_health_check.py's render_rows() uses), everything else maps
    1:1 to a DEVIATION/STREAK/BREADTH Alert."""
    df = df.copy()

    def _cluster_key(row):
        linked = row.get('linked_rule_ids')
        if linked and not pd.isna(linked):
            return ('cluster', row.get('project_id'), row.get('linked_dominant_partner'))
        return ('single', row.name)

    df['_key'] = df.apply(_cluster_key, axis=1)
    alerts = []
    for key, grp in df.groupby('_key', sort=False):
        rows = [r for _, r in grp.iterrows()]
        if key[0] == 'cluster' and len(rows) > 1:
            alerts.append(_incident_from_cluster(rows, am_map, partner_names, p4_breakdown_dict, date))
        else:
            for row in rows:
                alerts.append(_single_alert_from_row(row, am_map, partner_names, p4_breakdown_dict, date))
    return alerts


def parse_p4_breakdown(breakdown_str: str) -> Optional[dict]:
    """fetch_p4_country_breakdown() (daily_health_check.py) returns a pre-formatted display
    string ("VE 22, BO 11, AR 2"), not a dict - parsed back here rather than changing that
    function's own return shape (it's also used, unchanged, by the old renderer during the
    transition). See NOT_PROVIDED: this round-trip (dict -> string -> dict) is a smell; the
    cleaner fix is for fetch_p4_country_breakdown() to return the dict and let ITS caller
    format the string, once the old renderer is fully retired."""
    if not breakdown_str:
        return None
    pairs = re.findall(r'([A-Z]{2})\s+(\d+)', breakdown_str)
    return {k: int(v) for k, v in pairs} if pairs else None


# ================================================================= deliverable 5
NOT_PROVIDED = """
Fields the current engine/attribution data cannot confidently fill - left unset (never guessed):

1. Alert.escalation_reason - always None. The current data (`event_type`, `escalated_from`,
   `open_days`) can tell us an alert's severity rank went UP since yesterday, but not WHICH of
   STREAK / NONPOSITIVE_N_DAYS / SEVERITY_UP caused it. Distinguishing these would need the
   engine to record, per escalation, which specific condition changed - it doesn't today.

2. TopItemsNote (the breadth rules' own hit-GUID list) is reconstructed by regex from the
   engine's free-text `note` column ("hit GUIDs: guid_X -Y%, ..."), not read from a real
   column of (guid, pct) pairs. Fragile by construction - a wording change in
   run_partner_guid_lp_volume()'s own note text (health_check_engine.py) would silently break
   this parse. The real fix is for that function to expose the hit-GUID list as structured
   data; out of scope here since detection logic stays untouched.

3. BaselineType for STD-02 ("the market rate" - a peer comparison against the project-wide
   rate, not a historical self-baseline) is mapped to EXPECTED, the closest of the task's 4
   enum values - not a perfect fit. A literal "PEER" baseline type isn't in the given enum.

4. geo_mix is only ever populated for Project 4 (Brazino777.io) - fetch_p4_country_breakdown()
   is the only source of country-level data anywhere in this pipeline, confirmed by grep. Every
   other brand's Alert.geo_mix is None, not an empty-but-real {}.

5. Dimension.label for a BREADTH alert's own items (TOP_ITEMS) carries no traffic_type/segment
   info - the engine's hit-GUID note (see #2) names only the guid and its own % change.
"""
