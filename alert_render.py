# -*- coding: utf-8 -*-
"""
Deterministic renderer: list[Alert] -> list[str] (plain-text Telegram messages, before the
HTML escaping/<code>-wrapping layer in alert_telegram.py). Pure function - no I/O, no randomness,
no wall-clock reads; the same input list always produces byte-identical output.

FIX (2026-11-13, user feedback - "i prefer sth closer to this vibe, but a bit more brief now it
is too robotic", shown a real pre-this-project message as the reference "vibe"): this is the
SECOND rewrite of this module's wording in one day. The first (same day, finding #146) replaced
emoji with bracket tags ("[CRIT] NEW\\nClicks: -58% (...)") - still terse, still read as robotic.
This pass moves to one natural-sentence-per-alert ("CRITICAL - Clicks: 58% drop (211 today vs. a
7-day average of 502) - just escalated"), closer to the user's own pasted reference but trimmed
down from it (no separate "Just escalated from X to Y, this is new, not a repeat" sentence - that
status is now a short trailing clause on the SAME line; no per-note paragraph - one short
sentence each). Shown as 3 options against real 2026-11-12 data before implementing (see
AUDIT_HISTORY.md finding #147) - user picked "status folded into the same line" (option A).

Every formatting rule below that ISN'T this rewrite's own new wording is still pinned by the
original engineering ticket's fixture - see tests/test_alert_render.py's golden-file test, now
updated to the new wording with the same 16 alerts/same numbers.
"""
import html as _html_mod
import os
from typing import List, Optional

# ================================================================= HTML-mode hooks
# A single-process-batch-job simplification, not a general design pattern: alert_telegram.py
# (§5) needs the SAME identifier-producing text ("partner_8074", "landing page 406", "tracking
# link 24382 (Push/Pop)", "guid_30653") wrapped in <code> and partner names/segments HTML-
# escaped, with everything else byte-identical to the plain-text renderer this module's own
# golden test pins. Threading an explicit `html: bool` through every render_*() call would touch
# ~15 function signatures for 2 bits of state; a module-level flag flipped only inside
# html_mode(), always reset even on an exception, is a smaller surface for a script that is
# never called concurrently (one daily batch run, no threads/async). The plain-text golden
# test (tests/test_alert_render.py) never touches this flag, so it's unaffected either way.
_HTML_MODE = False


class html_mode:
    """`with html_mode(): ...` - alert_telegram.py's own context manager for emitting the
    <code>-wrapped, HTML-escaped variant of the same render_*() calls."""
    def __enter__(self):
        global _HTML_MODE
        self._prev = _HTML_MODE
        _HTML_MODE = True
        return self

    def __exit__(self, *exc):
        global _HTML_MODE
        _HTML_MODE = self._prev
        return False


def _esc(text) -> str:
    """HTML-escapes free text (partner names, segments) - identity in plain-text mode."""
    if text is None:
        return text
    return _html_mod.escape(str(text), quote=False) if _HTML_MODE else str(text)


def _code(text) -> str:
    """Wraps an identifier (partner_N, LP/TL ids, guids) in <code> - identity in plain-text
    mode. §5: "Wrap partner_{id}, LP/TL ids and guids in <code>.\""""
    text = str(text)
    return f"<code>{text}</code>" if _HTML_MODE else text


def _bold(text) -> str:
    """Wraps a scan-point (severity word, metric name, partner name, section header) in <b> -
    identity in plain-text mode, same pattern as _code()/_esc()."""
    return f"<b>{text}</b>" if _HTML_MODE else str(text)


from alert_types import (
    Alert, AlertType, BaselineType, Confidence, DimensionType, EscalationReason,
    HistoryFlag, Lifecycle, Scope, Severity, Unit,
    AttributionNote, CustomerConcentrationNote, SharedAssetNote, EscalationNote,
    HistoryNote, TopItemsNote, DeclaredPairNote, LagNote, PLATFORM, UNASSIGNED,
)

MINUS = '−'
EM_DASH = '—'

# ================================================================= NUMBER FORMATTING

def _fmt_thousands(value: float, decimals: int) -> str:
    """Thousands separators, fixed decimals, UNSIGNED (caller prepends the sign/minus)."""
    return f"{abs(value):,.{decimals}f}"


def _money_decimals(value: float) -> int:
    return 0 if abs(value) >= 100 else 2


def _rate_decimals(value: float) -> int:
    """Rate/ratio decimal count, by magnitude - mirrors _money_decimals()'s own approach.
    A thin-traffic click-to-reg/signup-to-deposit ratio on a single GUID/TL can be well under
    1%, where a flat 1dp rounds a real, meaningful move down to indistinguishable zeros."""
    v = abs(value)
    if v == 0 or v >= 1:
        return 1
    return 2


def format_value(value: Optional[float], unit: Unit, *, decimals: Optional[int] = None) -> str:
    """One value/baseline number for the '({value} today vs. {baseline prose})' parenthetical -
    signed only when negative, thousands separators always. `decimals`, when given, overrides
    the unit's own magnitude rule - used so a DEVIATION's value and baseline share ONE decimal
    count decided by the value's own magnitude (e.g. GGR -413/3.01 renders the baseline as "3",
    not "3.01")."""
    if value is None:
        return ""
    if unit == Unit.COUNT:
        d = 0
    elif unit == Unit.MONEY:
        d = _money_decimals(value) if decimals is None else decimals
    elif unit in (Unit.RATE, Unit.RATIO):
        d = _rate_decimals(value) if decimals is None else decimals
    elif unit == Unit.MULTIPLE:
        d = 1 if decimals is None else decimals
    else:
        raise ValueError(f"unknown unit {unit!r}")
    sign = MINUS if value < 0 else ''
    out = f"{sign}{_fmt_thousands(value, d)}"
    if unit in (Unit.RATE, Unit.RATIO):
        out += '%'
    elif unit == Unit.MULTIPLE:
        out += 'x'
    return out


def format_change_phrase(value: float, baseline: float, unit: Unit, *,
                          decimals: Optional[int] = None) -> str:
    """'58% drop', '320% rise', '12.3x higher', '4.3-point drop' - the natural-language lead-in
    for a DEVIATION line, replacing the old '-58%'/'-28.1 pp' signed-number style.

    A RELATIVE percentage change ("N% drop/rise") is used whenever it's well-defined and can't
    mislead - for COUNT, whenever baseline > 0 (clicks/regs/FTDs are never negative, so there's
    no sign-flip risk); for MONEY, only when value and baseline are BOTH positive (GGR/NGR can
    swing between two negative numbers, where "a 56% drop" from -819 to -503 reads backwards -
    that's actually a 56% IMPROVEMENT); for RATE/RATIO, when baseline > 0 and value >= 0 (a rate
    can legitimately hit exactly 0 - "100% drop" from a real baseline to zero is meaningful - but
    a value that flips negative against a small positive baseline, e.g. NGR/GGR from +2.0% to
    -26.1%, would compute a nonsensical multi-thousand-percent "drop").

    Everywhere else, an absolute signed delta instead, in the unit's own terms - never a percent
    that would misrepresent an unbounded or sign-crossing quantity. >=1000% relative rise is
    "Nx higher" rather than a huge percentage, same reasoning as the old ×N multiplier."""
    if unit == Unit.MONEY:
        use_pct = value > 0 and baseline > 0
    elif unit in (Unit.RATE, Unit.RATIO):
        use_pct = baseline > 0 and value >= 0
    else:  # COUNT, MULTIPLE - neither can legitimately go negative
        use_pct = baseline > 0

    if use_pct:
        pct = (value - baseline) / baseline * 100
        if pct >= 1000:
            return f"{value / baseline:.1f}x higher"
        word = "rise" if pct >= 0 else "drop"
        return f"{round(abs(pct))}% {word}"

    delta = value - baseline
    word = "rise" if delta >= 0 else "drop"
    if unit == Unit.MONEY:
        d = _money_decimals(delta) if decimals is None else decimals
        return f"{_fmt_thousands(delta, d)} {word}"
    if unit in (Unit.RATE, Unit.RATIO):
        d = _rate_decimals(delta) if decimals is None else decimals
        return f"{_fmt_thousands(delta, d)}-point {word}"
    if unit == Unit.MULTIPLE:
        d = 1 if decimals is None else decimals
        return f"{_fmt_thousands(delta, d)}x {word}"
    return f"{_fmt_thousands(delta, 0)} {word}"


def format_pct_signed(x: float, decimals: int = 0) -> str:
    """A bare signed percentage, e.g. a BREADTH item's own 'change' - '-100%', '+12.3%'."""
    sign = '+' if x >= 0 else MINUS
    return f"{sign}{_fmt_thousands(x, decimals)}%"


def format_share(share: float, affected_total_nonpositive: bool = False) -> Optional[str]:
    """A plain (unsigned) whole-number share, e.g. '57%', '30%' - or `None` as a sentinel when
    the number itself would mislead (share > 100%, or the affected total <= 0): the caller then
    uses "most of the move" instead of a number."""
    if share > 1.0 or affected_total_nonpositive:
        return None
    return f"{round(share * 100):.0f}%"


def _is_acronym(label: str) -> bool:
    """FTD GGR / RTP / GGR / NGR/GGR / FTDs stay as-is mid-sentence; Clicks/Avg first deposit/
    Returning-player rate get their first letter lowered. Checking the first TWO letters (not
    .isupper() on the whole label) matters for "FTDs" specifically - a trailing lowercase plural
    's' would otherwise make a naive whole-string check say "not an acronym"."""
    letters = [ch for ch in label if ch.isalpha()]
    return len(letters) >= 2 and letters[0].isupper() and letters[1].isupper()


def _metric_prose(label: str) -> str:
    if _is_acronym(label) or not label:
        return label
    return label[0].lower() + label[1:]


def _list_prose(items) -> str:
    """'A', 'A and B', 'A, B and C' - an Oxford-comma-free natural list join."""
    items = list(items)
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])} and {items[-1]}"


_CONFIDENCE_PREFIX = {
    Confidence.CONFIRMED: "Driven by",
    Confidence.LIKELY: "Likely driven by",
    Confidence.POSSIBLE: "Possible hint, not confirmed:",
}


# ================================================================= BODY LINES

def _dim_phrase(dim) -> Optional[str]:
    """(connector, text) for a DEVIATION's dimension - 'on landing page 406', 'on tracking link
    24382 (Push/Pop)', or a parenthetical '(Push/Pop)' for a plain traffic-type segment."""
    if dim is None or dim.type == DimensionType.NONE:
        return None
    if dim.type == DimensionType.LANDING_PAGE:
        return f"on landing page {_code(dim.id)}"
    if dim.type == DimensionType.TRACKING_LINK:
        label = f" ({_esc(dim.label)})" if dim.label else ""
        return f"on tracking link {_code(dim.id)}{label}"
    if dim.type == DimensionType.SEGMENT:
        return f"({dim.label})"
    raise ValueError(f"unknown dimension type {dim.type!r}")


_BASELINE_PROSE = {
    BaselineType.AVG_7D: "a 7-day average of {}",
    BaselineType.PRIOR_WEEK: "last week's figure of {}",
    BaselineType.PRIOR_MONTH: "last month's figure of {}",
    BaselineType.EXPECTED: "an expected {}",
}


def render_deviation_line(metric_label: str, unit: Unit, dim, value: float,
                           baseline: Optional[float], baseline_type: BaselineType) -> str:
    dim_phrase = _dim_phrase(dim)
    head = f"{_bold(metric_label)} {dim_phrase}" if dim_phrase else _bold(metric_label)
    if baseline_type == BaselineType.NONE or baseline is None:
        return f"{head}: {format_value(value, unit)} (no comparable baseline)"
    # value/baseline/delta share one decimal count, decided by the VALUE's own magnitude.
    if unit == Unit.MONEY:
        decimals = _money_decimals(value)
    elif unit in (Unit.RATE, Unit.RATIO):
        decimals = _rate_decimals(value)
    else:
        decimals = None
    change = format_change_phrase(value, baseline, unit, decimals=decimals)
    value_text = format_value(value, unit, decimals=decimals)
    baseline_text = format_value(baseline, unit, decimals=decimals)
    baseline_prose = _BASELINE_PROSE[baseline_type].format(baseline_text)
    return f"{head}: {change} ({value_text} today vs. {baseline_prose})"


def render_streak_body(metric_label: str, streak_days: int, clicks_per_day: float) -> str:
    plural = "day" if streak_days == 1 else "days"
    return (f"{_bold(metric_label)}: 0 for {streak_days} straight {plural} despite real traffic "
            f"({format_value(clicks_per_day, Unit.COUNT)} clicks/day)")


def render_breadth_body(metric_label: str, affected: int, total: int) -> str:
    pct = round(affected / total * 100) if total else 0
    return (f"{_bold(metric_label)}: {affected} of {total} qualifying tracking links dropped "
            f"(share {pct}%)")


def render_incident_lines(lines) -> List[str]:
    return [f"    {render_deviation_line(ln.metric.label, ln.metric.unit, None, ln.deviation.value, ln.deviation.baseline, ln.deviation.baseline_type)}"
            for ln in lines]


def _incident_header_label(lines) -> str:
    labels = list(dict.fromkeys(ln.metric.label for ln in lines))  # dedupe, keep order
    return _list_prose(labels)


# ================================================================= NOTES

_ESCALATION_REASON_TEXT = {
    # NOT exercised by any real data today - alert_mapping.py never sets escalation_reason (the
    # current engine data can't reliably distinguish these 3 reasons) - this exists so the
    # renderer and its unit test are correct and ready for when/if the upstream data can.
    EscalationReason.STREAK: "the streak got longer",
    EscalationReason.NONPOSITIVE_N_DAYS: "it stayed non-positive for more days",
    EscalationReason.SEVERITY_UP: "severity increased",
}


def _note_sort_key(note) -> int:
    order = [EscalationNote, SharedAssetNote, HistoryNote, AttributionNote,
             CustomerConcentrationNote, DeclaredPairNote, LagNote, TopItemsNote]
    for i, cls in enumerate(order):
        if isinstance(note, cls):
            return i
    raise ValueError(f"unknown note type {type(note)!r}")


def _share_phrase(share: float, metric_label: str, affected_total_nonpositive: bool = False) -> str:
    """'57% of clicks' or, when the share can't be trusted as a plain number, 'most of the
    {metric} move' - the metric name stays in the sentinel case too so a multi-metric list
    (CustomerConcentrationNote) doesn't lose track of which metric 'most of the move' refers to."""
    text = format_share(share, affected_total_nonpositive)
    if text is None:
        return f"most of the {_metric_prose(metric_label)} move"
    return f"{text} of {_metric_prose(metric_label)}"


def render_attribution_note(note: AttributionNote, metric_label: str) -> str:
    """ATTRIBUTION carries no metric of its own - it's always the PARENT alert's own metric,
    implicit from context. Callers (render_note(), threaded from render_alert_block()) supply it.

    POSSIBLE (the ratio-hint mechanism, §3d) measures a customer's share of the day's NUMERATOR
    sum, not literally a share "of NGR/GGR" (a ratio has no well-defined per-customer share, per
    health_check_engine.py's own RATIO_NUMERATOR_HINT docstring) - worded as a share of "today's
    volume" instead, matching the real pasted reference's own phrasing, not _share_phrase()'s
    "of {metric}" pattern (which is accurate for CONFIRMED/LIKELY, where the share genuinely is
    of that metric's own move)."""
    names = ', '.join(f"{_partner_tag(p)} ({_partner_name(p)}{_segment_suffix(p)})" for p in note.partners)
    prefix = _CONFIDENCE_PREFIX[note.confidence]
    if note.confidence == Confidence.POSSIBLE:
        share_text = format_share(note.share) or "most"
        return f"    {prefix} one player under {names} is {share_text} of today's volume."
    tail = _share_phrase(note.share, metric_label)
    return f"    {prefix} {names} - {tail}."


def render_customer_concentration_note(note: CustomerConcentrationNote) -> str:
    # "1 customer" is fixed, not len(shares_by_metric) - this note type is always ONE dominant
    # customer explaining several metrics, regardless of how many are listed.
    frags = [_share_phrase(share, metric.label) for metric, share in note.shares_by_metric]
    p = note.partner
    return f"    Driven by 1 customer under {_partner_tag(p)} ({_partner_name(p)}{_segment_suffix(p)}) - {_list_prose(frags)}."


def render_shared_asset_note(note: SharedAssetNote) -> str:
    pct = round(note.own_share * 100)
    return (f"    Shared with {note.partner_count} partners - this one drove {pct}% of its "
            f"clicks, so the attribution isn't certain.")


def render_escalation_note(note: EscalationNote) -> str:
    return f"    Escalated because {_ESCALATION_REASON_TEXT[note.reason]}."


def render_history_note(note: HistoryNote) -> str:
    plain = _metric_prose(note.metric_label)
    if note.lookback_days is None:
        return f"    No {plain} in the current lookback window."
    return f"    No {plain} in the last {note.lookback_days} days."


def render_top_items_note(note: TopItemsNote) -> str:
    items_text = ', '.join(f"{_code(it.id)} ({format_pct_signed(it.change)})" for it in note.items)
    more = f", and {note.more} more" if note.more else ""
    return f"    Worst links: {items_text}{more}."


def render_declared_pair_note(note: DeclaredPairNote) -> str:
    return f"    Also linked to {_list_prose(note.linked_labels)}, same partner, same day."


def render_lag_note(note: LagNote) -> str:
    trigger = _metric_prose(note.trigger_label)
    if note.shared_partner is None:
        return f"    Possibly follows yesterday's {trigger} move."
    p = note.shared_partner
    return f"    Follows yesterday's {trigger} move - same driver, {_partner_tag(p)} ({_partner_name(p)}{_segment_suffix(p)})."


def render_note(note, metric_label: Optional[str] = None) -> str:
    if isinstance(note, AttributionNote):
        return render_attribution_note(note, metric_label)
    if isinstance(note, CustomerConcentrationNote):
        return render_customer_concentration_note(note)
    if isinstance(note, SharedAssetNote):
        return render_shared_asset_note(note)
    if isinstance(note, EscalationNote):
        return render_escalation_note(note)
    if isinstance(note, HistoryNote):
        return render_history_note(note)
    if isinstance(note, TopItemsNote):
        return render_top_items_note(note)
    if isinstance(note, DeclaredPairNote):
        return render_declared_pair_note(note)
    if isinstance(note, LagNote):
        return render_lag_note(note)
    raise ValueError(f"unknown note type {type(note)!r}")


def _partner_tag(p) -> str:
    return _code(f"partner_{p.id}")


def _segment_suffix(p) -> str:
    return f", {_esc(p.segment)}" if p.segment else ""


def _partner_name(p) -> str:
    return _esc(p.name)


# ================================================================= ALERT BLOCK (one line + notes)

def _lifecycle_clause(a: Alert) -> str:
    """The short trailing clause folded onto the SAME line as the alert itself - NEW gets
    nothing (it's the default, unremarkable case), ESCALATED/RECURRING get a few words. FIX
    (2026-11-13, user-picked option A of 3 shown against real 2026-11-12 data): replaces the
    standalone "[TAG] STATUS" line/sentence every earlier format used."""
    if a.lifecycle == Lifecycle.ESCALATED:
        return " - just escalated"
    if a.lifecycle == Lifecycle.RECURRING:
        return f" - still open (day {a.lifecycle_day})"
    return ""


def render_alert_line(a: Alert) -> str:
    severity = _bold(a.severity.value)
    clause = _lifecycle_clause(a)
    if a.type == AlertType.INCIDENT:
        label = _incident_header_label(a.lines)
        return f"{severity} - {label} incident:{clause}"
    if a.type == AlertType.DEVIATION:
        body = render_deviation_line(a.metric.label, a.metric.unit, a.dimension,
                                      a.deviation.value, a.deviation.baseline, a.deviation.baseline_type)
    elif a.type == AlertType.STREAK:
        body = render_streak_body(a.metric.label, a.streak.streak_days, a.streak.clicks_per_day)
    elif a.type == AlertType.BREADTH:
        body = render_breadth_body(a.metric.label, a.breadth.affected, a.breadth.total)
    else:
        raise ValueError(f"unknown alert type {a.type!r}")
    return f"{severity} - {body}{clause}"


def render_alert_block(a: Alert) -> List[str]:
    lines = [render_alert_line(a)]
    if a.type == AlertType.INCIDENT:
        lines.extend(render_incident_lines(a.lines))
    metric_label = a.metric.label if a.metric else None
    for note in sorted(a.notes, key=_note_sort_key):
        lines.append(render_note(note, metric_label))
    return lines


# ================================================================= ENTITY / SECTION HEADERS

def _format_brands(brands) -> str:
    brands = sorted(brands)
    if len(brands) <= 1:
        return brands[0] if brands else ""
    prefix = os.path.commonprefix(brands)
    last_dot = prefix.rfind('.')
    if last_dot == -1:
        return ', '.join(brands)
    parts = [brands[0]] + [b[last_dot:] for b in brands[1:]]
    return ', '.join(parts)


def render_partner_header(entity_alerts: List[Alert]) -> str:
    p = entity_alerts[0].partner
    brands = sorted({b for a in entity_alerts for b in a.brands})
    head = f"{_partner_tag(p)} ({_bold(_partner_name(p))}{_segment_suffix(p)}) - {_format_brands(brands)}"
    if len(entity_alerts) > 1:
        head += f" - {len(entity_alerts)} alerts"
    return head


def render_brand_header(brand: str, entity_alerts: List[Alert]) -> str:
    geo = None
    for a in entity_alerts:
        if a.geo_mix:
            geo = a.geo_mix
            break
    head = _bold(brand)
    if geo:
        geo_text = ', '.join(f"{k} {v}" for k, v in geo.items())
        head += f" ({geo_text})"
    n = len(entity_alerts)
    head += f" - {n} alert" + ("" if n == 1 else "s")
    return head


def render_section_header(owner: str) -> str:
    if owner == PLATFORM:
        return _bold("Not partner-specific")
    if owner == UNASSIGNED:
        return _bold("UNASSIGNED (no AM tag)")
    return _bold(owner)


# ================================================================= SORTING

_LIFECYCLE_RANK = {Lifecycle.ESCALATED: 2, Lifecycle.RECURRING: 1, Lifecycle.NEW: 0}


def _alert_magnitude(a: Alert) -> float:
    """How big a deal this alert's own numbers say it is, generalized across every alert type.
    Confirmed against the one real same-severity/same-lifecycle tie in the fixture
    (partner_9552's two NEW WARNINGs): Clicks (-99.9%) sorted before Avg first deposit (-35%) -
    bigger |% change| first."""
    if a.type == AlertType.STREAK:
        return a.streak.clicks_per_day
    if a.type == AlertType.BREADTH:
        return a.breadth.affected
    if a.type == AlertType.DEVIATION:
        v, b = a.deviation.value, a.deviation.baseline
        if b:
            return abs((v - b) / b)
        return abs(v)
    if a.type == AlertType.INCIDENT:
        return max(_alert_magnitude_line(ln) for ln in a.lines)
    return 0.0


def _alert_magnitude_line(ln) -> float:
    v, b = ln.deviation.value, ln.deviation.baseline
    if b:
        return abs((v - b) / b)
    return abs(v)


def _partner_id_for_sort(a: Alert) -> int:
    return a.partner.id if a.partner else -1


def alert_sort_key(a: Alert):
    # Generalized fixture rule: INCIDENT ranks before a plain alert at the same severity/
    # lifecycle tier, ahead of magnitude - reconciles the fixture's 3-way PLATFORM tie on
    # Brazino777.ar (GGR+RTP incident, NGR/GGR, Clicks).
    return (
        0 if a.severity == Severity.CRITICAL else 1,
        -_LIFECYCLE_RANK[a.lifecycle],
        0 if a.type == AlertType.INCIDENT else 1,
        -_alert_magnitude(a),
        _partner_id_for_sort(a),
    )


def _entity_key(a: Alert):
    return a.partner.id if a.scope == Scope.PARTNER else a.brands[0]


def _owner_sort_key(owner: str):
    if owner == PLATFORM:
        return (2, "")
    if owner == UNASSIGNED:
        return (1, "")
    return (0, owner.lower())


# ================================================================= TOP-LEVEL RENDER (§5 splitting excluded - see alert_telegram.py)

TELEGRAM_CHAR_BUDGET = 3500  # headroom under the real 4096 cap - see alert_telegram.py


def build_sections(alerts: List[Alert]):
    """Returns [(owner, [(entity_header_line, [alert_block_lines, ...]), ...]), ...] in final
    display order - the shape alert_telegram.py's splitter consumes (it never needs to re-sort
    or re-group, only pack and, if needed, repeat a section header on a continuation message)."""
    by_owner = {}
    for a in alerts:
        by_owner.setdefault(a.owner, []).append(a)

    sections = []
    for owner in sorted(by_owner, key=_owner_sort_key):
        owner_alerts = by_owner[owner]
        by_entity = {}
        for a in owner_alerts:
            by_entity.setdefault(_entity_key(a), []).append(a)
        entity_order = sorted(by_entity, key=lambda k: min(alert_sort_key(a) for a in by_entity[k]))
        entities = []
        for ekey in entity_order:
            entity_alerts = sorted(by_entity[ekey], key=alert_sort_key)
            if entity_alerts[0].scope == Scope.PARTNER:
                header = render_partner_header(entity_alerts)
            else:
                header = render_brand_header(entity_alerts[0].brands[0], entity_alerts)
            blocks = [render_alert_block(a) for a in entity_alerts]
            entities.append((header, blocks))
        sections.append((owner, entities))
    return sections


def header_counts(alerts: List[Alert]):
    n_critical = sum(1 for a in alerts if a.severity == Severity.CRITICAL)
    n_warning = sum(1 for a in alerts if a.severity == Severity.WARNING)
    n_escalated = sum(1 for a in alerts if a.lifecycle == Lifecycle.ESCALATED)
    return n_critical, n_warning, n_escalated


def render_message_header(date: str, alerts: List[Alert]) -> str:
    # FIX (2026-11-13, user-picked option A): dropped the severity-count line and the NEW/
    # ESCALATED/RECURRING legend added earlier the same day - the reference format the user
    # pointed at has neither, and each alert line now states its own severity and status
    # directly, so a separate summary/legend is redundant rather than explanatory.
    return _bold(f"Daily health check {EM_DASH} {date}")


def render(alerts: List[Alert], date: str) -> List[str]:
    """Pure: list[Alert] -> list[str] (one string per Telegram message - §5's char-budget
    splitting lives in alert_telegram.py, which calls build_sections()/render_message_header()
    directly rather than re-parsing this function's plain-text output)."""
    sections = build_sections(alerts)
    lines = [render_message_header(date, alerts)]
    for owner, entities in sections:
        lines.append("")
        lines.append(render_section_header(owner))
        for i, (header, blocks) in enumerate(entities):
            lines.append(header)
            # A blank line between an entity's OWN multiple alerts, not just between different
            # entities - a partner with 2+ alerts would otherwise run them straight into each
            # other with nothing but the next severity word marking the boundary.
            for j, block in enumerate(blocks):
                if j > 0:
                    lines.append("")
                lines.extend(block)
            if i < len(entities) - 1:
                lines.append("")
    return ["\n".join(lines)]
