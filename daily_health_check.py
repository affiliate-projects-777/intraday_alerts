# -*- coding: utf-8 -*-
"""
Daily health-check runner - meant to be invoked by Windows Task Scheduler.

For each project with meaningful recent volume, pulls a rolling window ending yesterday,
runs the full rule engine (health_check_engine.py), computes the realistic NEW/ESCALATION
daily-notification stream, and writes one Excel workbook per run to OUT_DIR, then opens it
and/or fires a Windows toast notification.

Requires customers/tqa_model/.env to exist (see .env.example) - this is a known gap, see
CLAUDE.md at the repo root ("Known security issue"). Until it exists this script exits with
a clear error instead of a bare KeyError traceback.
"""
import os
import sys
import re
import json
import datetime as dt
import subprocess
import traceback
import html

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'customers', 'tqa_model')))

# FIX (2026-11-12, caught running the real entry point for a real date): Windows' default
# console codepage (cp1252) can't encode the emoji the new alert_render.py/alert_telegram.py
# modules introduced (the message text itself is fine - requests.post() sends it as UTF-8 over
# HTTP, unaffected by console encoding) - but log()'s own print(line), and run_for_date.py's
# print(part) of the actual message content, both write to this process's stdout, which
# crashed with UnicodeEncodeError the moment that content held a real "\U0001F4CA" character.
# Reconfigured once here (daily_health_check is imported by both main() and run_for_date.py)
# rather than in each entry point separately. Guarded - reconfigure() can be unavailable if
# stdout is replaced by something unusual (a very old Python, or a non-stream redirect).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

OUT_DIR = r'C:\Users\dsfetsori_flyxmarket\Documents\traffic\reports\daily_health_check'
LOG_PATH = os.path.join(OUT_DIR, 'run_log.txt')
SENT_LOG_PATH = os.path.join(OUT_DIR, 'telegram_sent_log.json')  # eval_date -> [sent timestamps], idempotency guard

# project_id -> real brand name, sourced 2026-11-05 from the `projects` table (replica_run_query
# "SELECT * FROM projects WHERE id IN (...)") - confirmed 7 project_ids exist in projects_statistics
# (1,2,3,4,6,7,10); project 2 ("Wazino777") is disabled (is_enabled=0) and excluded per user
# instruction. Projects 1/7/10 have near-zero recent FTD volume (3/14/1 rows respectively in a
# recent 30-day check) but are included per explicit user instruction ("ALL apart from 2") rather
# than the previous volume-based exclusion - a quiet project can still have an alert worth seeing
# the day it wakes up. "Project 4"/"Project 6" placeholder labels replaced with their real names.
PROJECTS = {
    1: 'Brazino777.bet.br',
    3: 'Brazino777.mx',
    4: 'Brazino777.io',
    6: 'Brazino777.ar',
    7: 'Brazino777.in',
    10: 'Brazino777.py',
}

# Project 4 (Brazino777.io) is the only project with meaningful internal country segmentation -
# projects_statistics.country varies within it (AR/BO/CL/CO/EC/VE are real target markets; this
# set matches the P4_COUNTRIES convention already used across customers/tqa_model's own analysis
# scripts, e.g. flag_check.py, kpi_month.py - not invented here). Projects 6/7/10 are already
# single-country brands (.ar/.in/.py) by project_id itself, so they don't need this.
P4_COUNTRIES = ['AR', 'BO', 'CL', 'CO', 'EC', 'VE']

BUFFER_DAYS = 35  # history pulled before the eval day, for 7-day baselines + multi-day escalation runs

SEV_RANK = {'INFO': 0, 'WARNING': 1, 'CRITICAL': 2}  # for sorting - alphabetical order puts WARNING above CRITICAL

# FIX (2026-11-12, user feedback: "it messes same day and previous day alerts and it is not
# clear"): secondary sort key, within the same severity tier - a fresh NEW or ESCALATED alert
# (something actually changed today) now lists above a REMINDER (a chronic condition just
# re-surfacing on its 7-day cadence, nothing new happened). Works together with _status_tag()'s
# [NEW]/[ESCALATED]/[DAY N] tag on the alert line itself - the tag says what it is, this makes
# sure today's real news isn't buried below a week of old reminders of the same severity.
TIME_RANK = {'NEW': 2, 'ESCALATION': 2, 'REMINDER': 0}


def _esc(x):
    """html.escape() for Telegram's HTML parse_mode, but quote=False - this text only ever
    appears in the message body, never inside an HTML attribute value, so escaping quotes/
    apostrophes (the default) only adds visual noise like "don&#x27;t" for no safety benefit;
    '<', '>', '&' are the only characters that can actually break HTML parse_mode here."""
    return html.escape(str(x), quote=False)


def log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


FINANCE_DOLLAR_RULES = {'FDP-01', 'FDP-02', 'FDP-03', 'AFD-01', 'DPS-01', 'DPS-02', 'DPS-03',
                         'GGR-01', 'GGR-02', 'GGR-03', 'ADM-01', 'ADM-02', 'ADM-03', 'BON-01',
                         'WDR-01', 'WDR-02', 'NGR-01', 'FGR-01'}
LOW_MATERIALITY_USD = 300  # below this $ swing, flag as likely noise rather than a real signal


def worth_it_verdict(row):
    """Deterministic materiality check on top of the engine's own floor/suppression logic
    (Fixes 1-6) - re-reads the real DB-sourced value/baseline this alert already carries and
    flags alerts whose absolute size is too small to act on, same reasoning used manually
    for the BAB-02/RTP-01 false alarms earlier. Reworded 2026-11-05 ("still messy" pass) to
    read as a plain clause instead of an "OK -"/"LOW -" status-code prefix, since it's now
    spliced into one flowing sentence by rule_family_action() rather than shown standalone.
    The BAB/RTP/final-fallback branches below are unreachable (the only call site only invokes
    this when rid is in FINANCE_DOLLAR_RULES) - pre-existing dead code, left as-is, out of
    scope for this wording pass."""
    rid = row['rule_id']
    val = row['value']
    base = row['baseline']
    if rid in FINANCE_DOLLAR_RULES:
        if val is not None and base is not None:
            swing = abs((val or 0) - (base or 0))
            if swing < LOW_MATERIALITY_USD:
                return f"a small ${swing:,.0f} move, likely just noise"
            return f"a ${swing:,.0f} move"
        return "not enough data yet to judge the size of this move"
    if rid.startswith('BAB'):
        return f"a ratio of {val:.2f} against the usual bets floor"
    if rid.startswith('RTP'):
        return "within an unvalidated placeholder band, so treat the number loosely"
    return "no specific note"


# FIX (2026-11-05 AM-friendly Telegram design proposal, S5; reworded twice more on 2026-11-05
# per direct user feedback - first "plain language, drop jargon", then "still messy, keep it
# simpler but useful"): deterministic, per-rule-family "what should the AM investigate/do"
# guidance. Replaces worth_it_verdict()'s single boilerplate fallback, which the Telegram audit
# found gave 0/10 real sample alerts anything differentiated outside the $/BAB/RTP families.
#
# Each string here is now a SHORT, lowercase-starting clause (one sentence, no internal
# branching prose) - render_single()/render_group() splice it onto the end of one combined
# sentence per alert. This is a deliberate cut from the first "plain language" pass, which
# wrote 2-4 full sentences per rule family: on a real 15-alert day (2026-10-30 backfill), that
# verbatim-repeated paragraph (the same CLK-12 text alone appeared 4+ times) pushed the message
# over the 4000-char cap and triggered truncation on an ordinary day, which is a worse outcome
# than the jargon it replaced. No rule-ID cross-references or internal doc-section citations
# here either - an AM reading on a phone has no access to this codebase or BUSINESS_CONTEXT.md.
#
# Deviation from the design proposal's literal S5 table, noted for the record: the table's
# "$-family" row appends a "GGR/NGR swings are game-outcome" caveat to EVERY dollar rule; that's
# only actually true for GGR/NGR/FGR (see BUSINESS_CONTEXT.md's "partner responsibility scope" -
# deposits/bets/bonuses ARE partner-attributable, only wins/payouts/GGR/NGR are not). Applying it
# to FDP/DPS/ADM/BON/WDR/AFD too would contradict that distinction, so it's scoped to GGR/NGR/FGR
# only here.
def rule_family_action(row):
    rid = row['rule_id']
    level = row['level']
    prefix, _, suffix = rid.partition('-')
    suffix_num = int(suffix) if suffix.isdigit() else None

    if rid in FINANCE_DOLLAR_RULES:
        base = worth_it_verdict(row)
        if prefix in ('GGR', 'NGR', 'FGR'):
            return base + " - also check if a few big players explain this before blaming the partner"
        return base
    if prefix == 'BAB':
        return "looks like bonus abuse, not real play - worth checking this partner's bonus terms"
    if prefix == 'RTP':
        return "win/loss rate is off - flag to the games team, only involve the partner if it's clearly tied to their traffic"
    if rid == 'NGG-01':
        return "revenue retained dropped week over week - treat as a rough signal for now"
    if prefix in ('CLK', 'REG', 'FTD'):
        if suffix_num in (12, 15, 17):  # complete collapse: Partner/GUID/LP
            return "looks like a tracking outage or fraud - confirm tracking before reaching out, and stop paying if it holds up"
        if suffix_num in (13, 18):  # breadth drop: Partner/LP
            txt = "several of this partner's tracking links dropped at once - check if it's partner-wide before raising it with them"
            if level == 'Landing Page':
                txt += "; this page is shared with other partners, so double-check who's really responsible"
            return txt
        if suffix_num in (14, 16, 19):  # spike: Partner/GUID/LP
            txt = "traffic jumped sharply - check if signups or deposits grew too, otherwise this may be fake traffic"
            if level == 'Landing Page':
                txt += "; this page is shared with other partners, so double-check who's really responsible"
            return txt
        return "a platform-wide trend, not one partner's issue"
    if prefix == 'C2R':
        return "fewer clicks are turning into signups - likely a traffic-quality or landing-page issue"
    if prefix == 'R2F':
        txt = "signups aren't converting to deposits - likely a payment or onboarding issue"
        note = row.get('note')
        if note and 'CPA-gaming' in str(note):
            txt += "; this pattern looks like it could be gaming the CPA payout rule, worth checking by hand"
        return txt
    if rid in ('STD-01', 'STD-02'):
        return "returning-player rate is low - a strong sign of fake or incentivized traffic; check for a recent spike on this partner too"
    if prefix == 'ENG':
        return "deposits aren't being played much - could mean bonus abuse rather than a traffic problem"
    return "a platform-wide trend, not one partner's issue"


def _fmt_num(x):
    """Thousands-separated number formatting for the value/baseline fields on an alert line.
    2026-11-05 "still messy" pass: a 7-day rolling-average baseline on a count metric (clicks,
    registrations, FTDs) is never a whole number (e.g. 68880.86), and showing those cents-like
    decimals on a click count read as noise, not precision - real example from a 2026-10-30
    backfill. Round to a whole number once the magnitude is large enough that a fraction isn't
    meaningful (>=100); below that (redeposit-rate percentages, BAB ratios, small dollar
    amounts) keep up to 2 decimals, trimmed of trailing zeros, e.g. 15.60 -> "15.6"."""
    if x is None or pd_isna(x):
        return "-"
    if abs(x) >= 100:
        return f"{x:,.0f}"
    s = f"{x:,.2f}"
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s


def _share_text(share, default="most"):
    """Formats a correlation/attribution share as a percentage, with a caveat when it exceeds
    100% - mathematically real, not a bug, for netting columns (GGR/admin_fee/NGR): one
    customer's dominant swing can exceed the day's NET total if other customers moved the
    opposite way (correlate_alerts()'s own docstring, health_check_engine.py, already validates
    a real 364.7% ADM-01 case on this basis). FIX (2026-11-12, user-reported real example):
    confirmed live the same day - partner_124's GGR share rendered as a bare "134%" with zero
    explanation, which reads as an obvious error to an AM instead of the real, already-validated
    netting effect it actually is."""
    if share is None or pd_isna(share):
        return default
    pct = abs(share) * 100
    # FIX (2026-11-12, user feedback: "this is not needed, we know it"): the >100%-is-possible
    # explanation (added per the health-check-auditor's wording review the same day) was itself
    # unnecessary - the team already understands the netting effect; just show the number.
    return f"{pct:.0f}%"


# FIX (2026-11-12, user feedback: "the AM will not know what 'R2F-15' means"): every note that
# cross-references ANOTHER alert used to show the bare rule_id. Prefix-based, same dispatch
# pattern as _baseline_label()/_is_percentage_metric() - not every one of the ~113 rule_ids
# needs its own entry, just its family's plain name.
_RULE_ID_LABELS = {
    'CLK': 'Clicks', 'REG': 'Registrations', 'FTD': 'FTDs', 'C2R': 'Click-to-signup rate',
    'R2F': 'Signup-to-deposit rate', 'GGR': 'GGR', 'NGR': 'NGR', 'NGG': 'NGR/GGR ratio',
    'RTP': 'RTP', 'ADM': 'Admin fees', 'BON': 'Bonuses', 'WDR': 'Withdrawals',
    'DPS': 'Deposits $', 'WAG': 'Bets $', 'STD': 'Returning-player rate',
    'AFD': 'Avg first deposit', 'BAB': 'Bonus abuse', 'FGR': 'FTD GGR',
    'FDP': 'First deposit $', 'FBT': 'Bets engagement', 'FBN': 'Bonus engagement',
    'DEP': 'Depositors', 'GEO': 'Foreign-geo share', 'ENG': 'Engagement ratio',
}


def _rule_id_label(rid):
    """Plain-language label for a bare rule_id (e.g. "R2F-15") appearing in a cross-reference
    note - an AM has no reason to know what any rule_id means."""
    return _RULE_ID_LABELS.get(rid.partition('-')[0], rid)


def _status_tag(row):
    """Short [STATUS] tag (NEW / ESCALATED / DAY N) for the alert line itself.

    FIX (2026-11-12, user feedback: "it messes same day and previous day alerts and it is not
    clear"): this used to be two separate trailing-sentence note functions, _reminder_note()
    ("Still open, day N - recurring reminder, not a new problem.") and _escalation_note() ("Just
    escalated from WARNING to CRITICAL..."), both now REMOVED. Whether something is brand new
    today, just got worse, or is a week-old reminder is the single fastest thing an AM needs to
    see, so it's now a short tag read in the same glance as the severity word, not a second
    sentence to read to the end of. build_telegram_message() also now sorts NEW/ESCALATED above
    REMINDER within each severity tier (see TIME_RANK there) - the tag and the ordering work
    together to stop today's fresh alerts and old ones still being open from reading as one
    undifferentiated list."""
    # FIX (2026-11-12, user-directed report format - see RULES.md's "AM/Project alert line
    # format" note): wording changed from "DAY N"/"ONGOING" to "RECURRING (day N)"/"RECURRING",
    # matching the status vocabulary the user's own analyst-report spec names explicitly
    # (NEW/ESCALATED/RECURRING) and kept unchanged when editing the generated report by hand.
    et = row.get('event_type')
    if et == 'ESCALATION':
        return 'ESCALATED'
    if et == 'REMINDER':
        days = row.get('open_days')
        return f"RECURRING (day {int(days)})" if days and not pd_isna(days) else "RECURRING"
    return 'NEW'


def entity_label(row):
    """Already-available-data entity label (2026-11-05 design proposal S6.1), reworded 2026-11-05
    for plain language: a tracking-link (GUID) line shows its resolved traffic_type (already a
    promo_map column, zero new query) and drops the raw "guid_" prefix in favor of a plain
    "tracking link N" phrasing - that prefix never appears in BUSINESS_CONTEXT.md's own
    human-facing prose, unlike "partner_N" which does, so it's dropped here but partner_N is
    kept as-is. Same treatment for "lp_N" -> "landing page N"."""
    level = row['level']
    entity = row['entity']
    num = entity.split('_', 1)[1] if '_' in entity else entity
    if level == 'GUID':
        tt = row.get('traffic_type')
        base = f"tracking link {num}"
        return f"{base} ({tt})" if tt else base
    if level == 'Landing Page':
        return f"landing page {num}"
    if level == 'Partner':
        return partner_label(int(num))  # finding #25: name alongside the raw partner_N id
    if level == 'Project/Traffic':
        return f"{entity} traffic (platform-wide)"
    return "platform-wide"  # level == 'Project'


def attribution_tag(row):
    """Promote the already-computed LP dominant-partner share from a conditionally-shown bracket
    note to a plain-language confidence caveat (2026-11-05 design proposal S6.1/S8). Returns a
    standalone trailing clause (capitalized, no wrapping parens/brackets) rather than an inline
    parenthetical - 2026-11-05 "still messy" pass: nesting this inside the alert's own
    "(Project, entity)" parenthetical produced double-nested parens, which is exactly the kind
    of symbol clutter that feedback flagged. Empty string when there's nothing to caveat.

    FIX (2026-11-10): the previous wording ("probably the right one") never said what "it" or
    "the right one" referred to - user feedback: this still doesn't make sense, and the real fix
    is the computation, not the words (see attribute_alerts()'s date-scoping fix, same date).
    Now names the actual partner and the real number behind the confidence tier, so the caveat
    is a specific, checkable claim instead of a vague hedge.

    FIX (2026-11-11, user feedback: "you can check the attribution and return the correct
    alert, this is noise"): the 60-85% tier used to print a "probably right, worth a quick
    check" caveat on every such alert - but 60% isn't actually borderline. With N>=2 partners
    splitting 100% of a landing page's clicks, a single partner at >=60% mathematically CANNOT
    be beaten by any other single partner (the rest share at most 40% between them, however
    many there are) - a real, unambiguous majority, not a close call. Collapsed the old
    85%-cutoff HIGH tier and the 60-85% MEDIUM tier into one: >=60% is trusted outright, same
    as the old >=85% tier always was. The caveat now only fires for genuine ambiguity - no
    partner holds a real majority of this landing page's clicks that day.

    FIX (2026-11-12, user feedback: "more brief, with structure and bullets"): cut from a full
    justifying sentence to a short bulleted fact - the entity line above already says which
    landing page this is, so this only needs to add "shared, owner unclear, here's why". Also
    dropped the partner name/id from this specific note (unlike every other note, which names
    the partner because it's cross-referencing a DIFFERENT entity) - this one is always read
    directly under that same partner's own header, so repeating "partner_11570 (gentoo (Meta
    LatAm))" right back added length without adding information."""
    attribution = str(row.get('attribution', ''))
    if not attribution.startswith('lp-dominant'):
        return ""
    m = re.search(r'(\d+)% of (\d+) partners', attribution)
    if not m:
        return "Shared page - attribution unresolved, check the detailed report."
    share, n = int(m.group(1)), int(m.group(2))
    if share >= 60:
        return ""
    return f"Shared with {n} partners (only {share}% of its clicks) - owner unclear, verify before acting."


def assemble_notes(row, skip_correlation=False):
    """FIX (2026-11-10, AUDIT_HISTORY.md finding #83): one shared note-assembler, called
    identically by render_single()/render_group()/render_cluster() below. Previously
    render_single() called all 5 note functions, render_group() called 3 (missing solo-whale
    and entity-pair), and render_cluster() called none at all - so a merged correlation cluster
    could never show chronic/REMINDER/ESCALATION status, purely because of which render path
    happened to handle that row, not because the signal didn't apply. A note added here now
    reaches every render path at once.

    FIX (2026-11-12, user feedback): _reminder_note()/_escalation_note() removed from this list
    - that information now lives in _alert_line()'s own [STATUS] tag (see _status_tag()), read
    in the same glance as severity instead of needing a second sentence.

    `skip_correlation` (FIX 2026-11-12): render_cluster() passes True - it already states
    _correlation_note()'s exact same "likely not platform-wide, driven by X" fact itself, in its
    own leading bullet, computed from the cluster's ANCHOR (highest-share) member rather than
    `row` (render_cluster always passes its highest-SEVERITY member here, which is not
    guaranteed to be the same row as the anchor) - calling _correlation_note() here too would
    repeat the same claim verbatim.

    FIX (2026-11-12, user feedback: a real example - partner_8074's Clicks alert named BOTH "a
    flag in io" (a second alert for the same partner, already rendered right below this one in
    the same partner block) and "Signup-to-deposit rate, not shown separately today" (a linked
    rule_id that ISN'T shown anywhere) as if they were the same kind of thing. User: show it "in
    this same group" - i.e. only mention a cross-reference when the thing referenced is actually
    visible nearby; don't create a pointer to something that isn't. Two changes: (1)
    _cross_project_note() REMOVED entirely - the partner header above already names every
    project this partner has an alert in (`_partner_header()`'s own "Brazino777.ar &
    Brazino777.io" naming), so a per-alert "Also flagged in Brazino777.io" line was always
    redundant with it, never added information. (2) `_correlation_note()`'s/`render_cluster()`'s
    "(also affects X, not shown separately today)" tails dropped the same way - see those
    functions' own notes. This is a real reversal of this project's long-standing "never
    silently drop content" default (findings #23/#48 and others) - kept deliberately for
    THIS specific case, because the user gave direct, explicit instruction to stop describing
    things that aren't independently visible, not because the old default was wrong in general."""
    notes = [] if skip_correlation else [_correlation_note(row)]
    notes.extend([attribution_tag(row), _solo_whale_note(row), _entity_correlation_note(row), _formula_correlation_note(row),
                  _top3_whale_note(row), _lag1_note(row),
                  _ratio_hint_note(row), _dominant_partner_note(row), _note_highlight(row)])
    return [n for n in notes if n]


def _correlation_note(row):
    """FIX (2026-11-09, reports/design_correlation_check_2026-11-09.md, AUDIT_HISTORY.md
    finding #48): labels a Project-level finance/product alert that health_check_engine.py's
    correlate_alerts() found shares a single dominant customer with one or more other alerts
    firing the same project/day, so the AM reads "one event, several symptoms" instead of
    several seemingly unrelated problems. Annotate-only - never changes which alerts fire or
    their severity, just adds context to the Note: line.

    FIX (2026-11-12, user feedback: "more brief, with structure and bullets" / "outliers are
    used to show that a problem is not real"): leads with "likely not platform-wide" (this IS an
    outlier check: is the move real/systemic, or does it collapse to one customer?) instead of a
    bare share number.

    FIX (2026-11-12, user feedback: a real example - "Same cause as Signup-to-deposit rate - not
    shown separately today" read as a pointer to something the AM could never actually find;
    user: only reference things "in this same group" [i.e. actually visible]): this used to also
    append "(also affects X, not shown separately today)" for whatever OTHER rule_ids were
    linked but not independently shown - by construction (see render_rows()'s group_key()) that
    parenthetical ALWAYS described something invisible whenever it appeared, which is exactly
    the kind of dangling reference the user flagged. Dropped - this note now states only the one
    fact that's always true and useful (who's really driving this, not whether something else
    unseen is "also affected"), with no `already_shown` filtering needed any more (there is
    nothing left here to filter)."""
    linked = row.get('linked_rule_ids')
    if not linked or pd_isna(linked):
        return ""
    partner = row.get('linked_dominant_partner')
    share = row.get('linked_dominant_share')
    partner_text = partner_label(partner) or "one partner"
    share_text = _share_text(share)
    metric = row.get('metric') or "this"
    return f"Likely not platform-wide - {share_text} of {metric} driven by {partner_text}."


def _solo_whale_note(row):
    """FIX (2026-11-09, connections audit finding #75): a Project-level finance/product alert
    with no same-day sibling to cluster with (so correlate_alerts() never set `linked_rule_ids`)
    can still itself be driven almost entirely by one player - confirmed on real data that 21%
    of such lone alerts (15/72 in the validated window) clear the same CORRELATION_ANCHOR_SHARE
    bar `linked_rule_ids` clusters use, and were getting zero outlier context before this.

    FIX (2026-11-12, user feedback: "outliers are used to show that a problem is not real"):
    reworded to lead with that framing explicitly ("likely not platform-wide") instead of a
    neutral share statement, and shortened per "more brief, with structure and bullets"."""
    partner = row.get('solo_dominant_partner')
    share = row.get('solo_dominant_share')
    if partner is None or pd_isna(partner):
        return ""
    partner_text = partner_label(partner)
    share_text = _share_text(share)
    metric = row.get('metric') or "this"
    return f"Likely not platform-wide - {share_text} of {metric} driven by {partner_text}."


def _entity_correlation_note(row):
    """FIX (2026-11-09, connections audit findings #69/#71/#76): health_check_engine.py's
    correlate_entity_pairs() links same-partner/same-day alert pairs that have no common
    customer_id to point at (breadth share, zero-FTD-despite-traffic, average deposit size are
    all aggregate conditions) but are still confirmed, on a real 69-day/6-project sweep, to share
    one underlying cause often enough to be worth naming - see ENTITY_CORRELATION_LINKS.

    FIX (2026-11-12, user feedback: a real example - "Same cause as Signup-to-deposit rate - not
    shown separately today" on partner_8074's Clicks alert, where the Signup-to-deposit alert
    genuinely isn't rendered anywhere (SIBLING_SUPPRESSION suppressed it under this same Clicks
    rule, same partner/day - `entity_linked_rule_ids` is set by correlate_entity_pairs(), which
    runs BEFORE suppression, on the then-still-present row). User: only reference things "in
    this same group" - i.e. actually visible; a pointer to something the AM can never find adds
    nothing. Dropped the "not shown separately today" caveat entirely: if NOTHING linked is
    independently visible (checked against `_ENTITY_DATE_OPEN`, the final post-suppression set),
    this returns "" and says nothing, rather than describing something invisible. If SOMETHING
    is visible, names only that (via _rule_id_label(), never the raw rule_id) - a real, checkable
    cross-reference, not a dangling one. This is a deliberate, user-directed exception to this
    project's usual "never silently drop content" default (see assemble_notes()'s own note)."""
    linked = row.get('entity_linked_rule_ids')
    if not linked or pd_isna(linked):
        return ""
    tokens = [t.strip() for t in str(linked).split(',') if t.strip()]
    open_today = _ENTITY_DATE_OPEN.get((row.get('entity'), row.get('date')), set())
    visible = [t for t in tokens if t in open_today]
    if not visible:
        return ""
    visible_text = ', '.join(dict.fromkeys(_rule_id_label(t) for t in visible))
    return f"Same cause as {visible_text}."


def _formula_correlation_note(row):
    """2026-11-24: health_check_engine.py's correlate_formula_pairs() links a Project-level NGR-01 /
    NGG-01 alert to the same-day alerts it is arithmetically built from (NGR = GGR - bonuses -
    admin fees; NGG = NGR / GGR). Linked rows are non-INFO only, so they are always visible."""
    linked = row.get('formula_linked_rule_ids')
    if not linked or pd_isna(linked):
        return ""
    tokens = [t.strip() for t in str(linked).split(',') if t.strip()]
    if not tokens:
        return ""
    uniq = list(dict.fromkeys(_rule_id_label(t) for t in tokens))
    pron = "it" if len(uniq) == 1 else "them"
    text = f"Follows from {', '.join(uniq)}: {_rule_id_label(str(row.get('rule_id')))} is built from {pron}."
    if row.get('formula_small_denominator') is True:
        text += " GGR is near zero, so this ratio may be exaggerated by a small base."
    return text


def _note_highlight(row):
    """FIX (2026-11-12, health-check-auditor finding, general audit 2026-11-12): the engine's
    `note` field carries several genuinely high-value, AM-relevant signals that were never
    rendered anywhere except the Excel report - confirmed live the same day: R2F-12's CPA-gaming
    flag (AUDIT_HISTORY finding #24's own example, still unreachable 6 days after being marked
    "resolved" there - the rendering path that was supposed to carry it, rule_family_action(),
    is dead code with no caller, confirmed by grep); a breadth rule's own named hit-GUIDs
    (confirmed live: partner_11537's real CLK-13 today rendered "5 of 6 qualifying tracking
    links dropped" with ZERO GUID names, even though `note` already said "hit GUIDs: guid_30653
    -100%, guid_30656 -100%, guid_31043 -99%, +2 more"); and the two spec-declared cross-rule
    escalation reasons (GGR-03 escalating because RTP-02 also fired; NGR-01 escalating because
    NGR has stayed <=0 for 3+ days) - severity correctly jumps to CRITICAL for both, but the AM
    was never told why. Rendering the WHOLE `note` field would reintroduce engineering jargon for
    every other rule (most of `note` is just a guard description like "Deposits & bets within
    +/-10%", not AM-relevant on its own) - this extracts only these specific, known-valuable
    patterns by their exact, stable source text instead of attempting a generic parse.

    FIX (2026-11-12, user feedback: "more brief"): shortened each case to one short clause."""
    note = row.get('note')
    if not note or pd_isna(note):
        return ""
    note = str(note)
    if 'CPA-gaming' in note:
        return "Possible CPA-gaming - deposits may be timed to just clear the minimum; check manually."
    if 'hit GUIDs:' in note:
        return f"Links: {note.split('hit GUIDs:', 1)[1].strip()}."
    if 'NGR<=0 for >=3 consecutive days' in note:
        return "Escalated - NGR has stayed <=0 for 3+ days, not just a relative drop."
    if 'escalated: RTP-02' in note:
        return "Escalated - RTP<90% also fired today (compliance risk: wins under-recorded)."
    return ""


def _top3_whale_note(row):
    """FIX (2026-11-11, AUDIT_HISTORY.md finding #121): health_check_engine.py's
    correlate_alerts() now also checks whether the top 3 customers TOGETHER explain most of an
    alert's move when no single one clears the 70% solo-whale bar - catches "several whales, no
    one dominant" the way the solo check structurally can't.

    FIX (2026-11-12, user feedback: "outliers are used to show that a problem is not real"):
    same "likely not platform-wide" outlier framing as _solo_whale_note(), shortened."""
    partners = row.get('top3_dominant_partners')
    if not partners or pd_isna(partners):
        return ""
    # finding #25: `partners` arrives as health_check_engine.py's own id-only "partner_124,
    # partner_1441, ..." string (that module has no access to _PARTNER_NAMES) - re-render each
    # with its name via partner_label() rather than threading the lookup dict into the engine.
    named = ', '.join(partner_label(int(p)) for p in re.findall(r'partner_(\d+)', partners))
    share = row.get('top3_dominant_share')
    share_text = _share_text(share)
    metric = row.get('metric') or "this"
    return f"Likely not platform-wide - {share_text} of {metric} driven by {named} together."


def _dominant_partner_note(row):
    """FIX (2026-11-12, found while reviewing today's real Telegram output for the user's other
    5 reported issues): health_check_engine.py's annotate_project_dominant_partner() (finding
    #73 - a Project-level CLK/REG/FTD-02/03 "platform-wide" alert is, 77% of the time on a real
    69-day/6-project sweep, really driven by one partner's own large share of the project's
    baseline) writes `dominant_partner`/`dominant_partner_share` on every qualifying row, but no
    render function anywhere ever read them - confirmed by grep, this mechanism has been
    silently dead on the Telegram side since it shipped (2026-11-09), visible only in the Excel
    report. Confirmed live, same day as this fix: today's Brazino777.ar CLK-02 ("Clicks: 80%
    drop... platform-wide") has dominant_partner=8074 (ConvertoBR) at 61.9% share sitting
    unused in the data - the exact same partner whose own CLK-12 complete-collapse alert appears
    two sections above it in the very same Telegram message, with nothing connecting the two.

    FIX (2026-11-12, user feedback: "outliers are used to show that a problem is not real"):
    same "likely not platform-wide" framing as _solo_whale_note()/_top3_whale_note(), shortened
    and made consistent so an AM learns to recognize this one phrase as meaning the same thing
    everywhere it appears."""
    partner = row.get('dominant_partner')
    if partner is None or pd_isna(partner):
        return ""
    share = row.get('dominant_partner_share')
    share_text = _share_text(share)
    metric = row.get('metric') or "this"
    return f"Likely not platform-wide - {share_text} of {metric} driven by {partner_label(partner)}."


def _lag1_note(row):
    """FIX (2026-11-11, AUDIT_HISTORY.md finding #122): health_check_engine.py's
    correlate_lag1() links an ENG-02 engagement spike to a cash-out-side alert (WDR-02/NGR-01/
    FGR-01/GGR-03) firing for the same project exactly one day later - two alerts that used to
    read as fully unrelated.

    FIX (2026-11-11, user feedback): two problems with the original wording. (1) It named the
    raw rule_id ("ENG-02") - meaningless to an AM; uses the metric's own plain-language label
    instead (health_check_engine.py now carries it through as `prior_day_metric`). (2) It
    asserted "not a new issue" / "the same event continuing" from timing alone, with no check
    that the two alerts actually share anything - correlate_lag1() now only makes that claim
    when a real shared dominant partner was found (`prior_day_shared_partner`, named directly
    here); otherwise this says the link is unconfirmed.

    FIX (2026-11-11, user feedback: "still... can be simpler and brief"): cut to one short
    clause per case instead of a full sentence with a trailing qualifier.

    FIX (2026-11-12, user feedback: "more brief"): trimmed further - "same partner:" was
    redundant with naming the partner right after it."""
    prior_metric = row.get('prior_day_metric')
    if not prior_metric or pd_isna(prior_metric):
        return ""
    shared = row.get('prior_day_shared_partner')
    if shared is not None and not pd_isna(shared):
        return f"Linked to yesterday's {prior_metric} spike ({partner_label(shared)})."
    return f"Possibly linked to yesterday's {prior_metric} spike - unconfirmed."


def _ratio_hint_note(row):
    """FIX (2026-11-11, AUDIT_HISTORY.md finding #123): health_check_engine.py's
    annotate_ratio_numerator_hint() gives ratio-family rules (ENG-01/02, FBT-01, FBN-01, NGG-01)
    a soft, explicitly-labeled hint about who contributed most to the ratio's numerator that day
    - deliberately NOT the rigorous customer-identity attribution correlate_alerts() gives
    dollar/count rules (a ratio has no well-defined per-customer share), so the wording below
    stays hedged rather than claiming this is confirmed.

    FIX (2026-11-12, user feedback: "outliers are used to show that a problem is not real" /
    "more brief"): same outlier framing as the other whale notes, kept hedged ("possibly") since
    this one is explicitly a soft hint, not a confirmed attribution."""
    partner = row.get('ratio_hint_partner')
    if partner is None or pd_isna(partner):
        return ""
    share = row.get('ratio_hint_share')
    share_text = _share_text(share, default="a large share")
    return f"Possibly not platform-wide - {share_text} contributed by {partner_label(partner)} (unconfirmed hint)."


def _metric_label(metric):
    """Swap a couple of engineering-shorthand metric names for plain English (2026-11-05, "more
    AM-oriented" pass) - pure display relabeling, same underlying metric/value. Everything else
    (Clicks, GGR, NGR, RTP, Bonus abuse ratio, ...) is left as-is: those are real business terms
    BUSINESS_CONTEXT.md itself uses, not engineering jargon."""
    # FIX (2026-11-12): was an exact `==` match - broke the moment STD-01's own metric string
    # started embedding its traffic type (health_check_engine.py, same date, "FTD-to-STD
    # (Push/Pop)"), same prefix-match pattern as Click-to-Reg/Reg-to-FTD just below.
    if metric.startswith('FTD-to-STD'):
        return 'Returning-player rate' + metric[len('FTD-to-STD'):]
    if metric.startswith('Click-to-Reg'):
        return 'Click-to-signup rate' + metric[len('Click-to-Reg'):]
    if metric.startswith('Reg-to-FTD'):
        return 'Signup-to-deposit rate' + metric[len('Reg-to-FTD'):]
    return metric


def _project_label(row):
    """Real brand name from PROJECTS (via project_label, already set in main()/run_for_date.py),
    falling back to "Project N" only if that wasn't set (e.g. a synthetic test row)."""
    label = row.get('project_label')
    return label if label else f"Project {row['project_id']}"


def _baseline_label(rid):
    """What "baseline" actually means varies by rule family - confirmed by reading each
    emit()/results.append() call in health_check_engine.py, not assumed. Most rules really do
    compare against a 7-day rolling average (baseline()/ratio_sum_baseline()), but:
      - STD-01 compares this week to the PROJECT's own prior week (not this entity's history).
      - STD-02 compares a partner's rate to that same week's project-wide MARKET rate - a peer
        comparison, not a historical self-baseline at all.
      - RTP-01/02/03 and BAB-01/02 compare against a fixed placeholder threshold/band
        (pd.Series(96/90/100/1.0, ...) in the engine), not an average of anything.
      - NGG-01 compares trailing-7d to the PRIOR 7-day window (week-over-week), close to a
        rolling average in spirit but framed as "the week before," not "usually."
      - GEO-01 compares against a fixed placeholder band (pd.Series(10.0, ...)), same shape as
        RTP/BAB, not a historical average - shipped without this dispatch entry in the 2026-11-10
        Tier 3 build (this function wasn't touched for the new rules), caught the next day by
        the 2026-11-11 general audit, before any real alert had a chance to render it wrong.
      - DEP-02 compares a trailing-7-day SUM to the PRIOR trailing-7-day sum (a weekly total,
        not a daily figure) - calling it "a 7-day average" would read as a typical day's count
        when it's actually last week's whole-week total. Same miss/catch timing as GEO-01.
    Calling all of these "a 7-day average" (as an earlier version of this function did, after
    the user picked that phrasing from 3 offered options) would be factually wrong for seven of
    the ~113 rule IDs - this is the running list of catches, including the two the 2026-11-11
    audit found before either had actually fired a real alert yet."""
    if rid == 'STD-01':
        return "last week's rate"
    if rid == 'STD-02':
        return "the market rate"
    if rid.startswith('RTP'):
        return "the expected level"
    if rid.startswith('BAB'):
        return "the normal level"
    if rid == 'NGG-01':
        return "the prior week's figure"
    if rid == 'GEO-01':
        return "the expected level"
    if rid == 'DEP-02':
        return "last week's total"
    return "a 7-day average"


def _is_percentage_metric(rid):
    """FIX (2026-11-09, AUDIT_HISTORY.md finding #36): these rule families' value/baseline are
    already rate/percentage numbers (STD-01/02 store round(rate*100,1); C2R-*/R2F-*/ENG-01/02/
    FBN-01/BON-02 go through ratio_sum_baseline(), which is numer/denom*100; RTP is wins/bets*100)
    but the Alert: line never said so - "...vs. a 7-day average of 0.06" and "...vs. the market
    rate of 31.3" both read ambiguously (a count? a ratio?) on first glance. BAB-*/NGR-01/NGG-01
    are deliberately excluded here: BAB's ratio is "x times the 1.0 abuse threshold," not a
    percentage of anything, and NGG-01's own 0-1 fraction gets a dedicated rescale in
    _alert_phrase() instead (it isn't already *100 in the engine the way these are).

    FIX (2026-11-11): `GEO-01` (share*100 vs. a fixed placeholder band, same shape as RTP) was
    added to `_baseline_label()` above as part of finding #118's fix, and AUDIT_HISTORY.md item
    110 claimed 'GEO' was added here too - it wasn't; this call was never actually made in that
    session, only drafted in the item's own write-up. Confirmed by direct read, not assumed.
    Fixing it now (found while implementing a separate, unrelated set of fixes) rather than
    leaving the documentation and the code disagreeing with each other."""
    prefix = rid.partition('-')[0]
    return prefix in ('C2R', 'R2F', 'STD', 'RTP', 'ENG', 'FBN', 'GEO') or rid == 'BON-02'


def _alert_phrase(row):
    """"Alert: x% drop (y today vs. {baseline_label} of z)" - x = magnitude, direction word
    (drop/rise), y = today's value, z = the baseline, {baseline_label} = what that baseline
    actually represents for this rule (see _baseline_label()). FIX history (2026-11-05): v1
    used the user's own exact template wording verbatim, "(y compared to z then)" - user
    reported "then" was unclear. v2 tried "(y today vs. z usually)" - user reported "usually"
    was still abstract. v3 named it "a 7-day average" universally - factually wrong for
    STD-01/02/RTP/BAB/NGG-01 (see _baseline_label()), caught before shipping. This (v4) is
    rule-aware. pct is occasionally None (e.g. FGR-01 when its own baseline is <= 0) - handled
    without fabricating a percentage.

    FIX (2026-11-06, Critical finding from the real-data audit): for BAB-01/02 and RTP-01/02/03,
    `row['pct']` is NOT a percent-change at all - health_check_engine.py's run_product() passes
    the same raw ratio/value as both `pct_series` and `value_series` for these two families
    (confirmed by reading every BAB/RTP emit() call), so `pct` is just the ratio itself. This
    rendered a real, live bonus-abuse alert as "Alert: 2% rise (1.5 today vs. the normal level
    of 1)" when the partner was actually 50% over the threshold - understating a real alert's
    severity. Also would have shown every low-RTP alert as "rise" (RTP's raw pct is always
    positive) regardless of whether RTP was actually too low or too high. Recompute the real
    percentage from value/baseline directly for these two families instead of trusting the
    engine's pct field."""
    pct = row['pct']
    rid = row['rule_id']
    raw_value, raw_baseline = row['value'], row['baseline']
    # FIX (2026-11-09, finding #36): NGG-01 is the one percentage-shaped metric the engine
    # stores as a raw 0-1 fraction (0.328) rather than already *100 - rescale for display only,
    # same number STD-01/02 and the ratio rules already show, just computed differently upstream.
    if rid == 'NGG-01':
        if raw_value is not None:
            raw_value = raw_value * 100
        if raw_baseline is not None:
            raw_baseline = raw_baseline * 100
    value = _fmt_num(raw_value)
    baseline = _fmt_num(raw_baseline)
    pct_sign = '%' if _is_percentage_metric(rid) or rid == 'NGG-01' else ''
    label = _baseline_label(rid)
    if rid in ('CLK-13', 'REG-13', 'FTD-13'):
        # FIX (2026-11-11, user-reported: a real alert read "67% drop (4 today vs. a 7-day
        # average of 6)" and looked like a tiny, meaningless move on 6 registrations/day).
        # These three rules fire on BREADTH - what share of a partner's own qualifying
        # tracking links individually collapsed - so health_check_engine.py's own emit() call
        # (run_partner_guid_lp_volume(), "FIX 9") deliberately passes `value`/`baseline` as
        # COUNTS OF TRACKING LINKS (how many dropped, how many qualified that day), not the
        # partner's own clicks/regs/FTDs at all. The generic drop/rise template below has no
        # way to know that and rendered it as if it were a volume comparison - factually
        # wrong, not just unclear, the same bug class as findings #33/#36 (a rule family whose
        # value/baseline don't mean what the generic template assumes).
        n_dropped, n_eligible = raw_value, raw_baseline
        # FIX (2026-11-12): was `if pct is not None` only - pct is NaN here (not None) whenever
        # masked_share()'s own n_eligible is NaN, same gap as the main nan% fix below.
        share_text = f"{abs(pct):.0f}%" if pct is not None and not pd_isna(pct) else "most"
        return f"{int(n_dropped)} of {int(n_eligible)} qualifying tracking links dropped (share {share_text})"
    if rid in ('R2F-15', 'C2R-15'):
        # FIX (2026-11-09, implementing the zero-FTD/zero-reg design proposal, AUDIT_HISTORY.md
        # finding #2): this rule family is an absence ("0 for N days despite real traffic"), not
        # a percent change vs. a baseline - the generic drop/rise template below genuinely
        # doesn't fit (there is no meaningful baseline to compare against; the whole point is
        # that it's zero). health_check_engine.py's run_zero_conversion() repurposes the
        # 'baseline' column to carry today's clicks (display context) instead of a historical
        # average for this rule family specifically - see that function's docstring.
        # FIX (2026-11-09, grammar bug caught in this same optimization pass): "a single
        # deposits"/"a single signups" is wrong - "deposits"/"signups" fits the "0 {X} for N
        # days" count phrase (plural), but "a single" needs the singular noun.
        metric_word = 'deposits' if rid == 'R2F-15' else 'signups'
        metric_singular = 'deposit' if rid == 'R2F-15' else 'signup'
        days = row.get('open_days')
        days_text = f"{int(days)} straight" if days else "multiple"
        clicks_text = f"{baseline} clicks/day" if raw_baseline is not None else "real clicks"
        if row['severity'] == 'CRITICAL':
            tail = f"this partner has never converted a single {metric_singular} in the past {BUFFER_DAYS} days of real volume"
        else:
            tail = f"this partner has not converted a single {metric_singular} yet in the current lookback window"
        return f"0 {metric_word} for {days_text} days despite real traffic ({clicks_text}) - {tail}"
    if rid.partition('-')[0] in ('BAB', 'RTP') and raw_value is not None and raw_baseline:
        pct = (raw_value - raw_baseline) / raw_baseline * 100
    # FIX (2026-11-12, user-reported real example: GGR-01 on Project 6, 2026-11-11, value=
    # -5,617.19, baseline=-671.89, rendered "736% rise" for what is actually a much bigger GGR
    # loss): `pct is None` never caught pct being NaN (a different thing in Python/pandas) -
    # health_check_engine.py's pctchg() returns NaN, not None, whenever baseline is exactly 0
    # (`base.replace(0, np.nan)`), so this branch was skipped and NaN flowed into the comparisons
    # below. Second real example same day: FGR-01 baseline=0.00 rendered "nan% rise".
    if pct is None or pd_isna(pct):
        # Grammar note: _baseline_label()'s phrases are built to follow "vs." (e.g. "vs. the
        # market rate of 31.3"), not "no" (e.g. "no a 7-day average" is wrong, as would be
        # "no the market rate") - so this rare branch (pct is None/NaN, mainly FGR-01) uses its
        # own fixed, always-correct phrase instead of splicing the label in.
        return f"{value}{pct_sign} today (no prior figure to compare against)"
    # FIX (2026-11-12, same user report): direction was derived from pct's SIGN, but
    # pct=(value-baseline)/baseline flips sign whenever baseline itself is negative (GGR/NGR/
    # FGR/NGG-01 can all go negative) - direction now compares value to baseline directly, which
    # tracks "better/worse" correctly regardless of either number's own sign; abs(pct) below is
    # unchanged (still just the magnitude). Same bug independently confirmed live the same day on
    # NGG-01 (ratio improved -58% -> +17%, baseline negative - the old sign-based logic would
    # have called an improvement a "drop").
    direction = "drop" if raw_value < raw_baseline else "rise"
    return f"{abs(pct):.0f}% {direction} ({value}{pct_sign} today vs. {label} of {baseline}{pct_sign})"


def _alert_line(row, entity_text=None):
    """FIX (2026-11-10): user feedback ("spaces, new lines still messy") after several rounds
    of wording/symbol cleanup on the previous labeled-block format (Severity:/Partner:/Link:/
    Project:/Countries:/Value:/Alert:, 5-8 lines per alert) - the remaining mess was structural,
    not wording: every field was repeated on every single alert even when it was identical to
    the one above it. Partner/Project/Countries now live once on the enclosing group header (see
    _partner_header()/_project_header() and build_telegram_message() below); this is the one
    line left per alert, down to severity + metric + (link, if any) + the existing, unchanged
    _alert_phrase() wording - no rewording of any metric math, just where it sits.

    FIX (2026-11-12, user-directed): the user took this exact output, hand-edited a full day's
    worth of it in a report format they control, and said to match THAT structure in the
    Telegram messages, "no more or less" - confirmed consistently across all 19 real alerts
    they edited: severity as a colored circle + severity word + "| STATUS" (not a bracketed
    tag), an em dash before the description, and status reworded from "DAY N"/"ONGOING" to
    "RECURRING (day N)"/"RECURRING" (see _status_tag()) - matching the exact vocabulary
    (NEW/ESCALATED/RECURRING) their own spec named."""
    emoji = '\U0001F534' if row['severity'] == 'CRITICAL' else '\U0001F7E0'  # red / orange circle
    status = _status_tag(row)
    metric = _esc(_metric_label(row['metric']))
    phrase = _esc(_alert_phrase(row))
    link = entity_text if entity_text is not None else (
        _esc(entity_label(row)) if row['level'] in ('GUID', 'Landing Page') else None)
    core = f"{metric} on {link}: {phrase}" if link else f"{metric}: {phrase}"
    return f"{emoji} {row['severity']} | {status} — {core}"


def _notes_block(row, skip_correlation=False):
    """FIX (2026-11-12, user feedback: "more brief, with structure and bullets"): each note was
    briefly its own bulleted sub-line - since superseded (see the next FIX note below) by a
    single plain "Impact:" line per the user's own hand-edited report format.

    FIX (2026-11-12, user-directed, same hand-edited report as _alert_line()): every alert in
    the user's edit carries AT MOST one plain "Impact: ..." line, never a bulleted list of
    several - whatever applicable notes exist are merged into that one sentence, and the
    bullet character is dropped entirely (matches their edit exactly: no "•" anywhere)."""
    notes = assemble_notes(row, skip_correlation)
    if not notes:
        return []
    return [f"Impact: {' '.join(_esc(n) for n in notes)}"]


def render_single(row):
    """One alert as a single line, with any enrichment notes as indented sub-lines underneath.
    See _alert_line()'s own note for why this replaced the former 5-8-line labeled block."""
    return [_alert_line(row)] + _notes_block(row)


def render_group(rule_id, severity, grp):
    """Same-rule_id/same-severity/same-project GUID or LP alerts under one partner, collapsed
    into one line instead of N near-duplicate ones (unchanged rationale - 2026-11-05 design
    proposal S3.2/S4 rule 3, real evidence: guid_28201/guid_28253's CLK-16 tracked within ~1% of
    each other on every one of 8 sample occasions). Every entity is still named (not just
    counted) - the compact one-line format leaves room for that without the clutter the old
    multi-line block had."""
    first = grp.iloc[0]
    entity_list = ' & '.join(_esc(entity_label(r)) for _, r in grp.iterrows())
    return [_alert_line(first, entity_text=entity_list)] + _notes_block(first)


def render_cluster(grp):
    """A `linked_rule_ids` cluster (health_check_engine.py's correlate_alerts(), AUDIT_HISTORY.md
    finding #48) collapsed into ONE alert block instead of N separate ones each carrying their
    own "Also linked to..." Note: line - the merge design worked out in
    reports/design_outlier_and_merge_analysis_2026-11-09.md section 4. Never blends the members'
    different units (dollars, a ratio, a rate) into one number - that would fabricate a figure
    with no real meaning (GGR dollars + RTP percentage + Admin Fees dollars have no common unit).
    Keeps each metric's own real unit on its own indented sub-line, reusing _alert_phrase()
    verbatim per member. Severity is the HIGHEST among the linked members, so a CRITICAL member
    is never visually buried under two WARNINGs - that member's own sub-line is tagged
    [CRITICAL] so it's still identifiable within the block.

    FIX (2026-11-10): dropped the Severity:/Partner:/Project:/Countries: labels a Project-level
    cluster used to repeat on its own - these clusters render inside the "Not partner-specific"
    section's per-project header now (build_telegram_message()), so the headline below only adds
    what that header doesn't already say: which partner actually explains the cluster.

    FIX (2026-11-12, user feedback: "more brief, with structure and bullets" / "correlation is
    used to group alerts, outliers are used to show that a problem is not real"): replaced the
    one-sentence headline ("One player under X is behind all N metrics, driving Y% of today's Z
    move alone:") with a short header naming WHAT's grouped and WHY (metric names + "(grouped)",
    the user's own word for what correlation does here), a [STATUS] tag matching _alert_line()'s,
    then one short bullet per thing the AM needs - the outlier check FIRST (explicitly "likely
    not platform-wide", not a bare percentage - this is the "is the problem real" check the user
    described), then each member metric's own line, same _alert_phrase() wording as before."""
    grp = grp.copy()
    grp['_sr'] = grp['severity'].map(SEV_RANK)
    grp = grp.sort_values('_sr', ascending=False)
    first = grp.iloc[0]
    worst_sev = 'CRITICAL' if (grp['severity'] == 'CRITICAL').any() else 'WARNING'
    anchor = grp.loc[grp['linked_dominant_share'].abs().idxmax()]
    anchor_share_text = _share_text(anchor['linked_dominant_share'])
    anchor_metric = _metric_label(anchor['metric'])

    # A Project-level row (every case correlate_alerts() currently covers) never gets
    # `partner_id` set by attribute_alerts() - it stays 'not-partner-specific' there. Using
    # `linked_dominant_partner` instead is the whole secondary value this mechanism provides:
    # the first time any Project-level alert gets named-partner attribution at all.
    dom_partner = first.get('linked_dominant_partner')
    pid = first.get('partner_id')
    if dom_partner is not None and not pd_isna(dom_partner):
        attributed_partner = int(dom_partner)
    elif pid is not None and not pd_isna(pid):
        attributed_partner = int(pid)
    else:
        attributed_partner = None
    partner_text = partner_label(attributed_partner) or "one partner"
    emoji = '\U0001F534' if worst_sev == 'CRITICAL' else '\U0001F7E0'
    status = _status_tag(first)
    metric_names = ', '.join(dict.fromkeys(_metric_label(m) for m in grp['metric']))

    # FIX (2026-11-12, user-directed, same hand-edited report as _alert_line()): matches their
    # edit of this exact cluster shape (GGR+RTP, FTD GGR+GGR+RTP) exactly - a merged emoji/pipe
    # header ending "<metrics> incident:", then each member's own value line with NO bullet, NO
    # per-member [CRITICAL] tag (their edit dropped it - severity is already the header's own),
    # then ONE "Partner contribution:" sentence naming the partner and listing EACH member's own
    # share (not just the anchor's) - "accounts for X% of the Y move and Z% of the W move" is
    # their exact wording, generalized here to N members instead of hardcoding 2.
    lines = [f"{emoji} {worst_sev} | {status} — {_esc(metric_names)} incident:"]
    for _, row in grp.iterrows():
        lines.append(f"{_esc(_metric_label(row['metric']))}: {_esc(_alert_phrase(row))}")
    shares = []
    for _, row in grp.iterrows():
        s = row.get('linked_dominant_share')
        if s is not None and not pd_isna(s):
            shares.append(f"{_share_text(s)} of the {_esc(_metric_label(row['metric']))} move")
    if shares:
        shares_text = ' and '.join(shares) if len(shares) <= 2 else ', '.join(shares[:-1]) + f", and {shares[-1]}"
        lines.append(f"Partner contribution: One customer under {_esc(partner_text)} accounts for {shares_text}.")
    # FIX (2026-11-10, AUDIT_HISTORY.md finding #83): previously this block attached none of the
    # 5 note functions, so a merged cluster could never show chronic/REMINDER/ESCALATION status
    # even though nothing stops a clustered row from also being a 3-week-old REMINDER. Uses the
    # highest-severity member (`first`, already selected by the severity sort above) as the
    # representative row - same convention render_group() already uses for its own notes.
    # Skips _correlation_note() (skip_correlation=True) since the "Partner contribution:" line
    # just above already states this cluster's own attribution more precisely (per-member
    # shares, not just the anchor's) - see assemble_notes()'s own note on why that function
    # would otherwise just repeat or conflict with it.
    extra_notes = _notes_block(first, skip_correlation=True)
    if extra_notes:
        lines.append(extra_notes[0].replace('Impact: ', 'Partner contribution: ', 1))
    return lines


def render_rows(rows):
    """Render a severity-ordered block of alert rows to a flat list of lines, one blank line
    between each alert's own labeled block, collapsing same-rule_id/same-severity/same-project
    GUID or LP rows into one combined block along the way (design proposal S4 rule 3), and
    collapsing a correlate_alerts() `linked_rule_ids` cluster into one combined block too (FIX
    2026-11-09, see render_cluster()). `rows.groupby(..., sort=False)` preserves the caller's
    existing severity ordering: groups are emitted in first-row-encountered order, not re-sorted.
    FIX (2026-11-05 "still messy" pass): the groupby key used to omit project_id, so two GUID
    alerts for the same rule_id/severity from DIFFERENT projects could be merged into one
    combined line under whichever project the first row happened to carry - confirmed on the
    real 2026-10-30 data (a Project 3 and a Project 6 GUID both firing CLK-16/WARNING got
    merged and mislabeled under Project 3). project_id is part of the grouping key."""
    def group_key(row):
        linked = row.get('linked_rule_ids')
        if linked and not pd_isna(linked):
            # A cluster is at most one per (project, dominant partner) on a given day - see
            # correlate_alerts()'s own docstring (only one anchor computed per project/day).
            return ('cluster', row.get('project_id'), row.get('linked_dominant_partner'))
        return ('single', row['rule_id'], row['severity'], row.get('project_id'))

    rows = rows.copy()
    rows['_group_key'] = rows.apply(group_key, axis=1)

    blocks = []
    for key, grp in rows.groupby('_group_key', sort=False):
        if key[0] == 'cluster' and len(grp) > 1:
            blocks.append(render_cluster(grp))
        elif len(grp) > 1 and grp['level'].iloc[0] in ('GUID', 'Landing Page'):
            blocks.append(render_group(grp['rule_id'].iloc[0], grp['severity'].iloc[0], grp))
        else:
            for _, row in grp.iterrows():
                blocks.append(render_single(row))
    out_lines = []
    for i, block in enumerate(blocks):
        if i > 0:
            out_lines.append("")
        out_lines.extend(block)
    return out_lines


def attribute_alerts(alerts, promo_maps, stats_maps):
    """Resolve each alert row to the partner it actually belongs to, so GUID/LP-level
    alerts can be grouped under the same partner as that partner's own Partner-level
    alerts (per user request, 2026-11-05 chat) rather than listed as unrelated rows.
      - Partner level: entity IS the partner_id.
      - GUID level: entity is actually a promo_id (see health_check_engine.py's
        guid_{promo_id} naming) - look it up directly in that project's promo_map.
      - Landing Page level: a landing page can be shared by several partners (confirmed
        2026-11-05: lp_179 alone has 70+ promo_ids across unrelated partners), so there is
        no single correct owner - attribute to whichever partner's OWN clicks on that LP
        account for the largest share of the LP's own move on the alert date, and record
        that share so a shaky (<~60%) attribution can be flagged rather than presented as
        fact.
      - Project / Project-Traffic level: genuinely not partner-specific - partner_id stays
        None and these go in their own section of the message instead of under any AM.
    Adds columns: partner_id, attribution (how it was resolved, incl. LP share for review).

    FIX (2026-11-10, closes the still-open half of AUDIT_HISTORY.md finding #26): the Landing
    Page share used to be computed from the ENTIRE 35-day pulled window's click totals, not
    scoped to the alert's own date - user feedback: a caveat explaining the resulting number is
    "probably right" doesn't help when the number itself is stale; the computation should be
    fixed, not hedged around. Now scoped to the 7 days ending on the alert's own date (the same
    trailing-window convention every baseline in this codebase already uses), so the share
    reflects who was actually driving that landing page's traffic around the time of the alert,
    not a blend that can be weeks out of date by the time it's read. Confidence tiers/wording
    unchanged below - genuinely split traffic still gets a caveat, it's just now a caveat about
    real, current ambiguity instead of an artifact of the old window.

    FIX (2026-11-12, user question - "clicks are connected to promo_ids, why can't the system
    verify the owner?"): the share above used to be each partner's share of the LP's total
    click VOLUME over the trailing window - i.e. "who normally uses this LP most", not "whose
    clicks actually moved on the alert date". Those are different questions: a partner that's
    57% of an LP's traffic but held perfectly flat can still get named as the cause of a drop
    actually driven by the other two partners' 43%. The data to tell them apart was already
    being pulled (stats_maps[pid] has partner_id per promo_id, which is exactly how the old
    volume share was computed) - it just wasn't being used to isolate each partner's OWN
    day-over-baseline move. Now attributes to whichever partner's own click delta (today vs.
    that partner's own trailing-7d baseline on this LP, same shift(1).rolling(7) convention as
    health_check_engine.py's baseline()) accounts for the largest share of the LP's total
    delta - i.e. whoever actually moved, not whoever is normally biggest. Falls back to the
    old volume-share guess (clearly labeled as such) only in the degenerate case where no
    partner's own move lines up with the LP's overall direction."""
    import pandas as pd

    def resolve(row):
        level, entity, pid, d = row['level'], row['entity'], row['project_id'], row['date']
        if level == 'Partner':
            return int(entity.split('_')[1]), 'direct', None, None
        if level == 'GUID':
            promo_id = int(entity.split('_')[1])
            m = promo_maps[pid][promo_maps[pid]['promo_id'] == promo_id]
            if m.empty:
                return None, 'guid-unmapped', None, None
            # FIX (2026-11-05 AM-friendly Telegram design): surface traffic_type on the alert
            # line - it's already a promo_map column pulled for this same lookup, just wasn't
            # exposed before (design proposal S6.1, "already available, zero new query").
            # finding #111 (suppression depth, Tier 2): also surface landing_id, same zero-new-
            # query promo_map lookup - lets apply_suppression() check a GUID's own Landing Page
            # alert (not just its Partner alert) as an alternate suppressing parent.
            lid = m.iloc[0]['landing_id']
            return (int(m.iloc[0]['partner_id']), 'guid-direct', m.iloc[0]['traffic_type'],
                    int(lid) if pd.notna(lid) else None)
        if level == 'Landing Page':
            lid = int(entity.split('_')[1])
            lp_stats = stats_maps[pid]
            # 8 days back (not 7) so that "prior" below - the days strictly before the alert
            # date - spans a full trailing 7 days, matching baseline()'s own
            # shift(1).rolling(7) convention exactly.
            window = lp_stats[(lp_stats['landing_id'] == lid) &
                               (lp_stats['date'] > d - pd.Timedelta(days=8)) & (lp_stats['date'] <= d)]
            if window.empty or window['clicks'].sum() <= 0:
                return None, 'lp-no-traffic', None, None
            daily = window.groupby(['date', 'partner_id'])['clicks'].sum().unstack(fill_value=0)
            partners = daily.columns
            n_partners = len(partners)
            by_partner_total = daily.sum().sort_values(ascending=False)
            if n_partners == 1:
                return int(partners[0]), 'lp-single-partner', None, None

            value = daily.loc[[d]].sum() if d in daily.index else pd.Series(0.0, index=partners)
            prior = daily[daily.index < d]
            baseline_ = prior.tail(7).mean() if not prior.empty else pd.Series(0.0, index=partners)
            move = value - baseline_  # >0: this partner's own clicks rose; <0: fell
            total_move = move.sum()

            # Attribute to whichever partner's own move explains the largest share of the
            # LP's total move, not whoever is normally biggest (see FIX 2026-11-12 above).
            same_direction = move[move * total_move > 0] if abs(total_move) > 1e-9 else move.iloc[0:0]
            if not same_direction.empty:
                top_partner = same_direction.abs().idxmax()
                top_share = abs(same_direction[top_partner] / total_move) * 100
                return (int(top_partner),
                        f'lp-dominant({top_share:.0f}% of {n_partners} partners by own click move, trailing 7d)',
                        None, None)

            # Degenerate case - no net LP-level move to attribute, or no single partner's own
            # move lines up with the LP's overall direction (e.g. two partners' opposite moves
            # happened to cancel out). Fall back to the old "biggest baseline user" guess,
            # explicitly labeled as unresolved rather than presented as a confirmed cause.
            share = by_partner_total.iloc[0] / by_partner_total.sum() * 100
            return (int(by_partner_total.index[0]),
                    f'lp-dominant({share:.0f}% of {n_partners} partners, trailing 7d, no attributable move)',
                    None, None)
        return None, 'not-partner-specific', None, None

    out = alerts.copy()
    resolved = out.apply(resolve, axis=1)
    out['partner_id'] = resolved.apply(lambda x: x[0])
    out['attribution'] = resolved.apply(lambda x: x[1])
    out['traffic_type'] = resolved.apply(lambda x: x[2])
    out['landing_id'] = resolved.apply(lambda x: x[3])
    return out


def fetch_account_managers(partner_ids):
    """partner_tags/partner_tags_ref double as the account-manager roster (confirmed
    2026-11-05: titles like Fotini/Willard/jackie/Gerakanakis are AM names, not generic
    labels). A partner can have >1 AM tag (seen on partner 10419: 'Gerakanakis, Willard') -
    join all of them.

    FIX (2026-11-12): this used to say "tag_id 1/37 are the only non-AM tags" - that was only
    ever confirmed against 4 sample titles, not checked exhaustively, and turned out wrong:
    tag_id 33/34 ("Vicky"/"Vicky2") are a 5-partner migration-sink bucket, not real AM names
    (see pull_project()'s own comment, health_check_engine.py, for the evidence). All non-AM
    tag_ids found so far (1, 33, 34, 37, plus 41="Test") are excluded upstream by pull_project,
    so none of them should appear here - if a new non-AM-looking title ever shows up in this
    function's output, that's the same gap recurring, not a new AM."""
    import db
    pids = [int(p) for p in partner_ids if p is not None and not (isinstance(p, float) and pd_isna(p))]
    if not pids:
        return {}
    ids_str = ','.join(str(p) for p in pids)
    tags = db.replica_run_query(f"""
        SELECT ptr.partner_id, pt.title
        FROM partner_tags_ref ptr JOIN partner_tags pt ON pt.id = ptr.tag_id
        WHERE ptr.partner_id IN ({ids_str})
    """)
    if tags.empty:
        return {}
    return tags.groupby('partner_id')['title'].apply(lambda x: ', '.join(sorted(set(x)))).to_dict()


# finding #25 (user request, 2026-11-11): "I want the partner name and id in the alerts" - a
# human-readable partner name, not just the raw partner_N id. Confirmed precedent for this exact
# query elsewhere in the repo (customers/tqa_model/build_partner_september_cohort_report.py and
# screen_spec/cpa_investigation/cpa_enrich.py both run this against the same MySQL replica
# family) - nickname if set, else username, else no name at all (id-only, same as today).
_PARTNER_NAMES = {}  # populated per run by build_telegram_message() below - see partner_label()

# FIX (2026-11-12, health-check-auditor finding, general audit 2026-11-12): same mechanism as
# _PARTNER_NAMES above - populated once per run by build_telegram_message(), read deep in the
# call stack by _entity_correlation_note() without threading a new parameter through render_
# single()/render_group()/render_cluster()/_notes_block()/assemble_notes(). Maps (entity, date)
# -> the set of rule_ids actually present in today's final, post-suppression, actionable alert
# set - see _entity_correlation_note() for why this is needed.
_ENTITY_DATE_OPEN = {}


def fetch_partner_names(partner_ids):
    import db
    pids = [int(p) for p in partner_ids if p is not None and not (isinstance(p, float) and pd_isna(p))]
    if not pids:
        return {}
    ids_str = ','.join(str(p) for p in pids)
    names = db.replica_run_query(f"SELECT id AS partner_id, nickname, username FROM _users WHERE id IN ({ids_str})")
    if names.empty:
        return {}
    names['label'] = names['nickname'].where(names['nickname'].notna() & (names['nickname'] != ''), names['username'])
    return dict(zip(names['partner_id'], names['label']))


def partner_label(pid):
    """"partner_124 (Nickname)" when a name is resolvable (see _PARTNER_NAMES above), else the
    bare "partner_124" this codebase has always shown. `pid` may be an int, numpy int, or a
    NaN-safe float (every call site here already guards None/NaN upstream in the same style).

    FIX (2026-11-11, user report "the messages do not show the name"): the name was being
    dropped into Telegram's HTML parse_mode unescaped - every other call site here runs
    user/DB-sourced text through _esc() first, this one didn't. A nickname/username containing
    '<', '>' or '&' (confirmed real values in `_users` can be arbitrary partner-chosen strings,
    not controlled input) would break Telegram's HTML parsing for that whole line, which would
    look exactly like "the name doesn't show" even though the lookup itself succeeded."""
    if pid is None or pd_isna(pid):
        return None
    pid = int(pid)
    name = _PARTNER_NAMES.get(pid)
    return f"partner_{pid} ({_esc(name)})" if name else f"partner_{pid}"


def pd_isna(x):
    import pandas as pd
    return pd.isna(x)


def fetch_p4_country_breakdown(eval_date, top_n=3):
    """Project 4 (Brazino777.io) FTD count by country for one day, for DISPLAY context only on
    its Telegram alerts - not a new alerting rule/threshold, and not wired into RuleEngine at
    all (2026-11-05, "include country for project 4" request). Deliberately isolated from
    pull_project()/the rest of the pipeline: a new per-country rule family would need the same
    kind of threshold validation the existing traffic-type rules got against real history, which
    this quick addition does not attempt - it only tells the AM which market(s) a project-4
    alert is concentrated in, using data already in projects_statistics (just not previously
    selected). Returns "" if there's nothing to show (e.g. zero FTDs that day)."""
    import db
    countries = ','.join(f"'{c}'" for c in P4_COUNTRIES)
    df = db.replica_run_query(f"""
        SELECT country, SUM(is_first_depositor) AS ftds
        FROM projects_statistics
        WHERE project_id = 4 AND date = '{eval_date.date()}' AND country IN ({countries})
        GROUP BY country
        HAVING ftds > 0
        ORDER BY ftds DESC
    """)
    if df.empty:
        return ""
    top = df.head(top_n)
    return ', '.join(f"{row['country']} {int(row['ftds'])}" for _, row in top.iterrows())


TELEGRAM_CHAR_BUDGET = 4000  # safety margin under Telegram's real 4096-char sendMessage cap


def _partner_header(pid, rows):
    """FIX (2026-11-10): one header line for everything a single partner has today, under one
    AM - previously every alert repeated its own "Partner: partner_X" line even when the
    partner above it was identical (confirmed on the real 2026-11-09 message: partner_1441
    appeared twice in a row). A partner almost always has one project per day, but a chronic
    cross-project partner (e.g. partner_9552, AUDIT_HISTORY.md finding #11) can have rows from
    more than one - names every distinct project rather than silently picking the first."""
    projects = rows[['project_id', 'project_label']].drop_duplicates()
    if len(projects) == 1:
        label = _esc(projects.iloc[0]['project_label'])
    else:
        label = ' & '.join(_esc(p) for p in sorted(projects['project_label'].unique()))
    p4_rows = rows[rows['project_id'] == 4]
    p4bd = p4_rows['p4_country_breakdown'].iloc[0] if not p4_rows.empty else None
    country = f" ({_esc(p4bd)})" if p4bd else ""
    count = f" - {len(rows)} alerts" if len(rows) > 1 else ""
    return f"<b>{partner_label(pid)}</b> - {label}{country}{count}"


def _project_header(rows):
    """Same role as _partner_header(), for the "Not partner-specific" section's per-project
    grouping - see build_telegram_message()."""
    first = rows.iloc[0]
    label = _esc(_project_label(first))
    p4bd = first.get('p4_country_breakdown')
    country = f" ({_esc(p4bd)})" if first['project_id'] == 4 and p4bd else ""
    count = f" - {len(rows)} alerts" if len(rows) > 1 else ""
    return f"<b>{label}</b>{country}{count}"


def build_telegram_message(eval_date, alerts, p4_country_breakdown=""):
    """AM -> partner -> alerts, then a 'not partner-specific' section (grouped by project) for
    Project/Project-Traffic-level alerts. Returns a LIST of one or more message strings (never
    a single str) - see the splitting FIX below.

    FIX (2026-11-10): user feedback after the labeled-block format ("Severity:"/"Partner:"/
    "Link:"/... per alert) still read as messy - the repeated per-alert fields were the actual
    problem, not the wording. Partner (or project, in the not-partner-specific section) is now
    a single header per group (_partner_header()/_project_header() above), with each of that
    group's alerts rendered as one line by render_single()/render_group()/render_cluster().
    This also fixes the real spacing bug those functions' blank-line logic couldn't reach: see
    _pack_telegram_messages()'s own FIX note for why two different partners under the same AM
    used to run together with no separator at all.

    FIX (2026-11-10, AUDIT_HISTORY.md finding #82): `alerts` must already carry `partner_id`/
    `attribution`/`traffic_type` and already have suppression applied - main()/run_for_date.py
    now call attribute_alerts() and health_check_engine.py's apply_suppression() per project,
    right after correlation and before either the Excel report or this function, so the Excel
    "_all_open" sheet and this Telegram message can no longer disagree on what's suppressed.
    This function no longer takes `promo_maps`/`stats_maps` and no longer calls
    attribute_alerts()/apply_lp_suppression() itself - callers do that earlier now.

    FIX (2026-11-05, per the user's exact template: "Account Manager name / Partner: x /
    Value: x / Alert: x% drop (y compared to z then)"): each alert now renders as a labeled
    multi-line block via render_single()/render_group() (see those functions) instead of one
    flowing sentence - the user said the sentence format was "still not clear" and gave this
    structure to follow instead. `p4_country_breakdown` is a pre-computed string (see
    fetch_p4_country_breakdown(), called from main()/run_for_date.py, not from here - this
    function stays DB-free so it's testable against synthetic data) attached to every project-4
    alert as display context, not a new alerting rule.

    FIX (2026-11-09, user-reported): a real day's message hit the old silent-but-marked
    4000-char cap and got truncated - confirmed not a one-off: adding R2F-15/C2R-15 (finding #2)
    and finding #23's periodic REMINDER events means a normal day can now legitimately carry
    more actionable alerts than one Telegram message holds (reconstructed from the real
    2026-11-08 report: 23 actionable alerts, ~3800 chars with incomplete attribution data -
    real attribution would be larger). Cutting content and telling the AM to go check the Excel
    report defeats the point of a daily digest. Now splits into multiple Telegram messages
    instead - every AM/partner block still reaches the feed, just possibly in message 2 or 3 of
    the day, each one under the Telegram length cap. Splits only at AM or partner-block
    boundaries (the natural seams already in the structure) - a single alert's own rendered
    block (render_single()/render_group()'s output) is never split across two messages. A
    single partner having >4000 chars of alerts in one day (would need ~15-20+ individual
    alerts for ONE partner on ONE day) is not handled with further sub-splitting - flagged as a
    known, not-expected-in-practice limit rather than over-engineered for a case never seen in
    real data."""
    if alerts.empty:
        return [f"Daily health check - {eval_date.date()}\n\nNo actionable alerts today."]

    attributed = alerts.copy()
    attributed['p4_country_breakdown'] = p4_country_breakdown
    # FIX (2026-11-12, user feedback): finding #11's lightweight mitigation (a per-alert
    # "Also flagged in Brazino777.io" note, via `_other_projects()`/`other_project_labels`,
    # formerly computed here) REMOVED - `_partner_header()` below already names every project a
    # partner has an alert in today (its own "Brazino777.ar & Brazino777.io" join), so the
    # per-alert note was always redundant with it, never adding information the AM didn't
    # already have from the header. `attribute_alerts()` groups a partner's rows by partner_id
    # regardless of project, so this is structurally guaranteed, not a one-off observation.

    # FIX (2026-11-12, health-check-auditor finding): see _ENTITY_DATE_OPEN's own comment and
    # _entity_correlation_note()'s docstring - must be set from `attributed` (final, post-
    # suppression, actionable rows only), not the engine's raw res_df, and must happen before
    # any rendering below reads it.
    global _ENTITY_DATE_OPEN
    _ENTITY_DATE_OPEN = attributed.groupby(['entity', 'date'])['rule_id'].apply(set).to_dict()

    # finding #25: gather every partner_id referenced ANYWHERE in today's alerts - not just the
    # row's own `partner_id` (Partner/GUID/LP-level rows), but also every correlation
    # mechanism's own dominant-partner column, since those name a partner on an otherwise
    # "not partner-specific" Project-level row. One query, used by partner_label() everywhere
    # below (entity_label, attribution_tag, the correlation/whale/ratio-hint notes,
    # _partner_header()).
    global _PARTNER_NAMES
    ref_ids = set(attributed['partner_id'].dropna().astype(int))
    for col in ('linked_dominant_partner', 'solo_dominant_partner', 'dominant_partner', 'ratio_hint_partner'):
        if col in attributed.columns:
            ref_ids |= set(attributed[col].dropna().astype(int))
    if 'top3_dominant_partners' in attributed.columns:
        for s in attributed['top3_dominant_partners'].dropna():
            ref_ids |= {int(p) for p in re.findall(r'partner_(\d+)', str(s))}
    _PARTNER_NAMES = fetch_partner_names(ref_ids)

    am_map = fetch_account_managers(attributed['partner_id'].dropna().unique().tolist())

    partner_level = attributed[attributed['partner_id'].notna()].copy()
    partner_level['am'] = partner_level['partner_id'].map(
        lambda p: am_map.get(int(p), 'UNASSIGNED (no AM tag)'))

    # FIX (2026-11-05 audit): was sorting by the raw 'severity' string with ascending=False,
    # which alphabetically puts WARNING (W) above CRITICAL (C) - the opposite of intended
    # priority order. Sort by sev_rank instead so CRITICAL always lists first.
    partner_level['_sev_rank'] = partner_level['severity'].map(SEV_RANK)
    # FIX (2026-11-10, AUDIT_HISTORY.md finding #87): a secondary tie-breaker WITHIN the same
    # severity tier - a WARNING carrying several enrichment notes (correlated, a chronic
    # reminder, a solo-whale flag, ...) now lists above a plain WARNING with none, without ever
    # outranking a genuine CRITICAL (severity stays the primary, untouched key). Low-confidence
    # UX judgment call, not a correctness fix - see the finding's own note.
    partner_level['_enrich_count'] = partner_level.apply(lambda r: len(assemble_notes(r)), axis=1)
    partner_level['_time_rank'] = partner_level['event_type'].map(TIME_RANK).fillna(1)

    # FIX (2026-11-05 Telegram audit): AM blocks and partner sub-blocks used to be ordered
    # alphabetically/by ID, unrelated to urgency - a busy AM/partner could sort after a quiet
    # one. Order both by (worst severity in the block, alert count) descending instead; ties
    # fall back to groupby's own alphabetical/ID order for determinism. This also removes the
    # ASCII case-sort trap on a lowercase AM name (e.g. "jackie") as a side effect, since that
    # AM's block now sorts by its alerts' urgency, not by the literal string "jackie".
    am_stats = partner_level.groupby('am')['_sev_rank'].agg(['max', 'count'])
    am_order = am_stats.sort_values(['max', 'count'], ascending=[False, False]).index.tolist()

    # Build (am_name, partner_lines) units in final display order - NOT joined into one flat
    # list, so _pack_telegram_messages() below can split between any two units without ever
    # splitting what's inside one.
    units = []
    for am in am_order:
        am_rows = partner_level[partner_level['am'] == am]
        partner_stats = am_rows.groupby('partner_id')['_sev_rank'].agg(['max', 'count'])
        partner_order = partner_stats.sort_values(['max', 'count'], ascending=[False, False]).index.tolist()
        for pid in partner_order:
            prows = am_rows[am_rows['partner_id'] == pid].sort_values(['_sev_rank', '_time_rank', '_enrich_count'], ascending=[False, False, False])
            units.append((am, [_partner_header(pid, prows)] + render_rows(prows)))

    # FIX (2026-11-05 Telegram audit): this section had no severity ordering at all (the
    # original severity-sort fix only covered the per-AM/partner block above).
    not_partner = attributed[attributed['partner_id'].isna()].copy()
    if not not_partner.empty:
        not_partner['_sev_rank'] = not_partner['severity'].map(SEV_RANK)
        not_partner['_enrich_count'] = not_partner.apply(lambda r: len(assemble_notes(r)), axis=1)
        not_partner['_time_rank'] = not_partner['event_type'].map(TIME_RANK).fillna(1)
        not_partner = not_partner.sort_values(['project_id', '_sev_rank', '_time_rank', '_enrich_count'], ascending=[True, False, False, False])
        # FIX (2026-11-10): grouped by project instead of one flat list - the same "state the
        # shared thing once" principle as _partner_header() above, applied here since every row
        # in this section shares a project but not a partner.
        for _, proj_rows in not_partner.groupby('project_id', sort=False):
            units.append(('Not partner-specific', [_project_header(proj_rows)] + render_rows(proj_rows)))

    return _pack_telegram_messages(eval_date, units)


def _pack_telegram_messages(eval_date, units):
    """Greedily packs (am_name, group_lines) units (now one per partner, or per project inside
    "Not partner-specific" - see build_telegram_message()) into one or more Telegram messages,
    each kept under TELEGRAM_CHAR_BUDGET. An AM's bold header is printed once per message that
    carries any of that AM's units, re-printed at the top of a later message if that AM's
    content had to continue there, so a message read on its own still says whose alerts it
    shows.

    FIX (2026-11-10): confirmed real bug, not just a style complaint - when the AM did NOT
    change between two units (e.g. two different partners under the same AM), `addition` used
    to be just `plines` with no separator at all, so the real 2026-11-09 message ran partner_
    1441's second alert straight into partner_124's block with zero blank line between them.
    Every unit now gets at least a one-line gap from whatever came before it; only the very
    first unit for a given AM also gets that AM's header."""
    base_header = f"Daily health check - {eval_date.date()}"
    messages = []       # list of fully-joined message strings
    current_lines = []  # lines for the message currently being built
    current_am = None   # which AM's header is already printed in current_lines (None = none yet)

    def header_for(am):
        return [f"\n<b>{_esc(am)}</b>", ""]

    def flush():
        if current_lines:
            messages.append('\n'.join(current_lines))

    for am, plines in units:
        addition = (header_for(am) if am != current_am else [""]) + plines
        addition_len = len('\n'.join(addition)) + (1 if current_lines else 0)
        if current_lines and len(base_header) + 1 + len('\n'.join(current_lines)) + addition_len > TELEGRAM_CHAR_BUDGET:
            flush()
            current_lines = []
            current_am = None
            addition = header_for(am) + plines  # new message - always needs its own AM header
        current_lines.extend(addition)
        current_am = am

    flush()
    n = len(messages)
    return [f"{base_header}{f' (part {i}/{n})' if n > 1 else ''}\n{m}" for i, m in enumerate(messages, 1)]


def send_telegram(text):
    """Returns True only if the message was actually sent and Telegram accepted it (status 200).
    FIX (2026-11-06, High finding from the real-data audit): used to return None unconditionally
    (not configured, failed, and succeeded all looked the same to the caller) - send_telegram_
    for_date() was calling mark_telegram_sent() regardless, so a real send failure would still
    get recorded as "sent" with no way to notice or retry. Not observed misfiring on the real
    2026-11-01..04 runs (every one logged the success line), but the guard had no way to catch
    it if it ever did."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        log("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing from .env) - skipping send")
        return False
    try:
        import requests
        # FIX (2026-11-05 Telegram audit): HTML parse_mode so build_telegram_message() can
        # bold [CRITICAL] lines - message content is html.escape()'d per-field at build time.
        resp = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              data={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}, timeout=15)
        if resp.status_code == 200:
            log("Telegram message sent")
            return True
        log(f"Telegram send failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        log(f"Telegram send failed (non-fatal): {e}")
        return False


def send_telegram_to_chat(text, chat_id):
    """Same as send_telegram() but to an explicit chat_id instead of the shared
    TELEGRAM_CHAT_ID - added 2026-11-17 for per-AM chat routing (see alert_telegram.py's
    AM_TELEGRAM_CHATS/send_am_chat_messages()). Only TELEGRAM_BOT_TOKEN is required here (the
    chat is passed in, not read from env) - everything else (HTML parse_mode, the 200-check)
    matches send_telegram() exactly."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        log("Telegram not configured (TELEGRAM_BOT_TOKEN missing from .env) - skipping send")
        return False
    try:
        import requests
        resp = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              data={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}, timeout=15)
        if resp.status_code == 200:
            log(f"Telegram message sent to chat {chat_id}")
            return True
        log(f"Telegram send to chat {chat_id} failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        log(f"Telegram send to chat {chat_id} failed (non-fatal): {e}")
        return False


def _load_sent_log():
    if not os.path.exists(SENT_LOG_PATH):
        return {}
    try:
        with open(SENT_LOG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _eval_date_key(eval_date):
    import pandas as pd
    return pd.Timestamp(eval_date).strftime('%Y-%m-%d')


def already_sent_telegram(eval_date):
    """FIX (idempotency, AUDIT_HISTORY.md item 4): True if a Telegram message has already
    gone out for this eval_date (scheduled run or a manual run_for_date.py rerun)."""
    return _eval_date_key(eval_date) in _load_sent_log()


def mark_telegram_sent(eval_date):
    key = _eval_date_key(eval_date)
    log_data = _load_sent_log()
    log_data.setdefault(key, []).append(dt.datetime.now().isoformat(timespec='seconds'))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(SENT_LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, indent=2)


def send_telegram_for_date(eval_date, texts, force=False):
    """Guarded entry point for the actual send - main() and run_for_date.py should call this
    instead of send_telegram() directly. Without `force`, a second run for a date that was
    already sent is logged and skipped rather than silently duplicating the AM-facing message.
    `texts` is the list build_telegram_message() returns (one or more message parts) - a bare
    str is also accepted for callers/tests that still pass one, wrapped into a 1-item list.

    FIX (2026-11-06, High finding from the real-data audit): used to call mark_telegram_sent()
    unconditionally right after send_telegram(), which always returned None regardless of
    success/failure - a real send failure would still get recorded as "sent," with no way to
    notice or retry. Now only marks the date sent when send_telegram() actually reports success.

    FIX (2026-11-09, multi-part messages): build_telegram_message() can now return several
    message parts for one eval_date (see its docstring). All parts are sent in order; the date
    is marked sent only if EVERY part succeeded - a partial failure leaves it unmarked so the
    next run retries the whole day, same all-or-nothing simplicity the single-message version
    already had, rather than tracking which specific parts went through."""
    if isinstance(texts, str):
        texts = [texts]
    key = _eval_date_key(eval_date)
    prior = _load_sent_log().get(key, [])
    if prior and not force:
        log(f"Telegram message SKIPPED for {key} - already sent at {prior[-1]} "
            f"({len(prior)} prior send(s)). Pass force=True / --force to resend anyway.")
        return
    all_ok = True
    for i, text in enumerate(texts, 1):
        if not send_telegram(text):
            all_ok = False
            log(f"Telegram send for {key} part {i}/{len(texts)} did not succeed.")
    if all_ok:
        mark_telegram_sent(eval_date)
    else:
        log(f"Telegram send for {key} did not fully succeed - NOT marked as sent, "
            f"so the next run for this date will retry rather than silently skip.")


def notify_windows(title, message):
    try:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information;"
            "$n.Visible = $true;"
            f"$n.ShowBalloonTip(15000, '{title}', '{message}', [System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Seconds 16; $n.Dispose()"
        )
        subprocess.Popen(['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps])
    except Exception as e:
        log(f"notification failed (non-fatal): {e}")


def main():
    try:
        import db  # noqa: F401  (import check only - confirms .env loaded without KeyError)
    except KeyError as e:
        log(f"BLOCKED: customers/tqa_model/.env is missing required var {e} - see .env.example. "
            f"Create that file with real values before this job can run.")
        notify_windows('Health check blocked', 'customers/tqa_model/.env is missing - see run_log.txt')
        sys.exit(1)

    from health_check_engine import (pull_project, RuleEngine, apply_suppression, apply_correlation,
                                      daily_notifications)
    import pandas as pd

    eval_date = pd.Timestamp(dt.date.today() - dt.timedelta(days=1))
    start = (eval_date - dt.timedelta(days=BUFFER_DAYS)).strftime('%Y-%m-%d')
    end = eval_date.strftime('%Y-%m-%d')
    all_dates = pd.date_range(start, end)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"health_check_{eval_date.strftime('%Y%m%d')}.xlsx")
    any_actionable = False
    total_actionable = 0
    telegram_rows = []       # per-project todays_out + project_id/label, for the AM-grouped Telegram message

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        summary_rows = []
        for pid, label in PROJECTS.items():
            log(f"Project {pid} ({label}): pulling {start}..{end}")
            try:
                ps, promo_map, stats, stats_country = pull_project(pid, start, end)
                if ps.empty:
                    log(f"Project {pid}: no data in window, skipping")
                    continue
                eng = RuleEngine(pid, ps, promo_map, stats, all_dates, eval_date, stats_country=stats_country)
                res = eng.run_all()
                if res.empty:
                    log(f"Project {pid}: 0 raw alert-rows")
                    summary_rows.append({'project_id': pid, 'label': label, 'raw_rows': 0, 'actionable_today': 0})
                    continue
                res['project_id'] = pid  # needed below by attribute_alerts()'s per-row project_id lookup
                # 'linked_*'/'solo_*'/'dominant_partner'/'entity_linked_rule_ids' columns - see
                # apply_correlation()'s own docstring (health_check_engine.py) for the declared
                # identity -> hierarchy-share -> declared-pair order (AUDIT_HISTORY.md finding
                # #84). Runs before daily_notifications() so a linked alert's NEW/ESCALATION/
                # REMINDER event already carries the tags.
                res = apply_correlation(res, ps, eng)
                # FIX (2026-11-10, AUDIT_HISTORY.md finding #82): attribution now runs BEFORE
                # suppression (previously only GUID rows had a resolved partner at this point;
                # Landing Page rows needed this same resolution but it used to happen later, in
                # build_telegram_message(), Telegram-only). Single-project dict is enough here -
                # resolve() only ever looks up this row's own project_id, which is always `pid`.
                res = attribute_alerts(res, {pid: promo_map}, {pid: stats})
                # One unified suppression (finding #82) - replaces the former GUID-only
                # (pre-attribution) + LP-only (post-attribution, Telegram-only) split, so the
                # Excel '_all_open' sheet below and the Telegram message always agree on what's
                # suppressed.
                res = apply_suppression(res)
                notif = daily_notifications(res)
                todays = notif[(notif['date'] == eval_date) & (notif['severity'] != 'INFO')] if not notif.empty else notif
                n_today = len(todays)
                total_actionable += n_today
                if n_today > 0:
                    any_actionable = True
                log(f"Project {pid}: {len(res)} raw rows, {n_today} actionable notifications today ({eval_date.date()})")
                summary_rows.append({'project_id': pid, 'label': label, 'raw_rows': len(res), 'actionable_today': n_today})

                # 'note' added 2026-11-05 (AM-friendly Telegram design) - already computed by
                # the engine's emit() calls, just wasn't selected through before; needed for
                # rule_family_action()'s R2F-12 CPA-gaming flag.
                # 'open_days' added 2026-11-06 (finding #23 fix) - daily_notifications()'s new
                # consecutive-days-open count, needed by _status_tag() for its "DAY N" tag
                # (2026-11-12: this column's consumer was _reminder_note(), since removed).
                # 'linked_rule_ids'/'linked_dominant_partner'/'linked_dominant_share' added
                # 2026-11-09 (finding #48 fix) - needed by _correlation_note() for linked alerts.
                # 'partner_id'/'attribution'/'traffic_type' added 2026-11-10 (finding #82) - now
                # resolved earlier in the pipeline (see attribute_alerts() call above), needed by
                # build_telegram_message() since it no longer re-attributes rows itself.
                # 'escalated_from' added 2026-11-10 (finding #81) for _escalation_note()'s "from
                # WARNING to CRITICAL" wording. FIX (2026-11-12): _escalation_note() removed -
                # _status_tag() just shows "[ESCALATED]" (severity itself already shows the
                # current level) and does not read this column. Left in `cols`/the Excel report
                # as useful audit-trail context, even though nothing in Telegram rendering
                # consumes it anymore.
                # 'top3_dominant_partners'/'top3_dominant_share' (finding #121), 'prior_day_rule_ids'
                # (finding #122), 'ratio_hint_partner'/'ratio_hint_share' (finding #123) added
                # 2026-11-11 - needed by _top3_whale_note()/_lag1_note()/_ratio_hint_note(). Without
                # these in `cols`, the Telegram path (which reads from this subset, not the full
                # `res`) would silently never see them, even though apply_correlation() sets them.
                # 'ratio_hint_confirmed' added 2026-11-13 (finding #148) - the SAME bug bit this
                # column immediately: the two-sided ratio-hint check computed it correctly, but it
                # never reached alert_mapping.py through this list, so every ratio-hint note
                # silently stayed "possible" even when the engine had confirmed it.
                cols = ['date', 'rule_id', 'metric', 'level', 'entity', 'value', 'baseline', 'pct', 'severity', 'event_type', 'note', 'open_days',
                        'escalated_from',
                        'linked_rule_ids', 'linked_dominant_partner', 'linked_dominant_share',
                        'solo_dominant_partner', 'solo_dominant_share',
                        'top3_dominant_partners', 'top3_dominant_share',
                        'prior_day_rule_ids', 'prior_day_metric', 'prior_day_shared_partner',
                        'ratio_hint_partner', 'ratio_hint_share', 'ratio_hint_confirmed',
                        'dominant_partner', 'dominant_partner_share', 'entity_linked_rule_ids',
                        'formula_linked_rule_ids', 'formula_small_denominator',
                        'partner_id', 'attribution', 'traffic_type']
                sheet_today = f'P{pid}_today'[:31]
                todays_out = todays[cols] if not todays.empty else pd.DataFrame(columns=cols)
                todays_out.to_excel(writer, sheet_name=sheet_today, index=False)
                if not todays_out.empty:
                    tr = todays_out.copy()
                    tr['project_id'] = pid
                    tr['project_label'] = label
                    telegram_rows.append(tr)

                sheet_all = f'P{pid}_all_open'[:31]
                res.sort_values(['date', 'rule_id', 'entity']).to_excel(writer, sheet_name=sheet_all, index=False)
            except Exception:
                log(f"Project {pid}: FAILED - {traceback.format_exc()}")
                summary_rows.append({'project_id': pid, 'label': label, 'raw_rows': 'ERROR', 'actionable_today': 'ERROR'})

        pd.DataFrame(summary_rows).to_excel(writer, sheet_name='Summary', index=False)
        notes = pd.DataFrame({'Note': [
            f"Run for eval_date={eval_date.date()} (yesterday), window {start}..{end}.",
            # FIX (2026-11-12, health-check-auditor finding, general audit 2026-11-12):
            # confirmed stale - directly contradicted the PROJECTS dict in this same file, which
            # has covered all 6 non-disabled projects since 2026-11-05 (see that dict's own
            # comment). Finding #42 flagged this exact drift before; it had not been fixed here.
            "Projects covered: all of PROJECTS above (1, 3, 4, 6, 7, 10) - every project except "
            "2 (Wazino777, disabled). A quiet project can still have an alert worth seeing the "
            "day it wakes up (see PROJECTS' own comment), so none are excluded by volume.",
            "'<proj>_today' = actionable (WARNING/CRITICAL) NEW or ESCALATION notifications for eval_date only "
            "- this is the daily inbox. '<proj>_all_open' = every currently-open alert row in the pulled window, "
            "for context/debugging.",
            "See health_check_engine.py's module docstring for the full list of rules NOT simulated "
            "(intraday/continuous, and ones needing an undefined business term) and caveats on the Finance rules.",
            "Thresholds were validated in depth against Project 3's September 2026 data; Projects 4/6 use the "
            "same template thresholds unvalidated against their own volume profile - treat their results as "
            "directional until spot-checked the way Project 3 was.",
        ]})
        notes.to_excel(writer, sheet_name='Methodology', index=False)

    log(f"Wrote {out_path}")

    all_alerts = pd.concat(telegram_rows, ignore_index=True) if telegram_rows else pd.DataFrame(
        columns=['date', 'rule_id', 'metric', 'level', 'entity', 'value', 'baseline', 'pct', 'severity',
                 'event_type', 'project_id', 'project_label', 'partner_id', 'attribution', 'traffic_type'])
    # FIX (2026-11-12): replaced the free-text renderer (build_telegram_message() and
    # everything it calls - render_single()/render_group()/render_cluster(), the dozen
    # _*_note() functions, _pack_telegram_messages()) with the typed-record pipeline
    # (alert_types.py/alert_mapping.py/alert_render.py/alert_telegram.py) per the user's own
    # task: structured Alert records through a deterministic renderer, not free-text sentences.
    # The OLD functions are left in this file, unused by main() now, rather than deleted in the
    # same change that introduces their replacement - alert_mapping.py's own map_alerts() still
    # reuses a few of their low-level helpers (_metric_label(), BUFFER_DAYS), so some are not
    # truly dead; see this session's own summary for which ones are safe to remove and which
    # the new pipeline depends on.
    from alert_mapping import map_alerts, parse_p4_breakdown
    from alert_telegram import send_for_date
    import re as _re
    p4_breakdown_dict = parse_p4_breakdown(
        fetch_p4_country_breakdown(eval_date)) if (all_alerts['project_id'] == 4).any() else None
    am_map = fetch_account_managers(all_alerts['partner_id'].dropna().unique().tolist())
    ref_ids = set(all_alerts['partner_id'].dropna().astype(int))
    for col in ('linked_dominant_partner', 'solo_dominant_partner', 'dominant_partner', 'ratio_hint_partner'):
        if col in all_alerts.columns:
            ref_ids |= set(all_alerts[col].dropna().astype(int))
    if 'top3_dominant_partners' in all_alerts.columns:
        for s in all_alerts['top3_dominant_partners'].dropna():
            ref_ids |= {int(p) for p in _re.findall(r'partner_(\d+)', str(s))}
    partner_names = fetch_partner_names(ref_ids)
    # `send_for_date()`'s own `date` param is used both for the message's own display text and
    # (via send_telegram_for_date()'s pd.Timestamp(eval_date) conversion) the idempotency key -
    # a plain "YYYY-MM-DD" string works for both; passing the raw Timestamp here instead would
    # make the message header read "2026-11-11 00:00:00", not "2026-11-11".
    typed_alerts = map_alerts(all_alerts, am_map, partner_names, p4_breakdown_dict, str(eval_date.date()))
    send_for_date(typed_alerts, str(eval_date.date()))
    # Per-AM chat routing (added 2026-11-17, see alert_telegram.py's AM_TELEGRAM_CHATS) -
    # additive alongside the shared-chat send above, not a replacement for it.
    from alert_telegram import send_am_chat_messages
    send_am_chat_messages(typed_alerts, str(eval_date.date()))

    if total_actionable > 0:
        notify_windows('Daily health check', f'{total_actionable} actionable alert(s) for {eval_date.date()} - see the report')
    else:
        notify_windows('Daily health check', f'No actionable alerts for {eval_date.date()}')

    try:
        os.startfile(out_path)
    except Exception as e:
        log(f"could not auto-open report (non-fatal): {e}")


if __name__ == '__main__':
    main()
