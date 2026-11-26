# -*- coding: utf-8 -*-
"""
Second, spec-driven rendering of the SAME typed Alert records (alert_types.py) - built so the
user can compare this wording against the current production format (alert_render.py) locally,
side by side, before deciding whether to adopt any of it. Implements the messaging spec handed
to this project on 2026-11-18 ("Optimise Daily and Hourly Health Alert Messaging") as closely as
the existing data model allows; see AUDIT_HISTORY.md findings #157-162 (the same-day audit) for
the gap analysis this module is the draft implementation of.

Deliberately NOT a replacement: alert_render.py/alert_telegram.py are completely untouched in
their default behavior and remain the only thing the scheduled daily job sends by default. This
module is wired up ONLY behind an explicit `--v2` flag in run_for_date.py (and
daily_health_check.py's own main(), for parity) - see those files' own `--v2` handling. Nothing
here is called unless that flag is passed.

Deliberately REUSES alert_render.py's grouping/ordering (build_sections(), the owner/entity sort
keys, the partner/brand/section-header functions) and its plain number-formatting helpers
(format_value()/format_change_phrase()/format_share()/format_pct_signed()) rather than
duplicating them - finding #157-161's audit found those pieces ALREADY spec-compliant (grouping
order, the %/pp distinction, always-show-baseline). Only the per-alert TITLE (finding #158), the
natural-sentence DESCRIPTION (finding #157's own "Instead of:" match), the STATUS vocabulary/
placement (finding #157/#162), and the supporting-note WORDING/italics (finding #159/#161) are
new - the actual subject of this rewrite. build_sections()/render() in alert_render.py both take
optional `render_block`/`render_header` keyword arguments specifically so this module can plug
in its own block/header renderers without alert_render.py duplicating a single line of grouping
logic - their default argument values are alert_render.py's own v1 functions, so every existing
caller that doesn't pass these keywords is 100% unaffected.

Title wording (TITLE_BY_PREFIX below) is an explicitly-flagged DRAFT taxonomy (finding #158) -
a deterministic, mechanical mapping from rule_id prefix + direction to a short phrase, not a
business-reviewed voice. It exists so there's something concrete to read and compare, not a
claim that this is the final wording.
"""
from typing import List, Optional

import alert_render as v1
from alert_types import (
    Alert, AlertType, BaselineType, Confidence, Lifecycle, Unit,
    AttributionNote, CustomerConcentrationNote, SharedAssetNote, EscalationNote,
    HistoryNote, TopItemsNote, DeclaredPairNote, LagNote,
)

EM_DASH = v1.EM_DASH


def _italic(text) -> str:
    """<i>-wraps a secondary-context line in HTML mode - spec §5's "Italics for secondary
    context" rule, which the 2026-11-18 audit found missing entirely from v1 (finding #159).
    Reads the SAME shared _HTML_MODE flag alert_render.html_mode() flips, so `with
    v1.html_mode(): ...` toggles this module's HTML-ness too, exactly like v1's own
    _bold()/_code()/_esc() - no second flag to keep in sync."""
    return f"<i>{text}</i>" if v1._HTML_MODE else str(text)


# ================================================================= TITLES (finding #158)
# Deterministic rule_id-prefix -> (rise title, drop title) lookup. The spec's own examples mix a
# generic spike/declining pattern with per-metric overrides - "WARNING | Clicks spike",
# "WARNING | Registrations declining", but "CRITICAL | RTP above expected level" (not "RTP
# spike"), since RTP reads naturally against a target band, not a plain magnitude move. Prefixes
# not listed fall back to a generic "{metric} spike"/"{metric} declining" pair.
_TITLE_BY_PREFIX = {
    'CLK': ("Clicks spike", "Clicks declining"),
    'REG': ("Registrations spike", "Registrations declining"),
    'FTD': ("FTDs spike", "FTDs declining"),
    'DEP': ("Depositors spike", "Depositors declining"),
    'C2R': ("Click-to-signup rate spike", "Click-to-signup rate declining"),
    'R2F': ("Signup-to-deposit rate spike", "Signup-to-deposit rate declining"),
    'GGR': ("GGR spike", "GGR declining"),
    'NGR': ("NGR spike", "NGR declining"),
    'NGG': ("NGR/GGR ratio spike", "NGR/GGR ratio declining"),
    'RTP': ("RTP above expected level", "RTP below expected level"),
    'ADM': ("Admin fees spike", "Admin fees declining"),
    'BON': ("Bonuses spike", "Bonuses declining"),
    'WDR': ("Withdrawals spike", "Withdrawals declining"),
    'DPS': ("Deposits $ spike", "Deposits $ declining"),
    'WAG': ("Bets $ spike", "Bets $ declining"),
    'STD': ("Returning-player rate above normal", "Returning-player rate declining"),
    'AFD': ("Avg first deposit spike", "Avg first deposit declining"),
    'BAB': ("Bonus abuse ratio above threshold", "Bonus abuse ratio declining"),
    'FGR': ("FTD GGR spike", "FTD GGR declining"),
    'FDP': ("First deposit $ spike", "First deposit $ declining"),
    'FBT': ("Bets engagement spike", "Bets engagement declining"),
    'FBN': ("Bonus engagement spike", "Bonus engagement declining"),
    'GEO': ("Foreign-geo share spike", "Foreign-geo share declining"),
    'ENG': ("Engagement ratio spike", "Engagement ratio declining"),
    'RET': ("Retention rate spike", "Retention rate declining"),
    'ROA': ("ROAS spike", "ROAS declining"),
}


def _direction(value: float, baseline: Optional[float], unit: Unit) -> str:
    """'rise' or 'drop' - reuses v1's own format_change_phrase() (its sign/percent-vs-point
    logic was already found spec-compliant - finding #157's §6 checklist) rather than
    re-deriving it, by reading the one word its phrase always ends in."""
    if baseline is None:
        return "rise" if value > 0 else "drop"
    phrase = v1.format_change_phrase(value, baseline, unit)
    return "drop" if phrase.endswith("drop") else "rise"


def _deviation_title(metric_key: str, metric_label: str, value, baseline, unit) -> str:
    prefix = (metric_key or '').partition('-')[0]
    rise_title, drop_title = _TITLE_BY_PREFIX.get(
        prefix, (f"{metric_label} spike", f"{metric_label} declining"))
    return rise_title if _direction(value, baseline, unit) == "rise" else drop_title


def _streak_title(metric_label: str) -> str:
    return f"No {v1._metric_prose(metric_label)}"


def _breadth_title(metric_label: str) -> str:
    return f"{metric_label} dropped across tracking links"


def _incident_title(lines) -> str:
    return f"{v1._incident_header_label(lines)} incident"


# ================================================================= STATUS (finding #157/#162)
def _status_text(a: Alert) -> str:
    """NEW / ONGOING · Day X / ESCALATED - the spec's own vocabulary (§3), always stated
    explicitly (unlike v1's folded trailing clause, which renders NOTHING for a NEW alert -
    finding #157/#162). "ONGOING" replaces v1's "RECURRING" - the spec names a different word
    for the same concept than the one v1 already settled on from an earlier spec; see finding
    #162 for that conflict."""
    if a.lifecycle == Lifecycle.ESCALATED:
        return "ESCALATED"
    if a.lifecycle == Lifecycle.RECURRING:
        return f"ONGOING · Day {a.lifecycle_day}" if a.lifecycle_day is not None else "ONGOING"
    return "NEW"


# ================================================================= DESCRIPTIONS (finding #157)
def _is_zero(value: float) -> bool:
    return abs(value) < 1e-9


def _change_clause(value: float, baseline: float, unit: Unit) -> str:
    """'dropped 58%' / 'rose 275%' / 'dropped 28.1 points' / 'rose sharply (12.0x higher)' - the
    verb form of v1's own format_change_phrase(), reusing its number rather than re-deriving a
    %/point/multiplier rule the audit already found correct."""
    phrase = v1.format_change_phrase(value, baseline, unit)
    if phrase.endswith(" higher"):
        return f"rose sharply ({phrase})"
    magnitude, word = phrase.rsplit(" ", 1)
    verb = "dropped" if word == "drop" else "rose"
    if magnitude.endswith("-point"):
        magnitude = f"{magnitude[:-6]} points"
    return f"{verb} {magnitude}"


def render_deviation_sentence(metric_label: str, unit: Unit, dim, value: float,
                               baseline: Optional[float], baseline_type: BaselineType) -> str:
    """One natural sentence - spec §3's own "Write:" style, e.g. "Clicks dropped 92% to 1,825
    today, compared with a 7-day average of 24,245." Special rules (spec §4): no-baseline and
    zero-value both get an explicit, literal statement rather than folding into the percentage."""
    dim_clause = v1._dim_phrase(dim)
    subject = f"{metric_label} {dim_clause}" if dim_clause else metric_label
    value_text = v1.format_value(value, unit)
    if baseline_type == BaselineType.NONE or baseline is None:
        return f"{subject} is {value_text} today; no comparable baseline is available."
    baseline_text = v1.format_value(baseline, unit)
    baseline_prose = v1._BASELINE_PROSE[baseline_type].format(baseline_text)
    if _is_zero(value):
        return f"{subject} dropped to zero today, compared with {baseline_prose}."
    change = _change_clause(value, baseline, unit)
    return f"{subject} {change} to {value_text} today, compared with {baseline_prose}."


def render_streak_sentence(metric_label: str, streak_days: int, clicks_per_day: float) -> str:
    plural = "day" if streak_days == 1 else "days"
    metric_prose = v1._metric_prose(metric_label)
    return (f"No {metric_prose} for {streak_days} consecutive {plural}, despite an average of "
            f"{v1.format_value(clicks_per_day, Unit.COUNT)} daily clicks.")


def render_breadth_sentence(metric_label: str, affected: int, total: int) -> str:
    pct = round(affected / total * 100) if total else 0
    return f"{metric_label} dropped across {affected} of {total} qualifying tracking links ({pct}%)."


def render_incident_sentences(lines) -> List[str]:
    return [f"    {render_deviation_sentence(ln.metric.label, ln.metric.unit, None, ln.deviation.value, ln.deviation.baseline, ln.deviation.baseline_type)}"
            for ln in lines]


# ================================================================= SUPPORTING-INFO NOTES (finding #159/#161)
# Same underlying content as v1's render_*_note() functions (nothing re-derived, no information
# dropped), relabeled to the spec's own named labels ("Most affected:"/"Main contributors:"/
# "Main driver:"/"Related alerts:" - finding #161) and italicized (finding #159).

def render_attribution_note_v2(note: AttributionNote, metric_label: str) -> str:
    names = ', '.join(f"{v1._partner_tag(p)} ({v1._partner_name(p)}{v1._segment_suffix(p)})" for p in note.partners)
    label = "Main driver:" if len(note.partners) == 1 else "Main contributors:"
    if note.confidence == Confidence.POSSIBLE:
        share_text = v1.format_share(note.share) or "most"
        return _italic(f"    {label} one player under {names} ({share_text} of today's volume, not confirmed).")
    tail = v1._share_phrase(note.share, metric_label)
    hedge = "" if note.confidence == Confidence.CONFIRMED else ", likely"
    return _italic(f"    {label} {names} - {tail}{hedge}.")


def render_customer_concentration_note_v2(note: CustomerConcentrationNote) -> str:
    frags = [v1._share_phrase(share, metric.label) for metric, share in note.shares_by_metric]
    p = note.partner
    return _italic(
        f"    Main driver: 1 customer under {v1._partner_tag(p)} ({v1._partner_name(p)}{v1._segment_suffix(p)}) "
        f"- {v1._list_prose(frags)}.")


def render_shared_asset_note_v2(note: SharedAssetNote) -> str:
    pct = round(note.own_share * 100)
    return _italic(f"    Shared with {note.partner_count} partners - this one drove {pct}% of its "
                   f"clicks, so the attribution isn't certain.")


def render_escalation_note_v2(note: EscalationNote) -> str:
    return _italic(f"    Escalated because {v1._ESCALATION_REASON_TEXT[note.reason]}.")


def render_history_note_v2(note: HistoryNote) -> str:
    plain = v1._metric_prose(note.metric_label)
    if note.lookback_days is None:
        return _italic(f"    No {plain} in the current lookback window.")
    return _italic(f"    No {plain} in the last {note.lookback_days} days.")


def render_top_items_note_v2(note: TopItemsNote) -> str:
    items_text = ', '.join(f"{v1._code(it.id)} ({v1.format_pct_signed(it.change)})" for it in note.items)
    more = f", and {note.more} more" if note.more else ""
    return _italic(f"    Most affected: {items_text}{more}.")


def render_declared_pair_note_v2(note: DeclaredPairNote) -> str:
    return _italic(f"    Related alerts: also linked to {v1._list_prose(note.linked_labels)}, same partner, same day.")


def render_lag_note_v2(note: LagNote) -> str:
    trigger = v1._metric_prose(note.trigger_label)
    if note.shared_partner is None:
        return _italic(f"    Related alerts: possibly follows yesterday's {trigger} move.")
    p = note.shared_partner
    return _italic(
        f"    Related alerts: follows yesterday's {trigger} move - same driver, "
        f"{v1._partner_tag(p)} ({v1._partner_name(p)}{v1._segment_suffix(p)}).")


def render_note_v2(note, metric_label: Optional[str] = None) -> str:
    if isinstance(note, AttributionNote):
        return render_attribution_note_v2(note, metric_label)
    if isinstance(note, CustomerConcentrationNote):
        return render_customer_concentration_note_v2(note)
    if isinstance(note, SharedAssetNote):
        return render_shared_asset_note_v2(note)
    if isinstance(note, EscalationNote):
        return render_escalation_note_v2(note)
    if isinstance(note, HistoryNote):
        return render_history_note_v2(note)
    if isinstance(note, TopItemsNote):
        return render_top_items_note_v2(note)
    if isinstance(note, DeclaredPairNote):
        return render_declared_pair_note_v2(note)
    if isinstance(note, LagNote):
        return render_lag_note_v2(note)
    raise ValueError(f"unknown note type {type(note)!r}")


# ================================================================= ALERT BLOCK (title line + description + notes)
def render_alert_header_line(a: Alert) -> str:
    """'[SEVERITY] | [Title] — [STATUS]' - spec §2's own structure line. Only the TITLE is
    bolded (spec §5 says bold applies to "alert titles", not the whole line) - severity stays
    plain uppercase text, matching the spec's own "CRITICAL | No FTDs" example literally."""
    if a.type == AlertType.INCIDENT:
        title = _incident_title(a.lines)
    elif a.type == AlertType.DEVIATION:
        title = _deviation_title(a.metric.key, a.metric.label, a.deviation.value, a.deviation.baseline, a.metric.unit)
    elif a.type == AlertType.STREAK:
        title = _streak_title(a.metric.label)
    elif a.type == AlertType.BREADTH:
        title = _breadth_title(a.metric.label)
    else:
        raise ValueError(f"unknown alert type {a.type!r}")
    status = _status_text(a)
    return f"{a.severity.value} | {v1._bold(title)} — {status}"


def render_alert_block_v2(a: Alert) -> List[str]:
    header = render_alert_header_line(a)
    if a.type == AlertType.INCIDENT:
        lines = [header] + render_incident_sentences(a.lines)
    elif a.type == AlertType.DEVIATION:
        lines = [header, render_deviation_sentence(
            a.metric.label, a.metric.unit, a.dimension,
            a.deviation.value, a.deviation.baseline, a.deviation.baseline_type)]
    elif a.type == AlertType.STREAK:
        lines = [header, render_streak_sentence(a.metric.label, a.streak.streak_days, a.streak.clicks_per_day)]
    elif a.type == AlertType.BREADTH:
        lines = [header, render_breadth_sentence(a.metric.label, a.breadth.affected, a.breadth.total)]
    else:
        raise ValueError(f"unknown alert type {a.type!r}")
    metric_label = a.metric.label if a.metric else None
    for note in sorted(a.notes, key=v1._note_sort_key):
        lines.append(render_note_v2(note, metric_label))
    return lines


# ================================================================= MESSAGE HEADER
def render_message_header_v2(date: str, alerts) -> str:
    # Spec §2 writes "Daily Health Check — [DATE]" (Title Case) - v1 deliberately uses "Daily
    # health check" (see alert_render.py's own render_message_header(), 2026-11-13 user choice).
    return v1._bold(f"Daily Health Check {EM_DASH} {date}")


# ================================================================= TOP-LEVEL RENDER
def render_v2(alerts: List[Alert], date: str) -> List[str]:
    """Drop-in v2 counterpart of alert_render.render() - same pure list[Alert] -> list[str]
    contract. Just v1's render() called with this module's own block/header renderers, so the
    two local-test formats can never drift on grouping/ordering, only on per-alert wording."""
    return v1.render(alerts, date, render_block=render_alert_block_v2, render_header=render_message_header_v2)
