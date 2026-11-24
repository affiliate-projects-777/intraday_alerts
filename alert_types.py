# -*- coding: utf-8 -*-
"""
Typed alert record model - the contract between detection (health_check_engine.py,
untouched by this change) and rendering (alert_render.py). An Alert is the unit the
renderer consumes; nothing downstream of this module should ever re-derive a fact
from a raw rule_id/DataFrame row again - if the renderer needs it, it belongs on the
record, set once by alert_mapping.py.

Deliberately plain dataclasses (not frozen) - alert_mapping.py builds these
incrementally (set severity, then attach notes, etc.) before handing them to the
renderer, which never mutates them.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


class Lifecycle(str, Enum):
    NEW = "NEW"
    RECURRING = "RECURRING"
    ESCALATED = "ESCALATED"


class EscalationReason(str, Enum):
    STREAK = "STREAK"
    NONPOSITIVE_N_DAYS = "NONPOSITIVE_N_DAYS"
    SEVERITY_UP = "SEVERITY_UP"


class AlertType(str, Enum):
    DEVIATION = "DEVIATION"
    STREAK = "STREAK"
    BREADTH = "BREADTH"
    INCIDENT = "INCIDENT"


class Unit(str, Enum):
    COUNT = "count"
    RATE = "rate"
    MONEY = "money"
    RATIO = "ratio"
    # FIX (2026-11-13, user feedback: "4,005.8% is complicated, keep the absolute number and
    # the % of change"): ENG-01/02 ("Engagement (Bets/Deposits)") is a turnover MULTIPLE (bets
    # can be many times deposits, since winnings get rewagered) - showing it as a percentage
    # ("4,005.8%") obscures what's actually a plain ratio ("40.1x"). MULTIPLE displays the bare
    # value/baseline as "{N}x" with no percent sign; the headline change is still a relative
    # percentage (scale-invariant, so "320% rise" is identical whether the ratio is shown as
    # 40.06 or 4,005.8%).
    MULTIPLE = "multiple"


class BaselineType(str, Enum):
    AVG_7D = "AVG_7D"
    PRIOR_WEEK = "PRIOR_WEEK"
    # FIX (2026-11-13, finding #149): RET-02 (D30 retention) compares to the prior COMPLETED
    # MONTH, not a prior week - rendering that as "last week's figure" (the only prior-period
    # wording that existed before) would be factually wrong, not just imprecise.
    PRIOR_MONTH = "PRIOR_MONTH"
    EXPECTED = "EXPECTED"
    NONE = "NONE"


class DimensionType(str, Enum):
    LANDING_PAGE = "landing_page"
    TRACKING_LINK = "tracking_link"
    SEGMENT = "segment"
    NONE = "none"


class Confidence(str, Enum):
    CONFIRMED = "CONFIRMED"
    LIKELY = "LIKELY"
    POSSIBLE = "POSSIBLE"


class HistoryFlag(str, Enum):
    NEVER_IN_LOOKBACK = "NEVER_IN_LOOKBACK"
    NONE = "NONE"


class Scope(str, Enum):
    PARTNER = "partner"
    BRAND = "brand"


UNASSIGNED = "UNASSIGNED"
PLATFORM = "PLATFORM"


@dataclass(frozen=True)
class PartnerRef:
    id: int
    name: str
    segment: Optional[str] = None


@dataclass(frozen=True)
class Dimension:
    type: DimensionType = DimensionType.NONE
    id: Optional[str] = None
    label: Optional[str] = None


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: Unit


@dataclass(frozen=True)
class BreadthItem:
    id: str
    change: float  # signed pct change, e.g. -100.0, -99.0


# ---- type-specific payloads (exactly one is set, matching Alert.type) ----

@dataclass(frozen=True)
class DeviationData:
    value: float
    baseline: Optional[float]
    baseline_type: BaselineType = BaselineType.NONE


@dataclass(frozen=True)
class StreakData:
    streak_days: int
    clicks_per_day: float
    lookback_days: int
    history_flag: HistoryFlag = HistoryFlag.NONE


@dataclass(frozen=True)
class BreadthData:
    affected: int
    total: int
    items: tuple = ()  # tuple[BreadthItem, ...]


@dataclass(frozen=True)
class IncidentLine:
    metric: Metric
    deviation: DeviationData


# ---- notes ----

@dataclass(frozen=True)
class AttributionNote:
    partners: tuple  # tuple[PartnerRef, ...]
    share: float
    confidence: Confidence


@dataclass(frozen=True)
class CustomerConcentrationNote:
    partner: PartnerRef
    shares_by_metric: tuple  # tuple[(Metric, float), ...] - ordered, dict would lose order


@dataclass(frozen=True)
class SharedAssetNote:
    partner_count: int
    own_share: float


@dataclass(frozen=True)
class EscalationNote:
    reason: EscalationReason


@dataclass(frozen=True)
class HistoryNote:
    metric_label: str
    lookback_days: Optional[int] = None  # None = "window" wording


@dataclass(frozen=True)
class TopItemsNote:
    items: tuple  # tuple[BreadthItem, ...], already the top-3-by-magnitude slice
    more: int = 0  # how many beyond the shown items


@dataclass(frozen=True)
class DeclaredPairNote:
    """A fixed, business-declared rule pairing (engine's ENTITY_CORRELATION_LINKS) also fired
    for the same partner, same day - e.g. CLK-13 <-> REG-13. Computed by apply_correlation()
    since the project started but never surfaced by this typed-record renderer until the
    2026-11-13 format revision (AUDIT_HISTORY.md finding #144's own §c gap)."""
    linked_labels: tuple  # tuple[str, ...] - plain-language metric labels, already deduped


@dataclass(frozen=True)
class FormulaPairNote:
    """A Project-level NGR drop that the engine's FORMULA_COUPLING links to same-day alerts it is
    arithmetically built from (NGR = GGR - bonuses - admin fees)."""
    metric_label: str  # the derived metric's own label, e.g. "NGR"
    linked_labels: tuple  # tuple[str, ...] - plain-language labels of the explaining alerts, deduped


@dataclass(frozen=True)
class LagNote:
    """A one-day-lag relationship (engine's LAG1_TRIGGER_RULE/LAG1_FOLLOWUP_RULES) - yesterday's
    trigger rule fired at Project level, same project. `shared_partner` is set only when a real
    cross-day driver was confirmed (same partner behind both days' moves); None means the timing
    lines up but no shared driver was found, so the rendered note must say "possibly", not "is"."""
    trigger_label: str
    shared_partner: Optional[PartnerRef] = None


Note = object  # documentation only - a Note is one of the 8 dataclasses above


@dataclass
class Alert:
    date: str
    owner: str  # an AM's display name, or UNASSIGNED, or PLATFORM
    scope: Scope
    brands: tuple  # tuple[str, ...] - brand domains, e.g. ("Brazino777.mx",)
    severity: Severity
    lifecycle: Lifecycle
    type: AlertType
    metric: Optional[Metric] = None  # None only for type == INCIDENT (each line has its own)
    geo_mix: Optional[dict] = None  # brand property: {"VE": 22, "BO": 11, ...}
    partner: Optional[PartnerRef] = None
    dimension: Dimension = field(default_factory=Dimension)
    lifecycle_day: Optional[int] = None
    escalation_reason: Optional[EscalationReason] = None
    deviation: Optional[DeviationData] = None
    streak: Optional[StreakData] = None
    breadth: Optional[BreadthData] = None
    lines: tuple = ()  # tuple[IncidentLine, ...] - only for type == INCIDENT
    notes: tuple = ()

    def __post_init__(self):
        validate_alert(self)


def validate_alert(a: "Alert") -> None:
    """Fail loudly - per the task's own §6, never silently render around a broken record."""
    if a.escalation_reason is not None and a.lifecycle != Lifecycle.ESCALATED:
        raise ValueError(
            f"Alert.escalation_reason={a.escalation_reason!r} set but lifecycle={a.lifecycle!r} "
            f"(must be ESCALATED)")
    if a.lifecycle == Lifecycle.RECURRING and a.lifecycle_day is None:
        raise ValueError("Alert.lifecycle == RECURRING requires lifecycle_day to be set")
    if a.type == AlertType.INCIDENT and len(a.lines) < 2:
        raise ValueError(f"Alert.type == INCIDENT requires >= 2 lines, got {len(a.lines)}")
    if a.type == AlertType.DEVIATION and a.deviation is None:
        raise ValueError("Alert.type == DEVIATION requires `deviation` to be set")
    if a.type == AlertType.STREAK and a.streak is None:
        raise ValueError("Alert.type == STREAK requires `streak` to be set")
    if a.type == AlertType.BREADTH and a.breadth is None:
        raise ValueError("Alert.type == BREADTH requires `breadth` to be set")
    if a.type == AlertType.INCIDENT and a.metric is not None:
        raise ValueError("Alert.type == INCIDENT carries its metric per-line, not on the alert")
    if a.type != AlertType.INCIDENT and a.metric is None:
        raise ValueError(f"Alert.type == {a.type!r} requires `metric` to be set")
    if a.scope == Scope.PARTNER and a.partner is None:
        raise ValueError("Alert.scope == partner requires `partner` to be set")
    if a.scope == Scope.BRAND and a.partner is not None:
        raise ValueError("Alert.scope == brand must not carry a `partner` (use notes for drivers)")
