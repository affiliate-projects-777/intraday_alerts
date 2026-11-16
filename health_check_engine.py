# -*- coding: utf-8 -*-
"""
Project health-monitoring rule engine, generalized across projects.
Source of truth for thresholds: Project_Health_Monitoring_Framework_FINAL_v2.xlsx, 'Final Rules' sheet.

NOT SIMULATED (flagged, not silently skipped):
  - Intraday/continuous rules (need live timestamp monitoring, not a daily historical re-run):
    DAT-01, CLK-01, CLK-17, REG-01, REG-17, FTD-01, DEP-01, BET-01, TDS-01, RDR-01.
  - TRK-01: "unattributed conversions" (no valid GUID/click ID) isn't distinguishable in this
    promo-keyed pull - would need the raw unattributed rows from projects_statistics, not yet scoped.
  - ARC-01 (archetype mix): archetype definitions live in tqa_model/verdict_service, not this replica.

IMPLEMENTED 2026-11-13 (finding #149), both previously listed above as undefined - the user
resolved both definitions directly, see run_retention()/run_roas() for the full reasoning:
  - RET-01/02 (retention): cohort = depositors in period N (not FTDs, not all registrants),
    retained = deposit again in period N+1. D7 weekly, D30 monthly (its own wider query - see
    run_retention()'s own comment on why BUFFER_DAYS=35 isn't enough for a monthly cohort).
  - ROA-01 (ROAS) = program profit / investment (CPA + listing fee), program profit computed the
    same corrected per-partner-RS-floor way as tqa_model/corrected_ngr_program_partner_
    investment.sql, at this rule's own weekly grain.

CAVEATS carried over from the Project-3/September validation, still unresolved here:
  - WDR-01/02 use payouts $ as a proxy for "withdrawal requests" (a count) - not the same signal.
  - BAB-01/02 use the template's own placeholder 0.04 assumed house edge - recheck per project.
  - FGR-01 "FTD GGR" is approximated as bets-wins on rows where is_first_depositor=1 that week
    (the FTD day's own play), not a cohort-level FTD GGR definition.
  - NGR formula assumed as GGR - bonuses - chargebacks - admin_fee (standard convention, not
    confirmed against this project's canonical NGR calc).
  - partner_fd_migrations exclusion still not applied (no clear customer-level join - same gap
    as before).

FIXES carried over from the Project-3 validation and later sessions:
  1. GUID Complete-drop/Spike (CLK/REG/FTD-15/16) require 2 consecutive days, matching every
     Partner/Project rule, instead of firing on a single day.
  2+3. GUID-level rules are suppressed when the same-metric Partner-level rule is already open
     for that GUID's partner the same day (covers Complete-drop/Spike AND the conversion-ratio
     rules C2R-12/13, R2F-13).
  4. Breadth rules (CLK/REG/FTD-13/14 at Partner level, CLK-18/19/FTD-18 at LP level) only count
     a GUID as "active" if ITS OWN baseline clears the same floor the standalone GUID rules use,
     and require >=3 qualifying GUIDs before computing a breadth % at all - otherwise a breadth
     rule is just 1-2 GUIDs' own noise relabeled as a "widespread" signal.
  5. BAB-02 (bonus abuse ratio) requires a real bets floor ($1000 baseline/$500 today), not just
     a ratio threshold - see inline comment near run_product().
  6. RTP-01 checks the high-RTP band and low-RTP band as two separate directional alerts instead
     of one direction-blind OR - see inline comment near run_product().
  7. All Click-to-Reg and Reg-to-FTD ratio rules (C2R-*, R2F-* at every level) now require the
     deviation from baseline to exceed the LARGER of (a) 1.5x this entity's own trailing 21-day
     day-level standard deviation, or (b) 1.96x the binomial standard error implied by today's
     own denominator volume (sqrt(p*(1-p)/n) where p=baseline rate, n=today's denominator) -
     see volatility_guard(). (a) catches entities that are just generally noisy (promo cycles,
     day-of-week effects); (b) catches the case (a) alone misses - a metric built on small raw
     counts swings harder in % terms purely from sampling noise (+/-2 conversions is enormous on
     a denominator of 15, a rounding error on 500), independent of that entity's past behaviour.
     Found 2026-11-05: lp_179's R2F-14 fired at 9.5%/-22% off a 12.2% 7-day baseline, but 9-11%
     readings recur every 1-2 weeks for this LP and always bounce back within days - the flat
     threshold didn't know this LP is simply noisier than the band assumed, the same structural
     gap as Fix 6 but for conversion ratios instead of RTP. Across Projects 3/4/6 over the last
     5 weeks this took conversion-rule actionable alerts from 30/23/4 down to 12/12/1.

FIXES from the 2026-11-05 audit (traffic/health_check/audit_agent/reports/audit_general_2026-11-05.md):
  8. GGR-01 (project GGR<=0) was missing the "& bets_guard" its own note already claimed was
     applied, unlike GGR-02/03 right below it - the only "complete collapse" rule in the finance
     block with no volume guard, and an absolute (not relative) threshold, so it was far more
     reachable by whale-variance noise on a low-volume day. Confirmed as the likely cause of
     Project 6 showing GGR-01 open 10/35 days, -$10 to -$4,462, no magnitude floor.
  9. CLK/REG/FTD-13 (Partner breadth-drop) reported the partner's own aggregate trend (ppct) as
     its value/baseline/pct, even though the rule triggers on breadth (share of GUIDs individually
     down) independent of the aggregate's direction - confirmed partner_1441's CLK-13 fired
     showing +110.6% under a rule named "breadth drop." Now reports the breadth share itself
     (count of dropping GUIDs / qualifying active GUIDs) so the displayed number matches what the
     rule actually measures. CLK/REG/FTD-14 (breadth-spike) have the identical mechanism but were
     left unchanged here - out of this fix's scope, tracked separately in AUDIT_HISTORY.md.
 10. BAB-01 (project-level bonus abuse ratio) had no bets-floor guard at all, unlike BAB-02 after
     Fix 5 - same divide-by-near-zero risk in principle. Now requires the same project-level
     bets_guard (>=50% of 7d avg) already computed and used by RTP-01/02/03 in this function.
 11. Removed the 'DEP-03' entry from TIERS: it had no self.emit() call anywhere in this file and
     is not on the NOT SIMULATED list above (which names DEP-01, not DEP-03) - dead configuration
     that could never fire, not a real rule being removed.
 12. CLK/REG/FTD-02/03 (Project-level drop/spike) had NO minimum volume floor at all - only the
     partner-breadth check (which itself only required a partner to average >=1/day to count as
     "active"). User-flagged false alarm 2026-11-03: Brazino777.in's REG-02/03 fired a 209% rise
     off a 4.86 regs/day baseline (15 vs ~4.86) - the same small-counts-swing-harder-in-%-terms
     gap Fix 7 fixed for ratio rules, just never applied to these raw-count project-level rules
     (the Project/Traffic-type tier right below already has a floor; this tier didn't). Now
     requires the same floor used at Project/Traffic-type level (Clicks >=500/day, Registrations
     >=50/day, FTDs >=20/day baseline) before CLK/REG/FTD-02/03 can fire.
 13. pull_project() derived a project's promo_id universe entirely from projects_statistics (the
     table only gets a row per customer-level event - registration/deposit/etc, never for a pure
     click). User-flagged 2026-11-05: a real ~115k/day -> near-zero clicks collapse on project 7
     (Brazino777.in), verified against the admin API, was completely invisible to every Clicks-
     based rule at every level. Root cause: 18 of the 21 promos on that project's own landing
     pages (via promo.landing_id -> landings.project_id, the only real project link - promo itself
     has no project_id column) never had a single projects_statistics row in the window, so they
     were never pulled into promo_statistics_countries either, despite driving virtually all of
     that click volume. Now derives promo_ids from landings.project_id directly (unioned with
     projects_statistics's own promo_ids as a safety net), so every promo on the project's landing
     pages is covered regardless of whether it ever converted.

FIXES from the 2026-11-10 flow-architecture audit (reports/audit_flow_architecture_2026-11-10.md),
implemented same day, user-approved ("build everything in roadmap"):
 14. Breadth-share numerator/denominator mask mismatch (AUDIT_HISTORY.md finding #60, confirmed
     still live at the time of this audit): CLK/REG/FTD-13 (Partner) and CLK-18/FTD-18 (LP)
     computed the breadth-drop/spike numerator (which GUIDs individually moved) over ALL of a
     partner's/LP's GUIDs, while the denominator (how many GUIDs counted as "active") was masked
     to only the floor-clearing subset - the two were never the same set, so the reported share
     could mathematically exceed 100% (confirmed real example: partner_1441's CLK-13 rendered
     "150% drop"). New shared masked_share() helper masks the numerator by the SAME eligibility
     mask used for the denominator, by construction, so this can't recur at a third call site
     later - replaces the dead, already-broken breadth_share_guid() placeholder (never called,
     its own `active` line was a tautology: `... and True) or True`) rather than leaving both the
     bug and the dead scaffolding in place.
 15. Suppression unified (AUDIT_HISTORY.md finding #82): the former two-mechanism split - this
     file's GUID-level apply_suppression()/GUID_SUPPRESS_UNDER (pre-attribution, feeding Excel)
     and daily_health_check.py's LP-level apply_lp_suppression()/LP_SUPPRESS_UNDER (post-
     attribution, Telegram-only) - meant the Excel "_all_open" sheet and the Telegram feed could
     silently disagree on what was suppressed. Replaced by one SUPPRESSION_HIERARCHY covering
     both GUID and LP children, and one apply_suppression(res_df) that runs AFTER attribution
     (so partner_id is already resolved for every row, GUID or LP) and before either output is
     written - both now agree by construction. Callers (main()/run_for_date.py) now call
     attribute_alerts() before apply_suppression() instead of after.
 16. Correlation/outlier detection decision tree formalized (AUDIT_HISTORY.md finding #84): the
     three correlation mechanisms (correlate_alerts - customer identity; annotate_project_
     dominant_partner - partner-id hierarchy share; correlate_entity_pairs - declared static
     pairs) were three independent call sites in main()/run_for_date.py with no stated reason for
     their order. New apply_correlation(res_df, ps, eng) runs them in that declared priority
     order as one call - reuses each mechanism's own, already-validated logic unchanged; this is
     an ordering/documentation fix, not a rewrite of any of the three.
 17. ESCALATION now carries which severity it escalated FROM (AUDIT_HISTORY.md finding #81):
     daily_notifications() already computed event_type=='ESCALATION' correctly, but nothing
     downstream could say "escalated from WARNING to CRITICAL" vs. just "CRITICAL" - the render-
     side fix (new _escalation_note(), daily_health_check.py) needs this field to exist here.

FIXES from the 2026-11-10 source-spec cross-check (findings #105-114), implemented same day,
user-approved ("Everything (1+2+3)") - see AUDIT_HISTORY.md's Implemented Improvements items
99-108 for the full detail per fix; summarized here per this file's own convention:
 18. GGR-02/03's bets guard tightened from +/-50% to +/-10% (finding #105); ADM-01/BON-01/WDR-01
     given the guard the spec's own text requires, DPS-01 confirmed deliberately ungated
     (finding #106); C2R-12/R2F-13 given the INFO tier the spec specifies (finding #114).
 19. NGR-01 gained the spec's "<=0 for 3 consecutive days" absolute-critical path, computed
     directly alongside the existing relative-drop WARNING path (finding #108). BAB-01's
     severity is now magnitude- not duration-based (CRITICAL at ratio>1.5, computed directly,
     bypassing emit()/TIERS - the old TIERS entry was dead config, see finding #107).
 20. New _apply_declared_escalations(), run once after every run_* method in run_all():
     GGR-03 escalates to CRITICAL when RTP-02 also fires the same project/day (finding #109).
 21. New GUID_LP_SUPPRESSION (GUID child suppressed under its own Landing Page's alert, not
     just its Partner's) and SIBLING_SUPPRESSION (same-level/same-entity/different-metric
     suppression - the "clicks > registrations > FTDs" funnel chain, plus RTP-01 suppressed
     under RTP-02/03) dicts, OR'd into apply_suppression() alongside the original
     SUPPRESSION_HIERARCHY (finding #111a/b). Needed attribute_alerts() (daily_health_check.py)
     to also resolve a GUID row's own landing_id.
 22. New rules: DEP-02 (Project, weekly breadth-drop in distinct depositors) and DEP-03
     (Project, daily drop in distinct depositors) - a different signal from DPS-01/02/03's
     dollar amount (finding #110). STD-01 now also computed per traffic-type, and STD-01's
     2nd-consecutive-week CRITICAL / STD-02's 2-consecutive-week firing requirement are both
     implemented (finding #112). GEO-01 (Partner, foreign-geo registration share) - needed a
     real pull_project() query change to pull country-level stats, not the "zero new query"
     this finding originally assumed (finding #113); RuleEngine's new stats_country param is
     optional, so every pre-existing caller keeps working unchanged.
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'customers', 'tqa_model')))
from db import replica_run_query  # noqa: E402  (requires customers/tqa_model/.env to exist)

TRAFFIC_TYPES = ['Push/Pop', 'Retargeting', 'SEO', 'PPC']
GUID_FLOOR = {'Clicks': 50, 'Registrations': 5, 'FTDs': 5}  # standalone GUID-rule floors, reused for breadth

# finding #113 (GEO-01, Tier 3): each project's own target-market country set, by project_id's
# own brand/TLD (same per-project mapping daily_health_check.py's PROJECTS dict documents as
# labels only - duplicated here as data since this module must not import daily_health_check.py,
# the wrong dependency direction). Project 4 (Brazino777.io) is the one genuinely multi-country
# brand - its set matches daily_health_check.py's own P4_COUNTRIES constant (AR/BO/CL/CO/EC/VE),
# not invented here, just not importable from this side.
PROJECT_TARGET_COUNTRIES = {
    1: {'BR'}, 3: {'MX'}, 4: {'AR', 'BO', 'CL', 'CO', 'EC', 'VE'}, 6: {'AR'}, 7: {'IN'}, 10: {'PY'},
}


# ================================================================= DATA PULL
def pull_project(project_id, start, end):
    # tag_id 41 = "Test" in partner_tags (confirmed 2026-11-10 via partner_11414 - real,
    # growing click volume spread near-evenly across every project with ~0 regs/FTDs, which
    # looked like a cross-project funnel break until the tag confirmed it's a test account).
    #
    # tag_id 33/34 = "Vicky"/"Vicky2" (user-reported 2026-11-12: a live GGR-02 alert's "Not one
    # dominant player" note named partner_1294/9977/10402 - TitanReach/ApexBet/fokatsi - as 91%
    # of the day's GGR move; user: these carry tags 33/34 and "are not real partners"). Verified
    # by direct query, not taken on trust: exactly 5 partners carry tag 33 or 34 (514, 1120,
    # 1294, 9977 -> 33; 10402 -> 34), and NONE of the 5 carries any other tag - unlike a real AM's
    # book (e.g. partner_10419 carries 'Gerakanakis, Willard' together). partner_10402/fokatsi is
    # independently confirmed as a migration SINK in `partner_fd_migrations` - it is
    # new_partner_id for 10+ rows whose old_partner_id values are unrelated legacy partners (149,
    # 554, 4109, 6860, 10042, 9537, 10419, ...), i.e. its activity is largely re-homed legacy
    # customers, not its own acquired traffic. fetch_account_managers()'s docstring (daily_
    # health_check.py) assumed tag_id 1/37 were the only non-AM tags, but that was only ever
    # confirmed against 4 sample titles (Fotini/Willard/jackie/Gerakanakis) - 33/34 were never
    # checked and slipped through. Excluding them at the SOURCE (not just from the dominant-
    # player note) because their activity was never real partner-driven traffic/GGR and was
    # already polluting Project-level aggregates too - the GGR-02 alert that surfaced this had
    # 91% of its move explained by these 3 accounts alone.
    excl = replica_run_query("SELECT DISTINCT partner_id FROM partner_tags_ref WHERE tag_id IN (1, 33, 34, 37, 41)")
    excluded_partners = set(excl['partner_id'].tolist())

    # 'partner_profit' added 2026-11-13 (ROA-01, finding #149): needed to compute program_profit
    # the SAME corrected way tqa_model/corrected_ngr_program_partner_investment.sql does - NGR
    # minus each partner's OWN RS (partner_profit) floored at 0 before summing across partners -
    # not the raw program_profit column, which that file's own comment confirms can let one
    # partner's negative-RS month net against another's positive one if just summed directly.
    ps = replica_run_query(f"""
        SELECT date, partner_id, promo_id, customer_id,
               payments, payouts, bonuses, chargebacks, cpa_amount,
               bets, wins, total_profit, admin_fee, partner_profit,
               is_first_depositor, first_deposit_amount, number_of_deposits
        FROM projects_statistics
        WHERE project_id = {int(project_id)} AND date >= '{start}' AND date <= '{end}'
    """)
    ps = ps[~ps['partner_id'].isin(excluded_partners)].copy()
    for c in ['payments', 'payouts', 'bonuses', 'chargebacks', 'cpa_amount', 'bets', 'wins',
              'total_profit', 'admin_fee', 'partner_profit', 'first_deposit_amount']:
        ps[c] = pd.to_numeric(ps[c], errors='coerce').fillna(0)
    ps['is_first_depositor'] = pd.to_numeric(ps['is_first_depositor'], errors='coerce').fillna(0)
    ps['number_of_deposits'] = pd.to_numeric(ps['number_of_deposits'], errors='coerce').fillna(0)
    ps['date'] = pd.to_datetime(ps['date'])

    # FIX 13: a promo's true project is promo.landing_id -> landings.project_id (the authoritative
    # link - `promo` itself has no project_id column). Deriving promo_ids from projects_statistics
    # alone (the old approach) misses any promo that got clicks but never produced a single
    # projects_statistics row (that table only gets a row per customer-level event - registration/
    # deposit/etc - never for a pure click). Confirmed 2026-11-05: project 7 (Brazino777.in) had 21
    # promos on its own landing pages (451/460), only 3 of which ever appeared in projects_statistics
    # in this window - the other 18 (incl. partner_1441, partner_5164, partner_11569's promos) drove
    # up to ~115k clicks/day that were completely invisible to every Clicks-based rule at every
    # level.
    landing_promo_ids = set(replica_run_query(f"""
        SELECT p.id AS promo_id FROM promo p
        JOIN landings l ON l.id = p.landing_id
        WHERE l.project_id = {int(project_id)}
    """)['promo_id'].tolist())

    # FIX 13 follow-up (2026-11-05, same-day user-confirmed double-count): the original version
    # unioned in EVERY promo_id ps had for this project_id, as a safety net for a promo whose
    # landing_id changed after an old projects_statistics row was written. That's too broad -
    # confirmed via guid_30529 (partner_1441): its landing (441, "Ice Fishing Wheel, Paraguay")
    # authoritatively belongs to project 10 (Brazino777.py) per `landings`, but projects_statistics
    # has 9 STALE rows mis-tagging it under project_id=4 (2026-10-15..22) - a one-time data-entry
    # error, not a real landing migration. Blindly trusting ps's own project_id duplicated this
    # promo's clicks/regs into BOTH project 4's and project 10's RuleEngine, firing the identical
    # REG-15 alert twice under two different brands. Now only adds a ps-derived promo_id into the
    # safety net when its OWN landing_id doesn't resolve to any project at all (a dead/deleted
    # landing) - never when it resolves to a DIFFERENT project than the one being pulled, since
    # that's a stale/mis-tagged ps row, not evidence the promo ever really belonged here.
    extra_ps_promo_ids = set(ps['promo_id'].dropna().astype(int).unique().tolist()) - landing_promo_ids
    if extra_ps_promo_ids:
        ids_str = ','.join(str(x) for x in extra_ps_promo_ids)
        resolved = set(replica_run_query(f"""
            SELECT p.id AS promo_id FROM promo p
            JOIN landings l ON l.id = p.landing_id
            WHERE p.id IN ({ids_str})
        """)['promo_id'].tolist())
        extra_ps_promo_ids -= resolved  # drop any that resolve to a DIFFERENT project - stale ps tag
    promo_ids = sorted(landing_promo_ids | extra_ps_promo_ids)
    if promo_ids:
        ids_str = ','.join(str(int(x)) for x in promo_ids)
        promo_map = replica_run_query(
            f"SELECT id AS promo_id, partner_id, traffic_type, landing_id, guid FROM promo WHERE id IN ({ids_str})")
        # finding #113 (GEO-01, Tier 3): promo_statistics_countries is keyed (date, promo_id,
        # country) - country was always IN this table, just summed away by the GROUP BY below
        # (previously date, promo_id only), per reports/design_outlier_and_merge_analysis_2026-
        # 11-09.md section 1b, which flagged this as "a real change to pull_project(), not a new
        # table, but not free either." Now pulled WITH country into stats_country, then
        # re-aggregated into the exact same `stats` shape every other caller already relies on -
        # no existing consumer of `stats` changes behavior; stats_country is additive, GEO-01-only.
        stats_country = replica_run_query(f"""
            SELECT date, promo_id, country, SUM(clicks) AS clicks, SUM(unique_clicks) AS unique_clicks,
                   SUM(registrations) AS registrations
            FROM promo_statistics_countries
            WHERE promo_id IN ({ids_str}) AND date >= '{start}' AND date <= '{end}'
            GROUP BY date, promo_id, country
        """)
        stats = stats_country.groupby(['date', 'promo_id'], as_index=False)[['clicks', 'unique_clicks', 'registrations']].sum()
    else:
        promo_map = pd.DataFrame(columns=['promo_id', 'partner_id', 'traffic_type', 'landing_id', 'guid'])
        stats = pd.DataFrame(columns=['date', 'promo_id', 'clicks', 'unique_clicks', 'registrations'])
        stats_country = pd.DataFrame(columns=['date', 'promo_id', 'country', 'clicks', 'unique_clicks', 'registrations'])

    stats['date'] = pd.to_datetime(stats['date'])
    stats = stats.merge(promo_map, on='promo_id', how='left')
    stats = stats[~stats['partner_id'].isin(excluded_partners)]

    stats_country['date'] = pd.to_datetime(stats_country['date'])
    stats_country = stats_country.merge(promo_map, on='promo_id', how='left')
    stats_country = stats_country[~stats_country['partner_id'].isin(excluded_partners)]
    return ps, promo_map, stats, stats_country


# ================================================================= HELPERS
def baseline(series_df):
    return series_df.shift(1).rolling(7).mean()


def pctchg(series_df, base):
    return (series_df - base) / base.replace(0, np.nan) * 100


def ratio_sum_baseline(numer, denom, window=7, scale=100):
    """Sum-weighted baseline for a numer/denom ratio: the 7d baseline is sum(numer)/sum(denom)
    over the trailing window, not a mean of each day's own ratio - so a low-denominator day
    can't swing the baseline as hard as a high-denominator day. `scale` is 100 for rate-as-
    percentage metrics (click-to-reg, bonuses/deposits); pass scale=1 for a $-per-count ratio
    (e.g. AFD-01/FBT-01) where multiplying by 100 would misrepresent the dollar value."""
    today = numer / denom.replace(0, np.nan) * scale
    num_base = numer.shift(1).rolling(window).sum()
    den_base = denom.shift(1).rolling(window).sum()
    base = num_base / den_base.replace(0, np.nan) * scale
    pct = (today - base) / base.replace(0, np.nan) * 100
    return today, base, pct


def denom_guard(denom_series, pct_band):
    b = baseline(denom_series)
    p = pctchg(denom_series, b).abs()
    return p <= pct_band


def volatility_guard(ratio_today, ratio_base, denom_today, window=21, k=1.5, z=1.96):
    """FIX 7: require today's point deviation from the 7-day baseline to exceed the LARGER of
    two noise estimates, not just clear a flat % threshold:
      (a) k * this entity's OWN trailing day-level standard deviation (empirical, `window`
          days before today) - catches entities that are just generally noisy day to day
          (promo cycles, day-of-week effects) beyond what a single formula predicts.
      (b) z * the binomial standard error implied by TODAY'S OWN denominator volume,
          sqrt(p*(1-p)/n) where p = baseline rate and n = today's denominator - catches
          the case the std alone misses: today's sample is smaller than usual, so even an
          entity with a calm history is statistically noisier today than its own past
          suggests. This is also why a metric built on small counts swings harder in %
          terms than one built on large counts: the same +/-2 raw conversions is a huge %
          move on a denominator of 15 and a rounding error on a denominator of 500 - (b)
          prices that directly instead of relying on (a) to have seen it before.
    Entities with too little history for a stable std (NaN) use (b) alone; if BOTH are
    unavailable (no std AND denom_today<=0), falls through as pass - preserves old
    behavior rather than silently suppressing everything for new/thin entities."""
    std = ratio_today.shift(1).rolling(window).std()
    p = (ratio_base / 100.0).clip(0, 1)
    se_binom = np.sqrt(p * (1 - p) / denom_today.replace(0, np.nan)) * 100
    required = pd.concat([k * std, z * se_binom], axis=1).max(axis=1)
    dev = (ratio_today - ratio_base).abs()
    return (dev > required) | required.isna()


def run_length_true(bool_series):
    """Consecutive-True run length ending at each day (0 if False that day)."""
    grp = (~bool_series).cumsum()
    return bool_series.groupby(grp).cumsum()


def masked_share(hit_mask_df, eligible_mask_df, min_eligible=3):
    """FIX 14 (closes AUDIT_HISTORY.md finding #60): share of ELIGIBLE entities (columns) whose
    hit_mask is True, with the numerator masked to the SAME eligible set as the denominator, by
    construction - the two can never be different sets, so the returned share can never exceed
    1.0 the way the former hand-inlined computation could (confirmed real example: partner_1441's
    CLK-13 on 2026-11-06 reported a "150% drop" because its numerator summed ALL GUIDs' hits while
    its denominator only counted the floor-clearing subset). `min_eligible` is Fix 4's own
    >=3-qualifying-GUIDs requirement, generalized to a parameter instead of a literal re-typed at
    each call site. Returns (share, masked_hit_df, n_eligible) - callers that need to name which
    entities hit (e.g. the Partner-level "hit GUIDs: ..." note) use masked_hit_df, which is now
    the authoritative hit set (same one the share was computed from)."""
    hit = hit_mask_df.where(eligible_mask_df, False).fillna(False)
    n_eligible_raw = eligible_mask_df.sum(axis=1)
    n_eligible = n_eligible_raw.where(n_eligible_raw >= min_eligible, np.nan)
    return hit.sum(axis=1) / n_eligible, hit, n_eligible


class RuleEngine:
    def __init__(self, project_id, ps, promo_map, stats, all_dates, eval_date, stats_country=None):
        self.project_id = project_id
        self.ps = ps
        self.promo_map = promo_map
        self.stats = stats
        # finding #113 (GEO-01): optional, defaults to None so every existing caller/synthetic
        # test that builds a RuleEngine with the old 6-arg signature keeps working unchanged -
        # GEO-01 just doesn't fire (no results appended) when it's not supplied.
        self.stats_country = stats_country
        self.all_dates = all_dates
        self.eval_date = eval_date  # the single day we report "today's" alerts for (usually yesterday)
        self.results = []
        self.guids_by_partner = {pid: grp['promo_id'].tolist() for pid, grp in promo_map.groupby('partner_id')}
        self.guids_by_lp = {lid: grp['promo_id'].tolist() for lid, grp in promo_map.groupby('landing_id')}
        self._build_series()

    def emit(self, rule_id, metric, level, entity, trig_series, value_series, base_series, pct_series, note):
        trig_series = trig_series.reindex(self.all_dates, fill_value=False).fillna(False)
        run = run_length_true(trig_series)
        for d in self.all_dates:
            r = int(run.loc[d])
            if r == 0:
                continue
            sev = severity_for_run(rule_id, r)
            if sev is None:
                continue
            self.results.append({
                'date': d, 'rule_id': rule_id, 'metric': metric, 'level': level, 'entity': entity,
                'value': round(float(value_series.loc[d]), 2) if pd.notna(value_series.loc[d]) else None,
                'baseline': round(float(base_series.loc[d]), 2) if pd.notna(base_series.loc[d]) else None,
                'pct': round(float(pct_series.loc[d]), 1) if pd.notna(pct_series.loc[d]) else None,
                'severity': sev, 'note': note,
            })

    # ------------------------------------------------------------- series prep
    def _build_series(self):
        ad = self.all_dates
        ps = self.ps
        self.proj_clicks = self.stats.groupby('date')['clicks'].sum().reindex(ad, fill_value=0)
        self.proj_regs = self.stats.groupby('date')['registrations'].sum().reindex(ad, fill_value=0)
        self.proj_ftds = ps.groupby('date')['is_first_depositor'].sum().reindex(ad, fill_value=0)
        self.proj_deposits = ps.groupby('date')['payments'].sum().reindex(ad, fill_value=0)
        self.proj_bets = ps.groupby('date')['bets'].sum().reindex(ad, fill_value=0)
        self.proj_wins = ps.groupby('date')['wins'].sum().reindex(ad, fill_value=0)
        self.proj_ggr = self.proj_bets - self.proj_wins
        self.proj_bonuses = ps.groupby('date')['bonuses'].sum().reindex(ad, fill_value=0)
        self.proj_admin = ps.groupby('date')['admin_fee'].sum().reindex(ad, fill_value=0)
        self.proj_chargebacks = ps.groupby('date')['chargebacks'].sum().reindex(ad, fill_value=0)
        self.proj_payouts = ps.groupby('date')['payouts'].sum().reindex(ad, fill_value=0)
        self.proj_ngr = self.proj_ggr - self.proj_bonuses - self.proj_chargebacks - self.proj_admin
        self.proj_depositors = ps[ps['payments'] > 0].groupby('date')['customer_id'].nunique().reindex(ad, fill_value=0)
        fdp = ps[ps['is_first_depositor'] == 1]
        self.proj_fdp_amt = fdp.groupby('date')['first_deposit_amount'].sum().reindex(ad, fill_value=0)
        self.proj_fdp_bets = fdp.groupby('date')['bets'].sum().reindex(ad, fill_value=0)
        self.proj_fdp_wins = fdp.groupby('date')['wins'].sum().reindex(ad, fill_value=0)
        self.proj_fdp_bonuses = fdp.groupby('date')['bonuses'].sum().reindex(ad, fill_value=0)

        # FIX 13 follow-up: since Fix 13, self.stats can have partner/promo/landing ids that ps
        # (projects_statistics, customer-event-only) never has - e.g. a promo with real clicks but
        # zero conversions in this window. Every per-entity frame below must share the SAME column
        # universe (union of both sources), or code elsewhere that does frame_a[id] after iterating
        # frame_b.columns raises KeyError the first time such an id shows up.
        partner_ids = sorted(set(self.stats['partner_id'].dropna().astype(int)) | set(ps['partner_id'].dropna().astype(int)))
        promo_ids_all = sorted(set(self.stats['promo_id'].dropna().astype(int)) | set(ps['promo_id'].dropna().astype(int)))
        landing_ids_all = sorted(set(self.promo_map['landing_id'].dropna().astype(int)))

        self.partner_clicks = self.stats.groupby(['date', 'partner_id'])['clicks'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=partner_ids, fill_value=0)
        self.partner_regs = self.stats.groupby(['date', 'partner_id'])['registrations'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=partner_ids, fill_value=0)
        self.partner_ftds = ps.groupby(['date', 'partner_id'])['is_first_depositor'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=partner_ids, fill_value=0)
        self.partner_depositors = ps[ps['payments'] > 0].groupby(['date', 'partner_id'])['customer_id'].nunique().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=partner_ids, fill_value=0)

        self.guid_clicks = self.stats.groupby(['date', 'promo_id'])['clicks'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=promo_ids_all, fill_value=0)
        self.guid_regs = self.stats.groupby(['date', 'promo_id'])['registrations'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=promo_ids_all, fill_value=0)
        self.guid_ftds = ps.groupby(['date', 'promo_id'])['is_first_depositor'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=promo_ids_all, fill_value=0)

        ps_lp = ps.merge(self.promo_map[['promo_id', 'landing_id']], on='promo_id', how='left')
        stats_lp = self.stats.copy()
        self.lp_clicks = stats_lp.groupby(['date', 'landing_id'])['clicks'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=landing_ids_all, fill_value=0)
        self.lp_regs = stats_lp.groupby(['date', 'landing_id'])['registrations'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=landing_ids_all, fill_value=0)
        self.lp_ftds = ps_lp.groupby(['date', 'landing_id'])['is_first_depositor'].sum().unstack(fill_value=0).reindex(ad, fill_value=0).reindex(columns=landing_ids_all, fill_value=0)

        ps_tt = self.stats[['date', 'promo_id', 'traffic_type']].drop_duplicates()
        ps_ftd_tt = ps.merge(self.promo_map[['promo_id', 'traffic_type']], on='promo_id', how='left')
        self.tt_clicks = self.stats.groupby(['date', 'traffic_type'])['clicks'].sum().unstack(fill_value=0).reindex(ad, fill_value=0)
        self.tt_regs = self.stats.groupby(['date', 'traffic_type'])['registrations'].sum().unstack(fill_value=0).reindex(ad, fill_value=0)
        self.tt_ftds = ps_ftd_tt.groupby(['date', 'traffic_type'])['is_first_depositor'].sum().unstack(fill_value=0).reindex(ad, fill_value=0)

    # ------------------------------------------------------------- VOLUME: project + project/traffic
    def run_project_volume(self):
        # FIX 12: project-level floor, same values as the Project/Traffic-type tier below -
        # without it, a low-volume project (e.g. a handful of regs/day) can swing past any %
        # threshold on pure small-count noise.
        PROJ_FLOOR = {'Clicks': 500, 'Registrations': 50, 'FTDs': 20}
        PROJ_VOL = [
            ('Clicks', self.proj_clicks, self.partner_clicks, 20, 30, 'CLK-02', 'CLK-03'),
            ('Registrations', self.proj_regs, self.partner_regs, 20, 40, 'REG-02', 'REG-03'),
            ('FTDs', self.proj_ftds, self.partner_ftds, 25, 40, 'FTD-02', 'FTD-03'),
        ]
        for metric, series, partner_df, drop_thr, spike_thr, rid_drop, rid_spike in PROJ_VOL:
            b = baseline(series); pct = pctchg(series, b)
            floor_ok = b >= PROJ_FLOOR[metric]
            pbase = baseline(partner_df)
            ppct = pctchg(partner_df, pbase)
            active = pbase >= 1
            drop_share = ((ppct <= -drop_thr) & active).sum(axis=1) / active.sum(axis=1).replace(0, np.nan)
            spike_share = ((ppct >= spike_thr) & active).sum(axis=1) / active.sum(axis=1).replace(0, np.nan)
            drop_trig = (pct <= -drop_thr) & (drop_share.fillna(0) >= 0.5) & floor_ok
            spike_trig = (pct >= spike_thr) & (spike_share.fillna(0) >= 0.5) & floor_ok
            self.emit(rid_drop, metric, 'Project', 'Project', drop_trig, series, b, pct, f">=50% of partners also down; project floor >={PROJ_FLOOR[metric]}/day")
            self.emit(rid_spike, metric, 'Project', 'Project', spike_trig, series, b, pct, f">=50% of partners also up; project floor >={PROJ_FLOOR[metric]}/day")

        TT_RULES = {
            'Clicks': dict(floor=500, series=self.tt_clicks,
                drop={'Push/Pop': (22, 'CLK-04'), 'Retargeting': (18, 'CLK-05'), 'SEO': (22, 'CLK-06'), 'PPC': (45, 'CLK-07')},
                spike={'Push/Pop': (30, 'CLK-08'), 'Retargeting': (40, 'CLK-09'), 'SEO': (55, 'CLK-10'), 'PPC': (85, 'CLK-11')}),
            'Registrations': dict(floor=50, series=self.tt_regs,
                drop={'Push/Pop': (25, 'REG-04'), 'Retargeting': (18, 'REG-05'), 'SEO': (25, 'REG-06'), 'PPC': (40, 'REG-07')},
                spike={'Push/Pop': (40, 'REG-08'), 'Retargeting': (45, 'REG-09'), 'SEO': (55, 'REG-10'), 'PPC': (100, 'REG-11')}),
            'FTDs': dict(floor=20, series=self.tt_ftds,
                drop={'Push/Pop': (40, 'FTD-04'), 'Retargeting': (25, 'FTD-05'), 'SEO': (33, 'FTD-06'), 'PPC': (75, 'FTD-07')},
                spike={'Push/Pop': (65, 'FTD-08'), 'Retargeting': (60, 'FTD-09'), 'SEO': (115, 'FTD-10'), 'PPC': (190, 'FTD-11')}),
        }
        for metric, cfg in TT_RULES.items():
            for tt in TRAFFIC_TYPES:
                if tt not in cfg['series'].columns:
                    continue
                s = cfg['series'][tt]; b = baseline(s); pct = pctchg(s, b)
                eligible = b >= cfg['floor']
                dthr, drid = cfg['drop'][tt]; sthr, srid = cfg['spike'][tt]
                self.emit(drid, f'{metric} ({tt})', 'Project/Traffic', tt, (pct <= -dthr) & eligible, s, b, pct, f"floor {cfg['floor']}/day")
                self.emit(srid, f'{metric} ({tt})', 'Project/Traffic', tt, (pct >= sthr) & eligible, s, b, pct, f"floor {cfg['floor']}/day")

    # ------------------------------------------------------------- VOLUME: partner + GUID + LP
    def run_partner_guid_lp_volume(self):
        PARTNER_CFG = {
            'Clicks': dict(partner_series=self.partner_clicks, guid_series=self.guid_clicks, floor=200,
                           guid_floor=GUID_FLOOR['Clicks'], rid_complete='CLK-12', rid_drop='CLK-13', rid_spike='CLK-14',
                           drop_thr=20, spike_thr=50),
            'Registrations': dict(partner_series=self.partner_regs, guid_series=self.guid_regs, floor=20,
                           guid_floor=GUID_FLOOR['Registrations'], rid_complete='REG-12', rid_drop='REG-13', rid_spike='REG-14',
                           drop_thr=20, spike_thr=50),
            'FTDs': dict(partner_series=self.partner_ftds, guid_series=self.guid_ftds, floor=10,
                           guid_floor=GUID_FLOOR['FTDs'], rid_complete='FTD-12', rid_drop='FTD-13', rid_spike='FTD-14',
                           drop_thr=20, spike_thr=50),
        }
        for metric, cfg in PARTNER_CFG.items():
            pser = cfg['partner_series']; pbase = baseline(pser); ppct = pctchg(pser, pbase)
            eligible = pbase >= cfg['floor']
            gser = cfg['guid_series']; gbase = baseline(gser); gpct = pctchg(gser, gbase)
            for pid in pser.columns:
                if pid not in eligible.columns or not eligible[pid].any():
                    continue
                guid_cols = [g for g in self.guids_by_partner.get(pid, []) if g in gser.columns]
                if not guid_cols:
                    continue
                g_drop_hit_raw = (gpct[guid_cols] <= -cfg['drop_thr']).fillna(False)
                g_spike_hit_raw = (gpct[guid_cols] >= cfg['spike_thr']).fillna(False)
                g_active = (gbase[guid_cols] >= cfg['guid_floor']).fillna(False)  # FIX 4
                # FIX 14: masked_share() masks the numerator by the same g_active eligibility
                # mask used for the denominator - see its own docstring for the bug this closes.
                drop_share, g_drop_hit, n_active = masked_share(g_drop_hit_raw, g_active, min_eligible=3)  # FIX 4b
                spike_share, g_spike_hit, _ = masked_share(g_spike_hit_raw, g_active, min_eligible=3)

                complete_trig = (ppct[pid] <= -90) & eligible[pid]
                drop_trig = (drop_share.fillna(0) >= 0.60) & eligible[pid]
                spike_trig = (spike_share.fillna(0) >= 0.60) & eligible[pid]
                self.emit(cfg['rid_complete'], metric, 'Partner', f'partner_{pid}', complete_trig, pser[pid], pbase[pid], ppct[pid], f"partner floor {cfg['floor']}/day met")
                # FIX 9 (2026-11-05 audit): this rule triggers on breadth (share of qualifying
                # GUIDs individually down), not on the partner's own aggregate trend, so report
                # the breadth share itself instead of ppct[pid] (which can show the opposite sign).
                drop_count = g_drop_hit.sum(axis=1)
                _before = len(self.results)
                self.emit(cfg['rid_drop'], metric, 'Partner', f'partner_{pid}', drop_trig, drop_count, n_active, -(drop_share * 100), f">=60% of qualifying (floor-clearing) GUIDs down >={cfg['drop_thr']}%")
                # finding #68 (connections audit, 2026-11-09): name the actual GUID(s) behind a
                # breadth-drop alert - confirmed on real data that only the minority of these
                # (50% Clicks, 16% Registrations, 6% FTDs) would have independently cleared
                # GUID-15's own -90% suppression threshold, so most of the time nothing names
                # which link(s) actually drove this; the "60% of links dropped" line alone left
                # an AM with no way to investigate without pulling raw data themselves. Top 3 by
                # size of drop, "+N more" beyond that - no new query, same gpct/g_drop_hit already
                # computed above for the share calc itself.
                for r in self.results[_before:]:
                    hits = sorted(
                        ((gid, gpct[gid].loc[r['date']]) for gid in guid_cols if g_drop_hit[gid].loc[r['date']]),
                        key=lambda x: x[1])
                    if not hits:
                        continue
                    shown = ', '.join(f"guid_{gid} {p:.0f}%" for gid, p in hits[:3])
                    extra = f", +{len(hits) - 3} more" if len(hits) > 3 else ""
                    r['note'] = r['note'] + f" - hit GUIDs: {shown}{extra}"
                self.emit(cfg['rid_spike'], metric, 'Partner', f'partner_{pid}', spike_trig, pser[pid], pbase[pid], ppct[pid], f"partner floor {cfg['floor']}/day met")

        GUID_CFG = {
            'Clicks': dict(series=self.guid_clicks, floor=GUID_FLOOR['Clicks'], rid_complete='CLK-15', rid_spike='CLK-16', spike_thr=100),
            'Registrations': dict(series=self.guid_regs, floor=GUID_FLOOR['Registrations'], rid_complete='REG-15', rid_spike='REG-16', spike_thr=100),
            'FTDs': dict(series=self.guid_ftds, floor=GUID_FLOOR['FTDs'], rid_complete='FTD-15', rid_spike='FTD-16', spike_thr=100),
        }
        for metric, cfg in GUID_CFG.items():
            s = cfg['series']; b = baseline(s); pct = pctchg(s, b)
            eligible = b >= cfg['floor']
            complete_trig = (pct <= -90) & eligible
            spike_trig = (pct >= cfg['spike_thr']) & eligible
            for gid in s.columns:
                self.emit(cfg['rid_complete'], metric, 'GUID', f'guid_{gid}', complete_trig[gid], s[gid], b[gid], pct[gid], f"GUID floor {cfg['floor']}/day met")
                self.emit(cfg['rid_spike'], metric, 'GUID', f'guid_{gid}', spike_trig[gid], s[gid], b[gid], pct[gid], f"GUID floor {cfg['floor']}/day met")

        LP_CFG = {
            'Clicks': dict(series=self.lp_clicks, guid_series=self.guid_clicks, guid_floor=GUID_FLOOR['Clicks'],
                           rid_drop='CLK-18', drop_thr=20, rid_spike='CLK-19', spike_thr=50),
            'FTDs': dict(series=self.lp_ftds, guid_series=self.guid_ftds, guid_floor=GUID_FLOOR['FTDs'],
                           rid_drop='FTD-18', drop_thr=20, rid_spike=None, spike_thr=None, floor=5, rid_complete='FTD-17'),
        }
        for metric, cfg in LP_CFG.items():
            s = cfg['series']; b = baseline(s); pct = pctchg(s, b)
            gser = cfg['guid_series']; gbase = baseline(gser); gpct = pctchg(gser, gbase)
            if cfg.get('rid_complete'):
                eligible_c = b >= cfg['floor']
                complete_trig = (pct <= -90) & eligible_c
                for lid in s.columns:
                    self.emit(cfg['rid_complete'], metric, 'Landing Page', f'lp_{lid}', complete_trig[lid], s[lid], b[lid], pct[lid], f"LP floor {cfg['floor']}/day met")
            for lid in s.columns:
                guid_cols = [g for g in self.guids_by_lp.get(lid, []) if g in gser.columns]
                lp_eligible = b[lid] > 0
                g_active = (gbase[guid_cols] >= cfg['guid_floor']).fillna(False) if guid_cols else pd.DataFrame(index=self.all_dates)
                # FIX 14 (closes finding #60 at LP level too - the identical bug existed here):
                # masked_share() masks the numerator by the same g_active mask as the denominator.
                if guid_cols:
                    g_drop_hit_raw = (gpct[guid_cols] <= -cfg['drop_thr']).fillna(False)
                    drop_share, _, n_active = masked_share(g_drop_hit_raw, g_active, min_eligible=3)
                else:
                    drop_share = pd.Series(np.nan, index=self.all_dates)
                    n_active = pd.Series(np.nan, index=self.all_dates)
                drop_trig = (pct[lid] <= -cfg['drop_thr']) & (drop_share.fillna(0) >= 0.65) & lp_eligible
                self.emit(cfg['rid_drop'], metric, 'Landing Page', f'lp_{lid}', drop_trig, s[lid], b[lid], pct[lid], '>=65% of its (real-volume) GUIDs also moved - FIX 4 applied at LP level too')
                if cfg.get('rid_spike'):
                    if guid_cols:
                        g_spike_hit_raw = (gpct[guid_cols] >= cfg['spike_thr']).fillna(False)
                        spike_share, _, _ = masked_share(g_spike_hit_raw, g_active, min_eligible=3)
                    else:
                        spike_share = pd.Series(np.nan, index=self.all_dates)
                    spike_trig = (pct[lid] >= cfg['spike_thr']) & (spike_share.fillna(0) >= 0.65) & lp_eligible
                    self.emit(cfg['rid_spike'], metric, 'Landing Page', f'lp_{lid}', spike_trig, s[lid], b[lid], pct[lid], '>=65% of its (real-volume) GUIDs also moved - FIX 4 applied at LP level too')

    # ------------------------------------------------------------- CONVERSION: C2R / R2F
    def run_conversion(self):
        # C2R project
        c2r_today, c2r_base, c2r_pct = ratio_sum_baseline(self.proj_regs, self.proj_clicks)
        guard = denom_guard(self.proj_clicks, 10)
        vg = volatility_guard(c2r_today, c2r_base, self.proj_clicks)
        self.emit('C2R-01', 'Click-to-Reg', 'Project', 'Project', (c2r_pct <= -15) & guard & vg, c2r_today, c2r_base, c2r_pct, 'Clicks within +/-10%; vs own 21d volatility')
        self.emit('C2R-02', 'Click-to-Reg', 'Project', 'Project', (c2r_pct >= 45) & guard & vg, c2r_today, c2r_base, c2r_pct, 'Clicks within +/-10%; vs own 21d volatility')

        C2R_TT = {'Push/Pop': (25, 'C2R-03', 35, 'C2R-07'), 'Retargeting': (22, 'C2R-04', 35, 'C2R-08'),
                  'SEO': (35, 'C2R-05', 50, 'C2R-09'), 'PPC': (55, 'C2R-06', 90, 'C2R-10')}
        for tt, (dthr, drid, sthr, srid) in C2R_TT.items():
            if tt not in self.tt_regs.columns or tt not in self.tt_clicks.columns:
                continue
            today, base, pct = ratio_sum_baseline(self.tt_regs[tt], self.tt_clicks[tt])
            g = denom_guard(self.tt_clicks[tt], 20)
            floor_ok = baseline(self.tt_regs[tt]) >= 20
            vg = volatility_guard(today, base, self.tt_clicks[tt])
            self.emit(drid, f'Click-to-Reg ({tt})', 'Project/Traffic', tt, (pct <= -dthr) & g & floor_ok & vg, today, base, pct, 'Clicks +/-20%, type avg >=20 regs/day; vs own 21d volatility')
            self.emit(srid, f'Click-to-Reg ({tt})', 'Project/Traffic', tt, (pct >= sthr) & g & floor_ok & vg, today, base, pct, 'Clicks +/-20%, type avg >=20 regs/day; vs own 21d volatility')

        c2r_partner_today, c2r_partner_base, c2r_partner_pct = ratio_sum_baseline(self.partner_regs, self.partner_clicks)
        for pid in self.partner_clicks.columns:
            g = denom_guard(self.partner_clicks[pid], 20)
            reg_floor_ok = baseline(self.partner_regs[pid]) >= 50
            vg = volatility_guard(c2r_partner_today[pid], c2r_partner_base[pid], self.partner_clicks[pid])
            trig = (c2r_partner_pct[pid] <= -20) & g & reg_floor_ok & vg
            self.emit('C2R-11', 'Click-to-Reg', 'Partner', f'partner_{pid}', trig, c2r_partner_today[pid], c2r_partner_base[pid], c2r_partner_pct[pid], 'partner avg >=50 regs/day; vs own 21d volatility')

        c2r_guid_today, c2r_guid_base, c2r_guid_pct = ratio_sum_baseline(self.guid_regs, self.guid_clicks)
        for gid in self.guid_clicks.columns:
            g = denom_guard(self.guid_clicks[gid], 20)
            reg_floor_ok = baseline(self.guid_regs[gid]) >= 10
            vg = volatility_guard(c2r_guid_today[gid], c2r_guid_base[gid], self.guid_clicks[gid])
            for rid, trig in [('C2R-12', (c2r_guid_pct[gid] <= -20) & g & reg_floor_ok & vg),
                               ('C2R-13', (c2r_guid_pct[gid] >= 50) & g & reg_floor_ok & vg)]:
                self.emit(rid, 'Click-to-Reg', 'GUID', f'guid_{gid}', trig, c2r_guid_today[gid], c2r_guid_base[gid], c2r_guid_pct[gid], 'GUID avg >=10 regs/day; vs own 21d volatility')

        c2r_lp_today, c2r_lp_base, c2r_lp_pct = ratio_sum_baseline(self.lp_regs, self.lp_clicks)
        for lid in self.lp_clicks.columns:
            g = denom_guard(self.lp_clicks[lid], 20)
            floor_ok = (baseline(self.lp_clicks[lid]) >= 100) & (baseline(self.lp_regs[lid]) >= 20)
            vg = volatility_guard(c2r_lp_today[lid], c2r_lp_base[lid], self.lp_clicks[lid])
            trig = (c2r_lp_pct[lid] <= -20) & g & floor_ok & vg
            self.emit('C2R-14', 'Click-to-Reg', 'Landing Page', f'lp_{lid}', trig, c2r_lp_today[lid], c2r_lp_base[lid], c2r_lp_pct[lid], 'LP floor met; vs own 21d volatility')

        # R2F project
        r2f_today, r2f_base, r2f_pct = ratio_sum_baseline(self.proj_ftds, self.proj_regs)
        guard = denom_guard(self.proj_regs, 10)
        vg = volatility_guard(r2f_today, r2f_base, self.proj_regs)
        self.emit('R2F-01', 'Reg-to-FTD', 'Project', 'Project', (r2f_pct <= -20) & guard & vg, r2f_today, r2f_base, r2f_pct, 'Regs within +/-10%; vs own 21d volatility')
        self.emit('R2F-02', 'Reg-to-FTD', 'Project', 'Project', (r2f_pct >= 20) & guard & vg, r2f_today, r2f_base, r2f_pct, 'Regs within +/-10%; vs own 21d volatility')

        R2F_TT = {'Push/Pop': (35, 'R2F-03', 50, 'R2F-07'), 'Retargeting': (25, 'R2F-04', 35, 'R2F-08'),
                  'SEO': (32, 'R2F-05', 45, 'R2F-09'), 'PPC': (55, 'R2F-06', 80, 'R2F-10')}
        for tt, (dthr, drid, sthr, srid) in R2F_TT.items():
            if tt not in self.tt_regs.columns or tt not in self.tt_ftds.columns:
                continue
            today, base, pct = ratio_sum_baseline(self.tt_ftds[tt], self.tt_regs[tt])
            g = denom_guard(self.tt_regs[tt], 20)
            floor_ok = (baseline(self.tt_regs[tt]) >= 50) & (baseline(self.tt_ftds[tt]) >= 10)
            vg = volatility_guard(today, base, self.tt_regs[tt])
            self.emit(drid, f'Reg-to-FTD ({tt})', 'Project/Traffic', tt, (pct <= -dthr) & g & floor_ok & vg, today, base, pct, 'Regs +/-20%, type avg >=50 regs & >=10 FTDs/day; vs own 21d volatility')
            self.emit(srid, f'Reg-to-FTD ({tt})', 'Project/Traffic', tt, (pct >= sthr) & g & floor_ok & vg, today, base, pct, 'Regs +/-20%, type avg >=50 regs & >=10 FTDs/day; vs own 21d volatility')

        r2f_partner_today, r2f_partner_base, r2f_partner_pct = ratio_sum_baseline(self.partner_ftds, self.partner_regs)
        for pid in self.partner_regs.columns:
            g10 = denom_guard(self.partner_regs[pid], 10); g20 = denom_guard(self.partner_regs[pid], 20)
            floor_ok = (baseline(self.partner_regs[pid]) >= 50) & (baseline(self.partner_ftds[pid]) >= 10)
            vg = volatility_guard(r2f_partner_today[pid], r2f_partner_base[pid], self.partner_regs[pid])
            self.emit('R2F-11', 'Reg-to-FTD', 'Partner', f'partner_{pid}', (r2f_partner_pct[pid] <= -20) & g10 & floor_ok & vg, r2f_partner_today[pid], r2f_partner_base[pid], r2f_partner_pct[pid], 'partner floor met; vs own 21d volatility')
            self.emit('R2F-12', 'Reg-to-FTD', 'Partner', f'partner_{pid}', (r2f_partner_pct[pid] >= 50) & g20 & floor_ok & vg, r2f_partner_today[pid], r2f_partner_base[pid], r2f_partner_pct[pid], 'possible CPA-gaming - manual confirm; vs own 21d volatility')

        r2f_guid_today, r2f_guid_base, r2f_guid_pct = ratio_sum_baseline(self.guid_ftds, self.guid_regs)
        for gid in self.guid_regs.columns:
            g = denom_guard(self.guid_regs[gid], 20)
            floor_ok = (baseline(self.guid_regs[gid]) >= 20) & (baseline(self.guid_ftds[gid]) >= 3)
            vg = volatility_guard(r2f_guid_today[gid], r2f_guid_base[gid], self.guid_regs[gid])
            self.emit('R2F-13', 'Reg-to-FTD', 'GUID', f'guid_{gid}', (r2f_guid_pct[gid] <= -20) & g & floor_ok & vg, r2f_guid_today[gid], r2f_guid_base[gid], r2f_guid_pct[gid], 'GUID floor met; vs own 21d volatility')

        r2f_lp_today, r2f_lp_base, r2f_lp_pct = ratio_sum_baseline(self.lp_ftds, self.lp_regs)
        for lid in self.lp_regs.columns:
            g = denom_guard(self.lp_regs[lid], 20)
            floor_ok = (baseline(self.lp_regs[lid]) >= 20) & (baseline(self.lp_ftds[lid]) >= 3)
            vg = volatility_guard(r2f_lp_today[lid], r2f_lp_base[lid], self.lp_regs[lid])
            self.emit('R2F-14', 'Reg-to-FTD', 'Landing Page', f'lp_{lid}', (r2f_lp_pct[lid] <= -20) & g & floor_ok & vg, r2f_lp_today[lid], r2f_lp_base[lid], r2f_lp_pct[lid], 'LP floor met; vs own 21d volatility')

    # ------------------------------------------------------------- ZERO-CONVERSION DESPITE REAL VOLUME (Fix 2026-11-09)
    def run_zero_conversion(self):
        """R2F-15/C2R-15 (Partner level) - closes AUDIT_HISTORY.md Critical finding #2.
        R2F-11/12's own eligibility floor (baseline(partner_ftds) >= 10) and C2R-11's
        (baseline(partner_regs) >= 50) can mathematically never be satisfied by a partner
        whose real FTDs/regs are zero - so the existing ratio rules are structurally blind to
        exactly the "real traffic, zero conversion" pattern BUSINESS_CONTEXT.md names as the
        highest-priority fraud signal (the partner_5164 case it cites by name). Confirmed on 6
        real, currently-open partners, see reports/audit_missed_patterns_2026-11-06.md finding 1.

        Gated on the COMPLEMENT of each existing rule's own floor, so these can only fire
        exactly where R2F-11/C2R-11 are structurally unreachable - no day should ever see both
        rules fire for the same partner (design proposal
        reports/design_zero_ftd_rule_2026-11-06.md section 4; the proposal flags this as
        HYPOTHESIS, not re-verified against a live floor-crossing day, since none of the 6
        known examples crossed the floor within the validated window - they all started the
        window already zero).

        Floor/N values (regs>=5/day OR clicks>=100/day for >=3 days with 0 FTDs; clicks>=500/day
        for >=5 days with 0 regs) are the proposal's best read of one real 36-day window (9
        candidates tested, 100% recall on all 6 known real examples) - not validated the way
        Project 3's original thresholds were against a full season. Revisit if live monitoring
        shows these too chatty (proposal section 2 flags 8 "temporary conversion lull on an
        otherwise-converting partner" cases as a real but lower-severity situation the WARNING
        tier already separates from the CRITICAL "never converted" tier) or too quiet.

        Severity uses the engine's normal run-length TIERS mechanism (WARNING at N, CRITICAL at
        2N via the R2F-15/C2R-15 TIERS entries) rather than the proposal's alternate
        "CRITICAL if cumulative FTDs/regs for the whole window-to-date are still 0" path -
        simpler, consistent with every other rule in this file (no rule here computes severity
        any other way), and produces the identical result for the proposal's own worked example
        (partner_5164: WARNING day 3, CRITICAL day 6, zero cumulative FTDs either way since it
        never converted in the validated window). Revisit only if live data surfaces a partner
        whose clicks/regs cross the floor late enough that run-length and cumulative-zero
        genuinely disagree.

        NOT implemented here, by explicit proposal scope: GUID level (R2F-16/C2R-16) -
        INVESTIGATE-only, untested for noise with GUID-under-Partner suppression applied (would
        need its own validation pass, proposal section 2 last bullet / section 6 item 2);
        Landing-Page level (R2F-14/C2R-14 already exist but share the identical structural gap)
        - out of scope per the proposal's own framing (section 4 last bullet / section 6 item 6);
        a brand-new-partner grace period - needs a new, unbudgeted "earliest-ever activity"
        query this pass doesn't run (proposal section 6 item 4), so a partner newly ramping up
        can hit CRITICAL on day 6 same as a long-chronic one."""
        r2f_floor_ok = baseline(self.partner_ftds) < 10
        r2f_trig = ((self.partner_regs >= 5) | (self.partner_clicks >= 100)) & (self.partner_ftds == 0) & r2f_floor_ok
        nan_series = pd.Series(np.nan, index=self.all_dates)
        for pid in self.partner_ftds.columns:
            self.emit('R2F-15', 'FTDs', 'Partner', f'partner_{pid}', r2f_trig[pid],
                      self.partner_ftds[pid], self.partner_clicks[pid], nan_series,
                      "partner regs>=5/day or clicks>=100/day, 0 FTDs, baseline FTDs<10 "
                      "(R2F-11 structurally unreachable) - 'baseline' column here holds today's "
                      "clicks, not a historical average, for Telegram display context")

        c2r_floor_ok = baseline(self.partner_regs) < 50
        c2r_trig = (self.partner_clicks >= 500) & (self.partner_regs == 0) & c2r_floor_ok
        for pid in self.partner_regs.columns:
            self.emit('C2R-15', 'Registrations', 'Partner', f'partner_{pid}', c2r_trig[pid],
                      self.partner_regs[pid], self.partner_clicks[pid], nan_series,
                      "partner clicks>=500/day, 0 registrations, baseline regs<50 "
                      "(C2R-11 structurally unreachable) - 'baseline' column here holds today's "
                      "clicks, not a historical average, for Telegram display context")

    # ------------------------------------------------------------- QUALITY: engagement, redeposit
    def run_quality(self):
        eng_today, eng_base, eng_pct = ratio_sum_baseline(self.proj_bets, self.proj_deposits)
        self.emit('ENG-01', 'Engagement (Bets/Deposits)', 'Project', 'Project', eng_pct <= -15, eng_today, eng_base, eng_pct, '')
        self.emit('ENG-02', 'Engagement (Bets/Deposits)', 'Project', 'Project', eng_pct >= 30, eng_today, eng_base, eng_pct, '')

        # STD: share of FTDs (from the week starting 14 days before eval, ending 7 days before) that
        # redeposited within 7 days of their FTD, this completed week vs the previous completed week.
        # finding #112 (Tier 3, spec cross-check): two gaps vs the spec's own STD-01/02 rows -
        #  (a) STD-01's Data Level is "Project / Traffic, Each type" (like the CLK/REG/FTD-04..11
        #      traffic-type tier) - this used to only ever compute the Project-wide rate, with no
        #      per-traffic-type breakdown at all.
        #  (b) STD-01's Alert-CRITICAL is "2nd consecutive week" and STD-02's own Duration is
        #      "2 consecutive weeks" (STD-02 has NO single-week WARNING at all) - this used to
        #      fire STD-01 as WARNING-only (no critical escalation path) and fire STD-02 off a
        #      single week's reading, with no 2-week confirmation requirement at all. Both are
        #      fixed below by also evaluating the ONE-WEEK-EARLIER week pair inline (no persisted
        #      state needed - a "2nd consecutive week" reading is just "this week's own pair AND
        #      last week's own pair both independently qualify", computable in one run).
        fdp = self.ps[self.ps['is_first_depositor'] == 1][['date', 'customer_id', 'partner_id', 'promo_id']].drop_duplicates()
        fdp = fdp.merge(self.promo_map[['promo_id', 'traffic_type']], on='promo_id', how='left')
        deposits = self.ps[self.ps['payments'] > 0][['date', 'customer_id']]

        def redeposit_rate(fdp_week, all_deposits):
            if fdp_week.empty:
                return np.nan, 0
            rates = []
            for _, row in fdp_week.iterrows():
                window = all_deposits[(all_deposits['customer_id'] == row['customer_id']) &
                                       (all_deposits['date'] > row['date']) &
                                       (all_deposits['date'] <= row['date'] + pd.Timedelta(days=7))]
                rates.append(1 if len(window) > 0 else 0)
            return (sum(rates) / len(rates) if rates else np.nan), len(rates)

        def week_pair(fdp_pool, this_end):
            """One completed-week-vs-prior-week redeposit-rate comparison, ending at this_end."""
            this_start = this_end - pd.Timedelta(days=6)
            prev_end = this_start - pd.Timedelta(days=1)
            prev_start = prev_end - pd.Timedelta(days=6)
            fdp_this = fdp_pool[(fdp_pool['date'] >= this_start) & (fdp_pool['date'] <= this_end)]
            fdp_prev = fdp_pool[(fdp_pool['date'] >= prev_start) & (fdp_pool['date'] <= prev_end)]
            rate_this, n_this = redeposit_rate(fdp_this, deposits)
            rate_prev, n_prev = redeposit_rate(fdp_prev, deposits)
            return rate_this, n_this, rate_prev, fdp_this, this_start

        def is_drop(rate_this, rate_prev):
            if not (pd.notna(rate_this) and pd.notna(rate_prev) and rate_prev > 0):
                return False, None
            rel_chg = (rate_this - rate_prev) / rate_prev * 100
            return rel_chg <= -25, rel_chg

        std_end = self.eval_date - pd.Timedelta(days=7)
        groups = [('Project', 'Project', fdp)] + [
            ('Project/Traffic', tt, fdp[fdp['traffic_type'] == tt]) for tt in TRAFFIC_TYPES
        ]
        for level, entity, fdp_pool in groups:
            rate_this, n_this, rate_prev, fdp_this, this_start = week_pair(fdp_pool, std_end)
            rate_prior_this, _, rate_prior_prev, _, _ = week_pair(fdp_pool, std_end - pd.Timedelta(days=7))
            dropped, rel_chg = is_drop(rate_this, rate_prev)
            if dropped:
                prior_dropped, _ = is_drop(rate_prior_this, rate_prior_prev)
                severity = 'CRITICAL' if prior_dropped else 'WARNING'
                # FIX (2026-11-12, health-check-auditor finding, general audit 2026-11-12):
                # every OTHER Project/Traffic-tier rule (CLK/REG/FTD-04..11, run_traffic_type()
                # above) embeds its own traffic_type into the `metric` string itself
                # (f'{metric} ({tt})') precisely because entity_label()/_alert_line() show no
                # separate entity text for 'Project/Traffic' rows - the metric string IS the
                # only place that context can appear. STD-01 never did this, so its
                # Project/Traffic rows rendered as plain "Returning-player rate: ..." with no
                # indication which traffic type fired - confirmed live: today's real CRITICAL
                # Brazino777.ar alert gave zero hint it was Push/Pop-specific. Same fix, same
                # pattern, now applied here too. STD-02 does not need this - it is always
                # Partner-level (gated to the Project-wide `level=='Project'` pool below, never
                # Project/Traffic), so it has no traffic-type dimension to mislabel.
                metric_label = f'FTD-to-STD ({entity})' if level == 'Project/Traffic' else 'FTD-to-STD'
                self.results.append({'date': self.eval_date, 'rule_id': 'STD-01', 'metric': metric_label,
                                      'level': level, 'entity': entity, 'value': round(rate_this * 100, 1),
                                      'baseline': round(rate_prev * 100, 1), 'pct': round(rel_chg, 1),
                                      'severity': severity,
                                      'note': f'week of {this_start.date()}, n={n_this} FTDs' + (' - 2nd consecutive week' if severity == 'CRITICAL' else '')})

            # partner-level (STD-02): partner redeposit rate vs the SAME pool's market rate,
            # needs >=30 FTDs that week AND the prior week's own reading to also qualify (spec:
            # "2 consecutive weeks", WARNING-only, no single-week alert at all).
            #
            # finding (2026-11-11 general audit, confirmed live): this block must stay
            # INDEPENDENT of whether STD-01 itself fired this week (no longer gated by a
            # `continue` tied to STD-01's own `dropped` check, as the 2026-11-10 refactor
            # accidentally did). STD-02 exists specifically to catch a partner
            # underperforming a market rate that is otherwise STABLE - exactly the case
            # where STD-01 does NOT fire. Confirmed this silenced a real case:
            # partner_10916 (Project 3) had a 6.6%/5.2% redeposit rate across two
            # consecutive weeks in Aug 2026 against a steady 31.3%/28.8% project rate
            # (>=30 FTDs both weeks, both sides) - STD-01 never fired either week (the
            # project rate itself never dropped 25%), so the old nested-`continue`
            # structure meant STD-02 was never even evaluated for either week.
            if level == 'Project' and n_this >= 1:
                for pid, grp in fdp_this.groupby('partner_id'):
                    if len(grp) < 30:
                        continue
                    p_rate, _ = redeposit_rate(grp, deposits)
                    if not (pd.notna(p_rate) and rate_this > 0 and p_rate < 0.5 * rate_this):
                        continue
                    grp_prior = fdp[(fdp['partner_id'] == pid) &
                                    (fdp['date'] >= this_start - pd.Timedelta(days=7)) &
                                    (fdp['date'] <= this_start - pd.Timedelta(days=1))]
                    if len(grp_prior) < 30:
                        continue
                    p_rate_prior, _ = redeposit_rate(grp_prior, deposits)
                    if not (pd.notna(p_rate_prior) and pd.notna(rate_prior_this) and rate_prior_this > 0 and p_rate_prior < 0.5 * rate_prior_this):
                        continue
                    self.results.append({'date': self.eval_date, 'rule_id': 'STD-02', 'metric': 'FTD-to-STD',
                                          'level': 'Partner', 'entity': f'partner_{pid}', 'value': round(p_rate * 100, 1),
                                          'baseline': round(rate_this * 100, 1), 'pct': round((p_rate - rate_this) / rate_this * 100, 1),
                                          'severity': 'WARNING', 'note': f'partner rate <50% of market rate for 2 consecutive weeks, n={len(grp)} FTDs this week'})

    # ------------------------------------------------------------- RETENTION (RET-01/02)
    # FIX (2026-11-13, finding #149): "retention" was undefined - the spec template's own
    # placeholder said so, and this engine's module docstring listed RET-01/02 as NOT SIMULATED
    # for exactly that reason. User resolved it directly: "retention is on depositors" - cohort
    # = everyone who deposited in period N (not FTDs only, not all registrants), retained =
    # deposited again in the immediately following period N+1. Same completed-period-vs-prior-
    # period, 2-consecutive-period escalation shape as STD-01/02 above (bypasses emit()/TIERS,
    # appends directly to self.results) - both need the FOLLOWING period to be fully complete
    # before retention can be judged, so both lag by one full period behind eval_date.
    def run_retention(self):
        deposits = self.ps[self.ps['payments'] > 0][['date', 'customer_id']].drop_duplicates()

        def customers_in(pool, start, end):
            return set(pool[(pool['date'] >= start) & (pool['date'] <= end)]['customer_id'])

        def retention_rate(pool, cohort_end, days):
            cohort_start = cohort_end - pd.Timedelta(days=days - 1)
            base = customers_in(pool, cohort_start, cohort_end)
            if not base:
                return np.nan, 0
            next_start = cohort_end + pd.Timedelta(days=1)
            next_end = cohort_end + pd.Timedelta(days=days)
            nxt = customers_in(pool, next_start, next_end)
            return len(base & nxt) / len(base), len(base)

        def is_drop(rate_this, rate_prev, threshold_pct):
            if not (pd.notna(rate_this) and pd.notna(rate_prev) and rate_prev > 0):
                return False, None
            rel_chg = (rate_this - rate_prev) / rate_prev * 100
            return rel_chg <= -threshold_pct, rel_chg

        # RET-01: D7 retention, weekly cohort. Needs ~21 days back from eval_date (cohort week +
        # its own completion week + one more prior pair for the 2-consecutive-week check) - well
        # within this engine's own BUFFER_DAYS=35 pull, no extra query needed.
        d7_end = self.eval_date - pd.Timedelta(days=7)  # cohort week's own next 7 days must be complete
        rate_this, n_this = retention_rate(deposits, d7_end, 7)
        rate_prev, _ = retention_rate(deposits, d7_end - pd.Timedelta(days=7), 7)
        dropped, rel_chg = is_drop(rate_this, rate_prev, 20)
        if dropped:
            rate_prior_prev, _ = retention_rate(deposits, d7_end - pd.Timedelta(days=14), 7)
            prior_dropped, _ = is_drop(rate_prev, rate_prior_prev, 20)
            severity = 'CRITICAL' if prior_dropped else 'WARNING'
            self.results.append({'date': self.eval_date, 'rule_id': 'RET-01', 'metric': 'D7 retention',
                                  'level': 'Project', 'entity': 'Project', 'value': round(rate_this * 100, 1),
                                  'baseline': round(rate_prev * 100, 1), 'pct': round(rel_chg, 1), 'severity': severity,
                                  'note': f'cohort week ending {d7_end.date()}, n={n_this} depositors' +
                                          (' - 2nd consecutive week' if severity == 'CRITICAL' else '')})

        # RET-02: D30 retention, monthly cohort. The 2-consecutive-month check alone needs up to
        # ~120 days back (this month's cohort + completion + the prior month's own pair) - well
        # beyond BUFFER_DAYS=35, so this pulls its own wider, minimal (customer_id, date,
        # payments only) query rather than widening every other rule's own daily pull.
        d30_end = self.eval_date - pd.Timedelta(days=30)
        wide_start = (d30_end - pd.Timedelta(days=65)).strftime('%Y-%m-%d')
        wide_end = self.eval_date.strftime('%Y-%m-%d')
        wide = replica_run_query(f"""
            SELECT date, customer_id FROM projects_statistics
            WHERE project_id = {int(self.project_id)} AND date >= '{wide_start}' AND date <= '{wide_end}'
              AND payments > 0
        """)
        wide['date'] = pd.to_datetime(wide['date'])
        wide_deposits = wide[['date', 'customer_id']].drop_duplicates()

        rate_this30, n_this30 = retention_rate(wide_deposits, d30_end, 30)
        rate_prev30, _ = retention_rate(wide_deposits, d30_end - pd.Timedelta(days=30), 30)
        dropped30, rel_chg30 = is_drop(rate_this30, rate_prev30, 15)
        if dropped30:
            rate_prior_prev30, _ = retention_rate(wide_deposits, d30_end - pd.Timedelta(days=60), 30)
            prior_dropped30, _ = is_drop(rate_prev30, rate_prior_prev30, 15)
            severity = 'CRITICAL' if prior_dropped30 else 'WARNING'
            self.results.append({'date': self.eval_date, 'rule_id': 'RET-02', 'metric': 'D30 retention',
                                  'level': 'Project', 'entity': 'Project', 'value': round(rate_this30 * 100, 1),
                                  'baseline': round(rate_prev30 * 100, 1), 'pct': round(rel_chg30, 1), 'severity': severity,
                                  'note': f'cohort month ending {d30_end.date()}, n={n_this30} depositors' +
                                          (' - 2nd consecutive month' if severity == 'CRITICAL' else '')})

    # ------------------------------------------------------------- FINANCE
    def run_finance(self):
        ps = self.ps
        ftd_today, ftd_base, ftd_pct = ratio_sum_baseline(self.proj_ftds, pd.Series(1, index=self.all_dates))  # unused placeholder guard
        ftd_guard = denom_guard(self.proj_ftds, 10)

        b = baseline(self.proj_fdp_amt); pct = pctchg(self.proj_fdp_amt, b)
        self.emit('FDP-01', 'FTD Deposits $', 'Project', 'Project', (pct <= -90) & ftd_guard, self.proj_fdp_amt, b, pct, 'FTD count within +/-10%')
        self.emit('FDP-02', 'FTD Deposits $', 'Project', 'Project', (pct <= -15) & ftd_guard, self.proj_fdp_amt, b, pct, 'FTD count within +/-10%')
        self.emit('FDP-03', 'FTD Deposits $', 'Project', 'Project', (pct >= 30) & ftd_guard, self.proj_fdp_amt, b, pct, 'FTD count within +/-10%')

        partner_fdp_amt = ps[ps['is_first_depositor'] == 1].groupby(['date', 'partner_id'])['first_deposit_amount'].sum().unstack(fill_value=0).reindex(self.all_dates, fill_value=0)
        ftd_floor_partner = baseline(self.partner_ftds) >= 10
        for pid in partner_fdp_amt.columns:
            if pid not in self.partner_ftds.columns or pid not in ftd_floor_partner.columns:
                continue
            afd_today, afd_base, afd_pct = ratio_sum_baseline(partner_fdp_amt[pid], self.partner_ftds[pid], scale=1)
            trig = (afd_pct <= -30) & ftd_floor_partner[pid]
            self.emit('AFD-01', 'Average first deposit', 'Partner', f'partner_{pid}', trig, afd_today, afd_base, afd_pct, 'partner avg >=10 FTDs/day, sum-weighted 7d baseline')

        fbt_today, fbt_base, fbt_pct = ratio_sum_baseline(self.proj_fdp_bets, self.proj_ftds, scale=1)
        self.emit('FBT-01', 'FTD Bets/FTDs', 'Project', 'Project', (fbt_pct <= -20) & ftd_guard, fbt_today, fbt_base, fbt_pct, 'FTD count within +/-10%, sum-weighted 7d baseline')

        fbn_today, fbn_base, fbn_pct = ratio_sum_baseline(self.proj_fdp_bonuses, self.proj_fdp_amt)
        self.emit('FBN-01', 'FTD Bonuses/FTD Deposits', 'Project', 'Project', fbn_pct >= 30, fbn_today, fbn_base, fbn_pct, '')

        fdp_ggr_7d = (self.proj_fdp_bets - self.proj_fdp_wins).rolling(7).sum()
        for d in self.all_dates:
            if pd.notna(fdp_ggr_7d.loc[d]) and fdp_ggr_7d.loc[d] <= 0 and self.proj_ftds.rolling(7).sum().loc[d] > 0:
                self.results.append({'date': d, 'rule_id': 'FGR-01', 'metric': 'FTD GGR', 'level': 'Project',
                                      'entity': 'Project', 'value': round(float(fdp_ggr_7d.loc[d]), 2), 'baseline': 0,
                                      'pct': None, 'severity': 'WARNING', 'note': 'trailing 7-day sum <= 0'})

        dep_b = baseline(self.proj_deposits); dep_pct = pctchg(self.proj_deposits, dep_b)
        depositor_guard = denom_guard(self.proj_depositors, 10)
        self.emit('DPS-01', 'Deposits $', 'Project', 'Project', dep_pct <= -90, self.proj_deposits, dep_b, dep_pct, '')
        self.emit('DPS-02', 'Deposits $', 'Project', 'Project', (dep_pct <= -15) & depositor_guard, self.proj_deposits, dep_b, dep_pct, 'Depositors within +/-10%')
        self.emit('DPS-03', 'Deposits $', 'Project', 'Project', (dep_pct >= 30) & depositor_guard, self.proj_deposits, dep_b, dep_pct, 'Depositors within +/-10%')

        # finding #119 (2026-11-11, bets/withdrawal-spike investigation): total bets $ was never
        # watched as its own metric anywhere in this file - only as a ratio denominator (ENG,
        # RTP, BAB) or a guard other rules check before firing (bets_guard/bets_guard_10 below).
        # Confirmed real gap: project 6 (Brazino777.ar) bets moved +50.6% vs. its own 7-day
        # baseline on 2026-11-10 ($37,227.68 vs. $24,711.52) and produced zero alert at any
        # level or rule. Same shape as DPS-01/02/03 (the equivalent rule for deposit $ volume),
        # same depositor_guard reused unchanged - the reasoning is identical: a $ move should
        # only count as a real signal while the underlying player base looks normal, not when
        # it's just fewer/more people showing up. Thresholds mirror DPS-01/02/03's own
        # (-90/-15/+30) as a starting point, not independently re-validated against this
        # project's real bets history - revisit if live monitoring shows these too chatty or
        # quiet, same as every other threshold in this file that started life this way.
        # finding #124 (2026-11-12, caught before publishing this build's doc update): this
        # family was first shipped as 'BET-01/02/03' - a direct, unnoticed collision with the
        # REAL spec's own BET-01 (module docstring line 8: a 2-hour no-bet-event intraday
        # outage check, confirmed in AUDIT_HISTORY.md's 2026-11-10 spec cross-check - a
        # completely different rule from this one). Renamed to WAG-01/02/03 ("wagered $") before
        # it ever shipped to an AM - caught by re-reading this file's own docstring while
        # updating the reference doc, not by any test (nothing here would have detected a
        # same-ID, different-meaning collision; both are just dict keys to the engine itself).
        bet_b = baseline(self.proj_bets); bet_pct = pctchg(self.proj_bets, bet_b)
        self.emit('WAG-01', 'Bets $', 'Project', 'Project', bet_pct <= -90, self.proj_bets, bet_b, bet_pct, '')
        self.emit('WAG-02', 'Bets $', 'Project', 'Project', (bet_pct <= -15) & depositor_guard, self.proj_bets, bet_b, bet_pct, 'Depositors within +/-10%')
        self.emit('WAG-03', 'Bets $', 'Project', 'Project', (bet_pct >= 30) & depositor_guard, self.proj_bets, bet_b, bet_pct, 'Depositors within +/-10%')

        # finding #110 (Tier 3, spec cross-check): DEP-02/DEP-03 - distinct-DEPOSITOR-count
        # rules, a different signal from DPS-01/02/03's deposit DOLLAR amount above (a partner
        # could keep its usual depositor count while the $ amount per depositor craters, or vice
        # versa - this is exactly why the spec gives them separate rule_ids). Thresholds are the
        # spec's own numbers (Project_Health_Monitoring_Framework_FINAL_v2.xlsx rows 59-60), not
        # independently re-validated against this project's real history the way Project 3's
        # original thresholds were - revisit if live monitoring shows these too chatty or quiet.
        dep_ct_b = baseline(self.proj_depositors); dep_ct_pct = pctchg(self.proj_depositors, dep_ct_b)
        self.emit('DEP-03', 'Depositors', 'Project', 'Project', dep_ct_pct <= -20, self.proj_depositors, dep_ct_b, dep_ct_pct, '')

        dep_this7 = self.proj_depositors.rolling(7).sum()
        dep_prev7 = self.proj_depositors.shift(7).rolling(7).sum()
        dep_wk_pct = pctchg(dep_this7, dep_prev7)
        p_this7 = self.partner_depositors.rolling(7).sum()
        p_prev7 = self.partner_depositors.shift(7).rolling(7).sum()
        p_active_prev = p_prev7 > 0
        p_pct = pctchg(p_this7, p_prev7)
        p_down_raw = (p_pct <= -15).fillna(False)
        dep02_breadth, _, _ = masked_share(p_down_raw, p_active_prev, min_eligible=3)
        dep02_trig = (dep_wk_pct <= -15) & (dep02_breadth.fillna(0) >= 0.65)
        self.emit('DEP-02', 'Depositors', 'Project', 'Project', dep02_trig, dep_this7, dep_prev7, dep_wk_pct,
                   "trailing-7d vs prior-7d; >=65% of previously-active (prior-7d) partners also down >=15% - "
                   "spec's own breadth bar is 'most partners down', 65% reused from CLK/REG/FTD breadth rules "
                   "rather than inventing a different number for the same kind of check")

        bets_guard = denom_guard(self.proj_bets, 50)  # "bets within/approx normal" proxy guard
        # finding #105 (2026-11-10 spec cross-check): GGR-02/GGR-03 were reusing GGR-01's own
        # +/-50% bets_guard, but the spec's own guard text for these two is "Bets within +/-10%"
        # (not 50% - that's GGR-01's "complete collapse" band, a much looser check appropriate to
        # an absolute GGR<=0 threshold, not a relative +/-15%/+20% one). A real bets swing of e.g.
        # +40% would have passed the old 50% guard and let a pure bets-volume move masquerade as
        # a GGR-02/03 signal. Dedicated +/-10% guard, matching GGR-02's already-correct deposits
        # guard and GGR-03's own "Bets within +/-10%" text.
        bets_guard_10 = denom_guard(self.proj_bets, 10)
        ggr_b = baseline(self.proj_ggr); ggr_pct = pctchg(self.proj_ggr, ggr_b)
        self.emit('GGR-01', 'GGR', 'Project', 'Project', (self.proj_ggr <= 0) & bets_guard, self.proj_ggr, ggr_b, ggr_pct, 'Bets >=50% of 7d avg')
        self.emit('GGR-02', 'GGR', 'Project', 'Project', (ggr_pct <= -15) & dep_pct.abs().le(10) & bets_guard_10, self.proj_ggr, ggr_b, ggr_pct, 'Deposits & bets within +/-10%')
        self.emit('GGR-03', 'GGR', 'Project', 'Project', (ggr_pct >= 20) & bets_guard_10, self.proj_ggr, ggr_b, ggr_pct, 'Bets within +/-10%')

        # finding #120 (2026-11-11, same investigation): bets_guard_10 silently suppresses
        # GGR-02/03 (and, by the identical mechanism in run_product() below, RTP-01/02/03)
        # whenever bets itself is the thing moving - confirmed real and live: project 6 had GGR
        # swing hard on 2026-11-10, the same day bets moved +50.6%, but GGR-02/03 never fired
        # because bets_guard_10 failed, leaving zero trace that a real GGR move had even been
        # evaluated. Not loosening the guard (it exists specifically to stop a pure bets-volume
        # move masquerading as a GGR signal - finding #105) - just leaving a trace instead of a
        # silent drop, as an INFO-only row. Bypasses emit()/TIERS, same pattern as
        # FGR-01/NGG-01/BAB-01 above - this isn't a severity-escalating alert, just a flag that a
        # guard fired; INFO rows are already filtered out of Telegram by the existing
        # severity != 'INFO' rule, same as every other INFO-tier rule here, so this is visible in
        # the Excel audit trail only, by design.
        ggr_guard_blocked = ((ggr_pct <= -15) | (ggr_pct >= 20)) & ~bets_guard_10
        for d in self.all_dates:
            if not ggr_guard_blocked.loc[d]:
                continue
            self.results.append({
                'date': d, 'rule_id': 'GGR-04', 'metric': 'GGR', 'level': 'Project', 'entity': 'Project',
                'value': round(float(self.proj_ggr.loc[d]), 2),
                'baseline': round(float(ggr_b.loc[d]), 2) if pd.notna(ggr_b.loc[d]) else None,
                'pct': round(float(ggr_pct.loc[d]), 1) if pd.notna(ggr_pct.loc[d]) else None,
                'severity': 'INFO',
                'note': f"GGR-02/03 would otherwise have fired here but bets_guard_10 blocked it "
                        f"(bets moved {bet_pct.loc[d]:.1f}% vs. its own 7-day baseline, outside the "
                        f"+/-10% guard band)",
            })

        adm_b = baseline(self.proj_admin); adm_pct = pctchg(self.proj_admin, adm_b)
        dep_ggr_guard = dep_pct.abs().le(10) & ggr_pct.abs().le(10)
        # finding #106/#61 (spec cross-check): ADM-01/BON-01/WDR-01 are "complete drop near
        # zero" rules with the SAME shape as DPS-01/GGR-01 (one-day CRITICAL on an absolute
        # threshold) but, unlike those two, had no volume guard at all - a near-zero admin/bonus/
        # withdrawal day caused by genuinely low deposits/GGR/depositors that week would fire
        # exactly like a real feed outage. Spec's own guard text: ADM-01 "Deposits and GGR within
        # +/-10%" (dep_ggr_guard, already computed above for ADM-02/03); BON-01 "Deposits and
        # registrations normal" (no exact % given - using the project's own +/-10% "normal" band,
        # consistent with every other guard in this block, rather than inventing a different
        # number); WDR-01 "Depositing players normal" (depositor_guard, defined below for DPS-02/
        # 03 - same +/-10% convention, reused here unchanged).
        self.emit('ADM-01', 'Admin Fees', 'Project', 'Project', (adm_pct <= -90) & dep_ggr_guard, self.proj_admin, adm_b, adm_pct, 'Deposits & GGR within +/-10%')
        self.emit('ADM-02', 'Admin Fees', 'Project', 'Project', (adm_pct <= -10) & dep_ggr_guard, self.proj_admin, adm_b, adm_pct, 'Deposits & GGR +/-10% (approx)')
        self.emit('ADM-03', 'Admin Fees', 'Project', 'Project', (adm_pct >= 20) & dep_ggr_guard, self.proj_admin, adm_b, adm_pct, 'Deposits & GGR +/-10% (approx)')

        dep_reg_guard = dep_pct.abs().le(10) & denom_guard(self.proj_regs, 10)
        bon_b = baseline(self.proj_bonuses); bon_pct = pctchg(self.proj_bonuses, bon_b)
        self.emit('BON-01', 'Bonuses', 'Project', 'Project', (bon_pct <= -90) & dep_reg_guard, self.proj_bonuses, bon_b, bon_pct, 'Deposits & registrations within +/-10% ("normal" - spec gives no exact %, using this block\'s own convention)')
        bon_ratio_today, bon_ratio_base, bon_ratio_pct = ratio_sum_baseline(self.proj_bonuses, self.proj_deposits)
        self.emit('BON-02', 'Bonuses/Deposits', 'Project', 'Project', bon_ratio_pct >= 30, bon_ratio_today, bon_ratio_base, bon_ratio_pct, '')

        wdr_b = baseline(self.proj_payouts); wdr_pct = pctchg(self.proj_payouts, wdr_b)
        self.emit('WDR-01', 'Withdrawals ($ proxy)', 'Project', 'Project', (wdr_pct <= -90) & depositor_guard, self.proj_payouts, wdr_b, wdr_pct, 'Depositing players within +/-10% ("normal", depositor_guard reused from DPS-02/03); $ proxy for request count - see caveats')
        self.emit('WDR-02', 'Withdrawals ($ proxy)', 'Project', 'Project', wdr_pct >= 50, self.proj_payouts, wdr_b, wdr_pct, '$ proxy for request count - see caveats')

        ngg = self.proj_ngr / self.proj_ggr.replace(0, np.nan)
        ngg_today7 = ngg.rolling(7).mean(); ngg_prev7 = ngg.shift(7).rolling(7).mean()
        ngg_pct = (ngg_today7 - ngg_prev7) / ngg_prev7.replace(0, np.nan) * 100
        for d in self.all_dates:
            if pd.notna(ngg_pct.loc[d]) and ngg_pct.loc[d] <= -10:
                self.results.append({'date': d, 'rule_id': 'NGG-01', 'metric': 'NGR/GGR', 'level': 'Project',
                                      'entity': 'Project', 'value': round(float(ngg_today7.loc[d]), 3), 'baseline': round(float(ngg_prev7.loc[d]), 3),
                                      'pct': round(float(ngg_pct.loc[d]), 1), 'severity': 'WARNING', 'note': 'trailing-7d vs prior-7d (assumed NGR formula, see caveats)'})

        ngr_b = baseline(self.proj_ngr); ngr_pct = pctchg(self.proj_ngr, ngr_b)
        self.emit('NGR-01', 'NGR', 'Project', 'Project', (ngr_pct <= -20) & ggr_pct.abs().le(10), self.proj_ngr, ngr_b, ngr_pct, 'GGR within +/-10% (assumed NGR formula, see caveats)')
        # finding #108 (spec cross-check): NGR-01's spec'd Alert-CRITICAL is "NGR<=0 for 3
        # consecutive days" - an ABSOLUTE path, independent of the relative -20%-vs-baseline
        # WARNING path above (a chronically low-but-stable NGR could sit at NGR<=0 for weeks
        # without ever showing a fresh -20% baseline drop, since baseline() would already have
        # adapted to the new low level by day ~8). Can't be a second emit() call for the same
        # rule_id - the two trigger conditions aren't mutually exclusive, so both firing the same
        # day would double-emit a row via the normal per-call run-length mechanism. Computed
        # directly instead: upgrade that day's existing WARNING row to CRITICAL if one exists,
        # else append a fresh CRITICAL row - never duplicates a (date, rule_id, entity) key.
        ngr_neg_run = run_length_true(self.proj_ngr <= 0)
        for d in self.all_dates:
            if ngr_neg_run.loc[d] < 3:
                continue
            existing = [r for r in self.results if r['rule_id'] == 'NGR-01' and r['level'] == 'Project' and r['date'] == d]
            if existing:
                existing[0]['severity'] = 'CRITICAL'
                existing[0]['note'] = existing[0]['note'] + ' - NGR<=0 for >=3 consecutive days (absolute, spec-declared)'
            else:
                self.results.append({
                    'date': d, 'rule_id': 'NGR-01', 'metric': 'NGR', 'level': 'Project', 'entity': 'Project',
                    'value': round(float(self.proj_ngr.loc[d]), 2),
                    'baseline': round(float(ngr_b.loc[d]), 2) if pd.notna(ngr_b.loc[d]) else None,
                    'pct': round(float(ngr_pct.loc[d]), 1) if pd.notna(ngr_pct.loc[d]) else None,
                    'severity': 'CRITICAL', 'note': 'NGR<=0 for >=3 consecutive days (absolute, spec-declared)',
                })

    # ------------------------------------------------------------- ROAS (ROA-01)
    # FIX (2026-11-13, finding #149): "ROAS" was undefined - the spec template's own placeholder
    # said so. User resolved it directly: "roas = program profit / investment (cpa + listing)".
    # program_profit uses the SAME corrected per-partner-RS-floor methodology as
    # tqa_model/corrected_ngr_program_partner_investment.sql - NGR minus each partner's OWN RS
    # (partner_profit) floored at 0 before summing across partners, not the raw program_profit
    # column (that file's own comment: a straight sum lets one partner's negative-RS period net
    # against another's positive one) - applied at this rule's own weekly grain rather than that
    # file's monthly one. Investment = CPA (direct sum) + listing fee, apportioned from the
    # monthly-recurring promo_listing_fees table per the brazpartners-query-conventions skill's
    # own containment rule. Same "last 7 days vs the 7 days before", 2-consecutive-week
    # escalation shape as STD-01/RET-01 above - unlike those, ROAS needs no forward-looking
    # completion wait, so it's evaluated AT eval_date itself, not lagged a week behind it.
    def _listing_fee_for_range(self, start, end):
        """Listing fee apportioned to [start, end] inclusive. A promo_listing_fees row's
        `amount` is a monthly-recurring charge, counted in FULL for every calendar month its
        [effective_from, effective_to) range touches (never pro-rated within a month - see the
        brazpartners-query-conventions skill). Apportioning that to an arbitrary sub-range (here,
        one week) means: for each calendar month the range touches, take that month's own full
        fee total via the same containment query, then weight by what fraction of the range's
        days fall in that month."""
        total = 0.0
        month_starts = pd.date_range(start.replace(day=1), end, freq='MS')
        for month_start in month_starts:
            month_end = month_start + pd.offsets.MonthEnd(0)
            days_in_month = month_end.day
            overlap_start = max(start, month_start)
            overlap_end = min(end, month_end)
            days_in_range = (overlap_end - overlap_start).days + 1
            if days_in_range <= 0:
                continue
            next_month = month_start + pd.offsets.MonthBegin(1)
            row = replica_run_query(f"""
                SELECT COALESCE(SUM(plf.amount), 0) AS fee
                FROM promo_listing_fees plf
                JOIN (SELECT DISTINCT promo_id FROM projects_statistics WHERE project_id = {int(self.project_id)}) ppm
                  ON ppm.promo_id = plf.promo_id
                WHERE plf.effective_from < '{next_month.strftime('%Y-%m-%d')}'
                  AND (plf.effective_to IS NULL OR plf.effective_to >= '{month_start.strftime('%Y-%m-%d')}')
            """)
            month_fee = float(row['fee'].iloc[0]) if not row.empty else 0.0
            total += month_fee * (days_in_range / days_in_month)
        return total

    def run_roas(self):
        def window_program_profit(start, end):
            w = self.ps[(self.ps['date'] >= start) & (self.ps['date'] <= end)]
            ngr = w['total_profit'].sum()
            rs_floored = w.groupby('partner_id')['partner_profit'].sum().clip(lower=0).sum()
            return ngr - rs_floored

        def roas_for(end):
            start = end - pd.Timedelta(days=6)
            cpa = self.ps[(self.ps['date'] >= start) & (self.ps['date'] <= end)]['cpa_amount'].sum()
            listing = self._listing_fee_for_range(start, end)
            investment = cpa + listing
            pp = window_program_profit(start, end)
            roas = pp / investment if investment > 0 else np.nan
            return roas, investment, pp

        def is_drop(rate_this, rate_prev):
            if not (pd.notna(rate_this) and pd.notna(rate_prev) and rate_prev > 0):
                return False, None
            rel_chg = (rate_this - rate_prev) / rate_prev * 100
            return rel_chg <= -20, rel_chg

        roas_this, inv_this, pp_this = roas_for(self.eval_date)
        roas_prev, _, _ = roas_for(self.eval_date - pd.Timedelta(days=7))
        dropped, rel_chg = is_drop(roas_this, roas_prev)
        if dropped:
            roas_prior_prev, _, _ = roas_for(self.eval_date - pd.Timedelta(days=14))
            prior_dropped, _ = is_drop(roas_prev, roas_prior_prev)
            severity = 'CRITICAL' if prior_dropped else 'WARNING'
            self.results.append({'date': self.eval_date, 'rule_id': 'ROA-01', 'metric': 'ROAS',
                                  'level': 'Project', 'entity': 'Project', 'value': round(roas_this, 3),
                                  'baseline': round(roas_prev, 3), 'pct': round(rel_chg, 1), 'severity': severity,
                                  'note': f'program profit {pp_this:.0f} / investment {inv_this:.0f}' +
                                          (' - 2nd consecutive week' if severity == 'CRITICAL' else '')})

    # ------------------------------------------------------------- PRODUCT
    def run_product(self):
        bets_guard = denom_guard(self.proj_bets, 50)
        rtp = self.proj_wins / self.proj_bets.replace(0, np.nan) * 100
        # FIX 6 (found investigating a user question: 2026-11-03 showed WARNING off a run of only
        # 1 actual low-RTP day, because 2026-11-02 breached the band from the OPPOSITE direction
        # (99.4%, too high) and the old single trig_series didn't distinguish direction - two
        # unrelated one-day blips got counted as one 2-day "drift". Split into separate low/high
        # triggers so only a same-direction run counts.
        self.emit('RTP-01', 'RTP', 'Project', 'Project', (rtp < 93) & bets_guard, rtp, pd.Series(96, index=self.all_dates), rtp, 'bets >=50% of 7d avg (placeholder band, low side)')
        self.emit('RTP-01', 'RTP', 'Project', 'Project', (rtp > 99) & bets_guard, rtp, pd.Series(96, index=self.all_dates), rtp, 'bets >=50% of 7d avg (placeholder band, high side)')
        self.emit('RTP-02', 'RTP', 'Project', 'Project', (rtp < 90) & bets_guard, rtp, pd.Series(90, index=self.all_dates), rtp, 'bets >=50% of 7d avg')
        self.emit('RTP-03', 'RTP', 'Project', 'Project', (rtp > 100) & bets_guard, rtp, pd.Series(100, index=self.all_dates), rtp, 'bets >=50% of 7d avg')

        # finding #120 (2026-11-11, same investigation as GGR-04 above): the identical
        # silent-suppression mechanism, here for RTP-02/03's own bets_guard. Checked against
        # RTP-02/03's actual thresholds (90/100), not RTP-01's placeholder band (93/99), since
        # RTP-01 is itself already suppressed under RTP-02/03 via SIBLING_SUPPRESSION - this INFO
        # row is about the stricter pair, not the redundant early-warning one.
        rtp_guard_blocked = ((rtp < 90) | (rtp > 100)) & ~bets_guard
        for d in self.all_dates:
            if not rtp_guard_blocked.loc[d]:
                continue
            self.results.append({
                'date': d, 'rule_id': 'RTP-04', 'metric': 'RTP', 'level': 'Project', 'entity': 'Project',
                'value': round(float(rtp.loc[d]), 2) if pd.notna(rtp.loc[d]) else None, 'baseline': 96,
                'pct': round(float(rtp.loc[d]), 2) if pd.notna(rtp.loc[d]) else None,
                'severity': 'INFO',
                'note': 'RTP-02/03 would otherwise have fired here but the bets volume guard '
                        'blocked it (bets outside +/-50% of its own 7-day baseline)',
            })

        bab = self.proj_bonuses / (self.proj_bets * 0.04).replace(0, np.nan)
        # finding #107 (spec cross-check): the spec's two BAB-01 tiers are MAGNITUDE-based, not
        # duration-based - "Day 2 at ratio>1.0" = WARNING, "Day 2 at ratio>1.5" = CRITICAL - both
        # at the SAME day-2 run-length. The old TIERS entry (None, 2, 2) couldn't express that:
        # severity_for_run() checks CRITICAL first, so w=c=2 meant WARNING was dead code - day 2
        # of ANY abuse-active run (ratio>1.0) always came out CRITICAL regardless of magnitude,
        # a 1.01 ratio and a 10.0 ratio rendered identically. Bypasses emit()/TIERS for this rule
        # (same pattern as FGR-01/NGG-01 above) - run-length still requires >=2 consecutive days
        # of ratio>1.0, but the day-2+ severity is picked by today's own ratio against the 1.5
        # split, not by how many days it's been running.
        bab_active_run = run_length_true((bab > 1.0) & bets_guard)
        for d in self.all_dates:
            if bab_active_run.loc[d] < 2:
                continue
            self.results.append({
                'date': d, 'rule_id': 'BAB-01', 'metric': 'Bonus abuse ratio', 'level': 'Project', 'entity': 'Project',
                'value': round(float(bab.loc[d]), 2), 'baseline': 1.0, 'pct': round(float(bab.loc[d]), 2),
                'severity': 'CRITICAL' if bab.loc[d] > 1.5 else 'WARNING',
                'note': '0.04 assumed house edge placeholder; bets >=50% of 7d avg; CRITICAL at ratio>1.5',
            })

        partner_bonuses = self.ps.groupby(['date', 'partner_id'])['bonuses'].sum().unstack(fill_value=0).reindex(self.all_dates, fill_value=0)
        partner_bets = self.ps.groupby(['date', 'partner_id'])['bets'].sum().unstack(fill_value=0).reindex(self.all_dates, fill_value=0)
        bab_partner = partner_bonuses / (partner_bets * 0.04).replace(0, np.nan)
        # FIX 5 (user-flagged false alarm: partner_5552 fired at $0.42 bets / $0.37 bonus on
        # 2026-11-03 - a pure divide-by-near-zero ratio, not abuse). It passed the old $100/day
        # floor only because ONE historical spike day inflated its 7-day baseline average while
        # actual daily betting was almost always <$300 (project-wide partner-day baseline bets:
        # median $176, p75 $772, p90 $3,686 - $100 barely filters anything). Raised the floor to
        # a data-grounded level AND require TODAY's bets to also clear a floor, so a stale high
        # baseline can't carry a now-dead partner through.
        bets_floor = (baseline(partner_bets) >= 1000) & (partner_bets >= 500)
        for pid in bab_partner.columns:
            if pid not in bets_floor.columns:
                continue
            trig = (bab_partner[pid] > 1.0) & bets_floor[pid]
            self.emit('BAB-02', 'Bonus abuse ratio', 'Partner', f'partner_{pid}', trig, bab_partner[pid], pd.Series(1.0, index=self.all_dates), bab_partner[pid], '0.04 assumed house edge placeholder; partner bets floor >=$1000/day avg AND >=$500 today')

    def run_geo(self):
        """finding #113 (Tier 3, spec cross-check): GEO-01 - share of a partner's registrations
        from outside this project's own target market, >=2 consecutive days. Needs
        self.stats_country (optional constructor arg, see __init__) - produces no rows at all if
        not supplied or if this project has no declared target-market set, same "flag, don't
        crash" convention as every other NOT SIMULATED rule in this file. Threshold is the
        spec's own explicitly-marked "(placeholder)" (Project_Health_Monitoring_Framework_
        FINAL_v2.xlsx row 96) - not independently validated against this project's real history
        the way Project 3's original thresholds were."""
        if self.stats_country is None or self.stats_country.empty:
            return
        target = PROJECT_TARGET_COUNTRIES.get(self.project_id)
        if not target:
            return
        sc = self.stats_country
        is_foreign = ~sc['country'].isin(target)
        partner_total = sc.groupby(['date', 'partner_id'])['registrations'].sum().unstack(fill_value=0).reindex(self.all_dates, fill_value=0)
        partner_foreign = (sc[is_foreign].groupby(['date', 'partner_id'])['registrations'].sum()
                            .unstack(fill_value=0).reindex(self.all_dates, fill_value=0)
                            .reindex(columns=partner_total.columns, fill_value=0))
        # spec gives no minimum volume for GEO-01 - reusing the partner-level C2R-11 registration
        # floor (own convention, not a new number) so a 1-of-3-foreign-registrations day can't
        # read as 33%.
        REG_FLOOR = 20
        for pid in partner_total.columns:
            total = partner_total[pid]
            foreign = partner_foreign[pid]
            share = foreign / total.replace(0, np.nan)
            eligible = baseline(total) >= REG_FLOOR
            trig = (share >= 0.10) & eligible
            self.emit('GEO-01', 'Foreign-geo share', 'Partner', f'partner_{pid}', trig, share * 100,
                      pd.Series(10.0, index=self.all_dates), share * 100,
                      f"registrations from outside target market {sorted(target)}; partner avg >={REG_FLOOR} regs/day; "
                      f"threshold is the spec's own placeholder, not independently validated")

    def _apply_declared_escalations(self):
        """finding #109 (spec cross-check): GGR-03's spec'd Alert-CRITICAL is "When RTP < 90%
        rule also fires" - a GGR spike co-occurring with a confirmed RTP-02 breach the same
        project/day is a compliance-risk signal (wins under-recorded, players underpaid), not
        routine noise, and the spec declares it CRITICAL regardless of GGR-03's own day-count.
        This is a business-rule-author-declared relationship, not discoverable from data or a
        shared formula (see reports/design_structural_correlation_2026-11-10.md, which audited
        exactly this kind of relationship and found none of the data-driven candidates worth
        building - this one is different because the spec states it directly). Severity-only
        change, runs once after every run_* method has populated self.results, mutates in
        place - does not add or remove rows."""
        rtp02_dates = {r['date'] for r in self.results if r['rule_id'] == 'RTP-02' and r['level'] == 'Project'}
        if not rtp02_dates:
            return
        for r in self.results:
            if r['rule_id'] == 'GGR-03' and r['level'] == 'Project' and r['date'] in rtp02_dates and r['severity'] != 'CRITICAL':
                r['severity'] = 'CRITICAL'
                r['note'] = (r['note'] + ' - ' if r['note'] else '') + 'escalated: RTP-02 (RTP<90%) also fired today (spec-declared compliance-risk pattern)'

    def run_all(self):
        self.run_project_volume()
        self.run_partner_guid_lp_volume()
        self.run_conversion()
        self.run_zero_conversion()
        self.run_quality()
        self.run_retention()
        self.run_finance()
        self.run_roas()
        self.run_product()
        self.run_geo()
        self._apply_declared_escalations()
        return pd.DataFrame(self.results)


# ================================================================= SEVERITY TIERS
# (info_days, warning_days, critical_days) - consecutive-day run length needed; None = tier not used.
TIERS = {
    'CLK-02': (None, 1, 2), 'CLK-03': (1, 2, None),
    'CLK-04': (1, 2, None), 'CLK-05': (1, 2, None), 'CLK-06': (1, 2, None), 'CLK-07': (1, 2, None),
    'CLK-08': (1, 2, None), 'CLK-09': (1, 2, None), 'CLK-10': (1, 2, None), 'CLK-11': (1, 2, None),
    'CLK-12': (None, 1, 2), 'CLK-13': (None, 1, None), 'CLK-14': (1, 2, None),
    'CLK-15': (None, 2, None), 'CLK-16': (1, 2, None),
    'CLK-18': (None, 1, 2), 'CLK-19': (1, 2, None),
    'REG-02': (None, 1, 2), 'REG-03': (1, 2, None),
    'REG-04': (1, 2, None), 'REG-05': (1, 2, None), 'REG-06': (1, 2, None), 'REG-07': (1, 2, None),
    'REG-08': (1, 2, None), 'REG-09': (1, 2, None), 'REG-10': (1, 2, None), 'REG-11': (1, 2, None),
    'REG-12': (None, 1, 2), 'REG-13': (None, 1, None), 'REG-14': (1, 2, None),
    'REG-15': (None, 2, None), 'REG-16': (1, 2, None),
    'FTD-02': (None, 1, 2), 'FTD-03': (1, 2, None),
    'FTD-04': (1, None, 2), 'FTD-05': (1, None, 2), 'FTD-06': (1, None, 2), 'FTD-07': (1, None, 2),
    'FTD-08': (1, 2, None), 'FTD-09': (1, 2, None), 'FTD-10': (1, 2, None), 'FTD-11': (1, 2, None),
    'FTD-12': (None, 1, 2), 'FTD-13': (None, 1, 2), 'FTD-14': (1, 2, None),
    'FTD-15': (None, None, 2), 'FTD-16': (1, 2, None),
    'FTD-17': (None, 1, 2), 'FTD-18': (1, 2, None),
    'C2R-01': (None, 1, 2), 'C2R-02': (1, 2, None),
    'C2R-03': (1, 2, None), 'C2R-04': (1, 2, None), 'C2R-05': (1, 2, None), 'C2R-06': (1, 2, None),
    'C2R-07': (1, 2, None), 'C2R-08': (1, 2, None), 'C2R-09': (1, 2, None), 'C2R-10': (1, 2, None),
    # finding #114 (spec cross-check): C2R-12/R2F-13 used (None, 2, None), dropping the first
    # (w-1) days of a run with no row at all (emit() skips a day with sev=None) - unlike every
    # other i=1 WARNING-first rule in this file (R2F-12, FDP-02, C2R-15, ...), which keeps i=1
    # specifically so open_days (daily_notifications()) counts the run's true start instead of
    # whichever day WARNING first fires - see the C2R-15 comment below for the confirmed-by-test
    # failure mode this avoids. INFO rows stay filtered out of the Telegram feed as always.
    'C2R-11': (1, 2, None), 'C2R-12': (1, 2, None), 'C2R-13': (1, 2, None), 'C2R-14': (None, 1, None),
    # C2R-15 (Partner, zero-registration-despite-real-clicks): WARNING at N=5 (the design
    # proposal's own recommended run length), CRITICAL at 2N=10 - run-length escalation via the
    # same mechanism as every other rule, not the proposal's separate "cumulative-zero" path
    # (see run_zero_conversion() docstring). AUDIT_HISTORY.md finding #2 / implemented 2026-11-09.
    # i=1 (not None): unlike most WARNING-first rules, this one's Alert: text displays the real
    # day-count (open_days, from daily_notifications()) to the AM - that count is only accurate
    # from day 1 if every day of the run gets a row (even a filtered-out INFO one); with i=None
    # the first w-1 days produce no row at all (emit() drops them), so open_days would start
    # counting from whichever day WARNING first fires instead of the condition's true start -
    # confirmed by a synthetic test showing "for 1 straight days" on the actual 3rd day. INFO
    # rows are already excluded from the Telegram feed/`_today` sheet by the existing
    # `severity != 'INFO'` filter in main()/run_for_date.py, same as every other i=1 rule
    # (R2F-12, FDP-02, DPS-02, GGR-01, WDR-02, ...) - no new AM-facing noise.
    'C2R-15': (1, 5, 10),
    'R2F-01': (None, 1, 2), 'R2F-02': (1, 2, None),
    'R2F-03': (1, None, 2), 'R2F-04': (1, None, 2), 'R2F-05': (1, None, 2), 'R2F-06': (1, None, 2),
    'R2F-07': (1, 2, None), 'R2F-08': (1, 2, None), 'R2F-09': (1, 2, None), 'R2F-10': (1, 2, None),
    # R2F-13: see the C2R-12/13 finding #114 comment above - same fix, same reason.
    'R2F-11': (None, 1, 2), 'R2F-12': (1, 2, None), 'R2F-13': (1, 2, None), 'R2F-14': (1, 2, None),
    # R2F-15 (Partner, zero-FTD-despite-real-regs/clicks): WARNING at N=3, CRITICAL at 2N=6 -
    # see C2R-15's i=1 comment above (same open_days-accuracy reason) and
    # run_zero_conversion() docstring. AUDIT_HISTORY.md finding #2.
    'R2F-15': (1, 3, 6),
    'ENG-01': (None, 2, None), 'ENG-02': (None, 2, None),
    'FDP-01': (None, None, 1), 'FDP-02': (None, 2, 3), 'FDP-03': (None, 2, None),
    'AFD-01': (None, 2, None),
    'FBT-01': (1, None, None),
    'FBN-01': (None, 2, None),
    'DPS-01': (None, None, 1), 'DPS-02': (None, 1, 2), 'DPS-03': (None, 2, None),
    # WAG-01/02/03 (finding #119, renamed from BET-* per finding #124 - see run_finance()'s own
    # comment): mirrors DPS-01/02/03's own tier shape exactly - same "complete collapse ->
    # moderate drop -> spike" pattern, same reasoning for why.
    'WAG-01': (None, None, 1), 'WAG-02': (None, 1, 2), 'WAG-03': (None, 2, None),
    # finding #110 (Tier 3, spec cross-check): DEP-02 (Project, weekly breadth-drop) and DEP-03
    # (Project, daily drop) were both genuinely new rules (DEP-03's TIERS entry was previously
    # REMOVED as dead config by Fix 11 - see module docstring - because it had no emit() call
    # anywhere; now implemented for real in run_finance(), see below). DEP-02's own Alert column
    # is CRITICAL at "Week 1" with no WARNING tier at all - consistent with ADM-01/BON-01/WDR-01/
    # DPS-01's "operator-side near-total breakdown" pattern (straight to CRITICAL, no staged
    # WARNING) rather than a gradual drift. DEP-03 is a plain daily %-drop rule, INFO/WARNING/
    # CRITICAL at day 1/2/3 exactly as the spec's own Alert-INFO/WARNING/CRITICAL columns state.
    'DEP-02': (None, None, 1), 'DEP-03': (1, 2, 3),
    'GGR-01': (None, 1, 2), 'GGR-02': (None, 2, None), 'GGR-03': (None, 2, None),
    'ADM-01': (None, None, 1), 'ADM-02': (None, 2, None), 'ADM-03': (None, 2, None),
    'BON-01': (None, None, 1), 'BON-02': (None, 2, None),
    'WDR-01': (None, None, 1), 'WDR-02': (None, 2, None),
    'NGR-01': (None, 2, None),
    'RTP-01': (None, 2, None), 'RTP-02': (None, 1, 2), 'RTP-03': (None, 1, 2),
    # BAB-01's entry removed (finding #107): the rule's severity is now magnitude- not
    # duration-based (ratio>1.5 = CRITICAL, else WARNING, both at day>=2 of an active run - see
    # run_product()), computed directly and appended to self.results, bypassing emit()/TIERS
    # entirely - same "dead config, remove rather than leave misleading" call as Fix 11 made for
    # DEP-03 back when IT had no emit() call. BAB-02 is unaffected (spec gives it no magnitude
    # split at all, just a single WARNING-only threshold) and still goes through emit()/TIERS.
    'BAB-02': (None, 2, None),
    # finding #113 (Tier 3, spec cross-check): GEO-01 (Partner, foreign-geo registration share)
    # - spec's own threshold is explicitly marked "(placeholder)" in the source document itself,
    # not just unvalidated by this audit. WARNING only at day 2, no INFO/CRITICAL tier (spec
    # gives neither) - run_geo() below.
    'GEO-01': (None, 2, None),
}


def severity_for_run(rule_id, run):
    i, w, c = TIERS.get(rule_id, (None, 1, None))  # default: WARNING on day 1 if a rule is missing a tier
    if c is not None and run >= c:
        return 'CRITICAL'
    if w is not None and run >= w:
        return 'WARNING'
    if i is not None and run >= i:
        return 'INFO'
    return None


# ================================================================= SUPPRESSION (Fixes 2+3,
# unified 2026-11-10 per AUDIT_HISTORY.md finding #82 - see FIX 15 in the module docstring)
# Child-level alert (GUID or Landing Page) -> the Partner-level rule_id(s) whose same-day,
# same-partner alert already explains it. Formerly two separate dicts/functions (GUID_SUPPRESS_
# UNDER here, pre-attribution; LP_SUPPRESS_UNDER in daily_health_check.py, post-attribution,
# Telegram-only) - merged into one, since the underlying principle is identical one level up:
# suppress a CHILD only when its OWN PARENT, same metric family, same day, is already open.
SUPPRESSION_HIERARCHY = {
    'CLK-15': ['CLK-12', 'CLK-13'], 'CLK-16': ['CLK-14'],
    'REG-15': ['REG-12', 'REG-13'], 'REG-16': ['REG-14'],
    'FTD-15': ['FTD-12', 'FTD-13'], 'FTD-16': ['FTD-14'],
    'C2R-12': ['C2R-11'], 'C2R-13': ['C2R-11'],
    'R2F-13': ['R2F-11'],
    'CLK-18': ['CLK-12', 'CLK-13'], 'CLK-19': ['CLK-14'],
    'FTD-17': ['FTD-12', 'FTD-13'], 'FTD-18': ['FTD-12', 'FTD-13'],
}

# finding #111a (suppression depth, Tier 2, spec cross-check): a GUID child's own Landing Page
# can ALSO explain it, same-day, independent of whether the GUID's Partner-level rule happens to
# be open - the spec's own "Suppress if" text for CLK-15/FTD-15 names the LP rule as an
# ADDITIONAL, not alternative-only-if-no-partner-match, parent. Only the GUID rules whose spec
# row actually names an LP rule get an entry here (REG-15's spec text also names an LP parent,
# but no LP-level Registrations rule exists in this engine at all - structurally impossible,
# left out rather than pointing at a rule_id that can never fire).
GUID_LP_SUPPRESSION = {
    'CLK-15': ['CLK-18'],
    'FTD-15': ['FTD-17', 'FTD-18'],
}

# finding #111b (suppression depth, Tier 2, spec cross-check): same-level, same-entity,
# different-metric suppression - a downstream funnel stage (Registrations, FTDs) explained by
# an upstream stage (Clicks, Registrations) already collapsing for the SAME partner/GUID/LP/
# Project that same day ("clicks > registrations > FTDs", the spec's own suppression chain,
# declared per-rule in its "Suppress if" column), plus RTP-01's own declared suppression under
# RTP-02/RTP-03 (the early-warning band is redundant once the metric has actually breached the
# hard <90%/>100% thresholds those two cover). Structurally different from SUPPRESSION_HIERARCHY
# above (cross-LEVEL, child-under-parent) and GUID_LP_SUPPRESSION (cross-hierarchy, GUID-under-
# its-LP) - this one never crosses level or hierarchy, so it's checked against a flat (rule_id,
# level, entity, date) open-set built from the WHOLE res_df, not just Partner-level rows.
# Deliberately NOT applied to Project-level rules suppressing Partner/Traffic-type rules (a
# separate, LEVEL-crossing relationship the 2026-11-10 structural-correlation audit explicitly
# recommended keep as ANNOTATION only, never suppression - reports/design_structural_
# correlation_2026-11-10.md, "no need to change this for now" per user decision).
SIBLING_SUPPRESSION = {
    'REG-02': ['CLK-02'], 'REG-03': ['CLK-03'],
    'FTD-02': ['REG-02', 'CLK-02'], 'FTD-03': ['REG-03', 'CLK-03'],
    'REG-12': ['CLK-12'], 'REG-13': ['CLK-13'], 'REG-14': ['CLK-14'],
    'FTD-12': ['REG-12', 'CLK-12'], 'FTD-13': ['REG-13', 'CLK-13'], 'FTD-14': ['REG-14', 'CLK-14'],
    'REG-15': ['CLK-15'], 'REG-16': ['CLK-16'],
    'FTD-15': ['REG-15', 'CLK-15'], 'FTD-16': ['REG-16', 'CLK-16'],
    'FTD-17': ['CLK-18'], 'FTD-18': ['CLK-18'],
    'C2R-12': ['CLK-15', 'CLK-16'], 'C2R-13': ['CLK-15', 'CLK-16'],
    'R2F-13': ['FTD-15', 'FTD-16'],
    'R2F-11': ['FTD-12', 'FTD-13'], 'R2F-12': ['FTD-12', 'FTD-13', 'FTD-14'],
    'RTP-01': ['RTP-02', 'RTP-03'],
}


def apply_suppression(res_df):
    """FIX 15: requires `partner_id` already resolved on every row (daily_health_check.py's
    attribute_alerts(), called BEFORE this now, for both GUID and Landing Page rows - previously
    only GUID rows had a resolved partner at this point, via this function's own promo_map
    lookup; LP rows needed attribute_alerts()'s click-share resolution, which is why LP
    suppression used to have to run later, in a separate function, Telegram-only). One function,
    one pipeline point, run after attribution and before either the Excel report or the Telegram
    message is built - so the two outputs can no longer silently disagree on what's suppressed.

    finding #111 (2026-11-10 spec cross-check): extended with two more suppression shapes beyond
    the original child-under-Partner-parent check - see GUID_LP_SUPPRESSION and
    SIBLING_SUPPRESSION above for what each covers and why they're separate dicts. All three are
    OR'd together - a row suppressed by any one of them is suppressed."""
    partner_open = set(
        res_df.loc[res_df['level'] == 'Partner', ['rule_id', 'partner_id', 'date']]
        .dropna().itertuples(index=False, name=None))
    lp_open = set(
        res_df.loc[res_df['level'] == 'Landing Page', ['rule_id', 'entity', 'date']]
        .dropna().itertuples(index=False, name=None))
    all_open = set(
        res_df[['rule_id', 'level', 'entity', 'date']]
        .dropna().itertuples(index=False, name=None))

    def is_suppressed(row):
        if row['level'] in ('GUID', 'Landing Page') and row['rule_id'] in SUPPRESSION_HIERARCHY:
            pid = row.get('partner_id')
            if pid is not None and not pd.isna(pid):
                if any((parent_rid, pid, row['date']) in partner_open for parent_rid in SUPPRESSION_HIERARCHY[row['rule_id']]):
                    return True
        if row['level'] == 'GUID' and row['rule_id'] in GUID_LP_SUPPRESSION:
            lid = row.get('landing_id')
            if lid is not None and not pd.isna(lid):
                lp_entity = f'lp_{int(lid)}'
                if any((parent_rid, lp_entity, row['date']) in lp_open for parent_rid in GUID_LP_SUPPRESSION[row['rule_id']]):
                    return True
        if row['rule_id'] in SIBLING_SUPPRESSION:
            if any((sib_rid, row['level'], row['entity'], row['date']) in all_open for sib_rid in SIBLING_SUPPRESSION[row['rule_id']]):
                return True
        return False

    res_df = res_df.copy()
    res_df['suppressed'] = res_df.apply(is_suppressed, axis=1)
    return res_df[~res_df['suppressed']].drop(columns=['suppressed'])


# ================================================================= CORRELATION (2026-11-09)
# Implements reports/design_correlation_check_2026-11-09.md (AUDIT_HISTORY.md finding #48),
# approved by the user after independently verifying its motivating 2026-11-07 example.
# rule_id -> (ps column to sum per customer_id that day, restrict to is_first_depositor==1 rows).
# 'ggr' is a derived column (bets - wins), added to `ps` inside correlate_alerts(), not a literal
# projects_statistics column. Ratio-family rules (ENG-01/02, STD-01, FBT-01, FBN-01, NGG-01) are
# deliberately absent - a rolling-sum ratio has no well-defined per-customer share the way a
# day-level dollar/count sum does (design report section 1b) - they are never linked, by design,
# not by omission.
CORRELATION_RULE_METRIC = {
    'GGR-01': ('ggr', False), 'GGR-02': ('ggr', False), 'GGR-03': ('ggr', False),
    'NGR-01': ('ggr', False),
    'FGR-01': ('ggr', True),
    'RTP-01': ('wins', False), 'RTP-02': ('wins', False), 'RTP-03': ('wins', False),
    'ADM-01': ('admin_fee', False), 'ADM-02': ('admin_fee', False), 'ADM-03': ('admin_fee', False),
    'WDR-01': ('payouts', False), 'WDR-02': ('payouts', False),
    'BON-01': ('bonuses', False), 'BON-02': ('bonuses', False), 'BAB-01': ('bonuses', False),
    'DPS-01': ('payments', False), 'DPS-02': ('payments', False), 'DPS-03': ('payments', False),
    'FDP-01': ('first_deposit_amount', True), 'FDP-02': ('first_deposit_amount', True), 'FDP-03': ('first_deposit_amount', True),
    # WAG-01/02/03 (finding #119, renamed per #124): same shape as DPS-01/02/03's own entry
    # above, mapped to 'bets'.
    'WAG-01': ('bets', False), 'WAG-02': ('bets', False), 'WAG-03': ('bets', False),
}
# Validated against a real 39-day/6-project sweep (design report section 2c): every real genuine
# link's anchor cleared this with room to spare (smallest 78.8%), every real false-link-risk case
# fell well short (largest computable 9.1%) - the window couldn't distinguish 50/60/70 from each
# other, so 70 was picked as the most conservative of the three with zero recall cost on this
# window, not because 50/60 were shown worse. Revisit if live data lands in the untested 10-70% band.
CORRELATION_ANCHOR_SHARE = 0.70

# finding #121 (2026-11-11, bets/withdrawal-spike investigation): CORRELATION_ANCHOR_SHARE only
# catches a SINGLE dominant customer - it was designed for "one whale explains this alert," not
# "several whales together explain it, but no individual clears 70%." Confirmed real gap: project
# 6's WDR-02 on 2026-11-10 had its single biggest customer at 31.5% (well short of 70%), so got
# zero attribution at all, even though 3 customers together explained 61.5% of that day's payouts
# - real whale-driven activity, just spread across a few people instead of one. Checked against 4
# real cases from the same episode: top-3 combined share was 61.5%/68.9% (payouts, 2 days) and
# 53.1%/48.0% (bets, 2 days) - 0.50 clears 3 of 4, picked as a deliberately looser bar than the
# single-anchor 70% (it has to be, since it's now spread over 3 people, not 1) but still clearly
# above an even split. Not independently validated beyond this one episode's 4 data points - same
# caveat as CORRELATION_ANCHOR_SHARE's own note above, revisit once more real instances exist.
TOP3_COMBINED_SHARE = 0.50


def correlate_alerts(res_df, ps):
    """Tags same-day Project-level finance/product alerts that share a single dominant
    customer_id as their real driver - annotate-only, never suppresses or changes severity.

    For each day with >=2 of the CORRELATION_RULE_METRIC rule_ids firing (at 'Project' level -
    Partner/GUID/LP rows are never touched, see below), computes each fired rule's own dominant
    customer_id and that customer's share of the day's total for the rule's mapped column
    (e.g. GGR's 'ggr' = bets-wins, nets across winners/losers so one big winner can swing it by
    far more than their own raw activity - confirmed on real data reaching 364.7% for ADM-01 on
    one real day, which is a genuine strong signal here, not an error to clip). If the single
    highest-share rule that day (the "anchor") clears CORRELATION_ANCHOR_SHARE, every other
    fired rule that day whose own dominant customer_id matches the anchor's gets linked to it -
    regardless of that other rule's own share number (RTP's share structurally reads lower than
    GGR/ADM/NGR for the identical event - design report section 1c - so requiring every member
    to independently clear the threshold would have missed half the real examples found).

    Adds three columns, additive only: `linked_rule_ids` (comma-joined other rule_ids in the
    cluster), `linked_dominant_partner`, `linked_dominant_share` (this rule's own share, for
    display - deliberately NOT the anchor's share, since section 1c's point is that they
    legitimately differ). Empty/NaN when nothing is linked - the overwhelming majority of rows.

    Project-level only, by design (not just by not-yet-extended): a real 39-day/6-project sweep
    found zero same-day collisions for the three Partner-level siblings (AFD-01/STD-02/BAB-02)
    in this rule tier - see the design report section 3.

    FIX (2026-11-09, findings #74/#75, connections audit):
      - #74: a project/day can in principle hold a SECOND, independent dominant-customer
        cluster once the first anchor's members are set aside - a real sweep found 0/70 days
        actually need this (safe to ship, just rare so far - kept for correctness, not because
        it is commonly hit today). After one cluster is resolved, its rule_ids are removed from
        the candidate pool and the remainder is re-checked for another cluster the same day.
      - #75: a lone alert with no same-day sibling to cluster with was previously skipped
        entirely and got zero outlier context, even when it was itself whale-driven - confirmed
        on real data that 15/72 (21%) of such lone alert-days would independently clear
        CORRELATION_ANCHOR_SHARE if checked. Written to separate `solo_dominant_partner`/
        `solo_dominant_share` columns (never `linked_rule_ids`, since there is nothing to list
        as linked) so "one of several symptoms of one cause" and "this one alert alone is
        whale-driven" stay distinguishable downstream."""
    res_df = res_df.copy()
    res_df['linked_rule_ids'] = ''
    res_df['linked_dominant_partner'] = np.nan
    res_df['linked_dominant_share'] = np.nan
    res_df['solo_dominant_partner'] = np.nan
    res_df['solo_dominant_share'] = np.nan
    res_df['top3_dominant_partners'] = ''
    res_df['top3_dominant_share'] = np.nan

    in_scope = res_df[(res_df['level'] == 'Project') & (res_df['rule_id'].isin(CORRELATION_RULE_METRIC))]
    if in_scope.empty:
        return res_df

    ps = ps.copy()
    ps['ggr'] = ps['bets'] - ps['wins']

    for d, day_rows in in_scope.groupby('date'):
        rule_ids_today = day_rows['rule_id'].unique()
        day_ps = ps[ps['date'] == d]
        dominant = {}
        for rid in rule_ids_today:
            col, ftd_only = CORRELATION_RULE_METRIC[rid]
            rows = day_ps[day_ps['is_first_depositor'] == 1] if ftd_only else day_ps
            if rows.empty:
                continue
            by_cust = rows.groupby('customer_id')[col].sum()
            total = by_cust.sum()
            # near-zero-total guard: with a netting column (ggr/admin_fee), a total close to $0
            # can make "share" explode to a meaningless number purely from division by a tiny
            # denominator - not the same as a real, large netting swing like the validated
            # 364.7% ADM-01 case above (|total|=185.45, not near-zero). $1 is a judgment call,
            # not independently validated beyond this reasoning.
            if by_cust.empty or abs(total) < 1.0:
                continue
            top_cust = by_cust.abs().idxmax()
            share = by_cust.loc[top_cust] / total
            top_partner = rows.loc[rows['customer_id'] == top_cust, 'partner_id'].iloc[0]
            dominant[rid] = (top_cust, top_partner, share, by_cust, rows)

        remaining = dict(dominant)
        while len(remaining) >= 2:
            anchor_rid = max(remaining, key=lambda r: abs(remaining[r][2]))
            anchor_share = remaining[anchor_rid][2]
            if abs(anchor_share) < CORRELATION_ANCHOR_SHARE:
                break  # the max in `remaining` doesn't clear it, so nothing else here can either
            anchor_cust = remaining[anchor_rid][0]
            linked = [rid for rid, v in remaining.items() if v[0] == anchor_cust]
            if len(linked) < 2:
                # cleared the bar alone but no sibling shares its customer - a solo whale, #75
                _, partner, share, _, _ = remaining[anchor_rid]
                idx = (res_df['date'] == d) & (res_df['rule_id'] == anchor_rid) & (res_df['level'] == 'Project')
                res_df.loc[idx, 'solo_dominant_partner'] = partner
                res_df.loc[idx, 'solo_dominant_share'] = share
                del remaining[anchor_rid]
                continue
            for rid in linked:
                _, partner, share, _, _ = remaining[rid]
                others = ', '.join(sorted(r for r in linked if r != rid))
                idx = (res_df['date'] == d) & (res_df['rule_id'] == rid) & (res_df['level'] == 'Project')
                res_df.loc[idx, 'linked_rule_ids'] = others
                res_df.loc[idx, 'linked_dominant_partner'] = partner
                res_df.loc[idx, 'linked_dominant_share'] = share
                del remaining[rid]

        # whatever never clustered (0, 1, or - if the loop broke early - more rule_ids) gets
        # checked against its OWN dominant customer, not just skipped (#75).
        for rid, (_, partner, share, by_cust, rows) in remaining.items():
            idx = (res_df['date'] == d) & (res_df['rule_id'] == rid) & (res_df['level'] == 'Project')
            if abs(share) >= CORRELATION_ANCHOR_SHARE:
                res_df.loc[idx, 'solo_dominant_partner'] = partner
                res_df.loc[idx, 'solo_dominant_share'] = share
                continue
            # finding #121: no single customer dominates - check whether the top 3 TOGETHER do.
            # Only meaningful with >=3 distinct customers in the first place (otherwise "top 3"
            # is just "everyone", not a concentration signal), and only worth a separate note
            # when it explains clearly more than the single top customer already would (15pp
            # margin - arbitrary but keeps this from just restating the solo-share number).
            if len(by_cust) < 3:
                continue
            top3 = by_cust.abs().sort_values(ascending=False).head(3)
            combined_share = by_cust.loc[top3.index].sum() / by_cust.sum()
            if abs(combined_share) >= TOP3_COMBINED_SHARE and abs(combined_share) - abs(share) >= 0.15:
                partners = sorted({
                    int(rows.loc[rows['customer_id'] == c, 'partner_id'].iloc[0]) for c in top3.index
                })
                res_df.loc[idx, 'top3_dominant_partners'] = ', '.join(f'partner_{p}' for p in partners)
                res_df.loc[idx, 'top3_dominant_share'] = combined_share

    return res_df


# finding #69/#71/#76 (connections audit, 2026-11-09): same-partner/same-day alert pairs
# confirmed on a real 69-day/6-project sweep to share one underlying cause even though there is
# no common customer_id the way correlate_alerts() above finds one - these rules fire off
# aggregate volume/ratio conditions (breadth share, zero-FTD-despite-traffic, an average deposit
# size), which have no single "whose fault" customer. Separate, additive `entity_linked_rule_ids`
# column - never collides with `linked_rule_ids` (customer-identity, Project-level only) since
# this mechanism is Partner-level only. Symmetric: listing A -> [B] also links B -> A.
ENTITY_CORRELATION_LINKS = {
    'CLK-13': ['REG-13'],  # #69: 53%/51%/49% co-open at lag 0/1/2 - a shared episode, not a lag
    'R2F-15': ['CLK-12', 'CLK-13', 'CLK-14', 'REG-12', 'REG-13', 'REG-14'],  # #71: 68 real instances
    'C2R-15': ['CLK-12', 'CLK-13', 'CLK-14', 'REG-12', 'REG-13', 'REG-14'],  # #71: 5 real instances
    'AFD-01': ['CLK-13', 'CLK-14', 'REG-13', 'REG-14', 'FTD-13', 'FTD-14'],  # #76: 32 real instances
}


def correlate_entity_pairs(res_df):
    """Annotate same-partner/same-day co-occurrences from ENTITY_CORRELATION_LINKS - see that
    dict's own comment for why this is a separate mechanism from correlate_alerts(). Never
    suppresses, never touches severity - adds `entity_linked_rule_ids` only."""
    res_df = res_df.copy()
    res_df['entity_linked_rule_ids'] = ''
    partner_rows = res_df[res_df['level'] == 'Partner']
    if partner_rows.empty:
        return res_df

    links = {}
    for rid, others in ENTITY_CORRELATION_LINKS.items():
        links.setdefault(rid, set()).update(others)
        for o in others:
            links.setdefault(o, set()).add(rid)

    for (entity, d), grp in partner_rows.groupby(['entity', 'date']):
        rule_ids_today = set(grp['rule_id'])
        for rid in rule_ids_today & set(links):
            matched = sorted(rule_ids_today & links[rid])
            if not matched:
                continue
            idx = (res_df['entity'] == entity) & (res_df['date'] == d) & (res_df['rule_id'] == rid) & (res_df['level'] == 'Partner')
            res_df.loc[idx, 'entity_linked_rule_ids'] = ', '.join(matched)

    return res_df


# finding #122 (2026-11-11, bets/withdrawal-spike investigation): a one-day-lag relationship -
# ENG-02 (bets/deposits ratio spike, often a big win being played back out) followed the NEXT
# day by WDR-02/NGR-01/FGR-01/GGR-03 (the cash-out/falloff side of the same event) - confirmed
# real on project 6 (Brazino777.ar): ENG-02 fired 2026-11-09 (+51.7%), WDR-02 fired 2026-11-10
# (+115.6%), same project, traced to the same underlying whale (customer_18134263). Every
# existing correlation mechanism here is same-day only, so these showed up as two fully
# disconnected daily alerts. Direction matters (yesterday's ratio spike explains today's
# cash-out, not the reverse), so this is deliberately one-directional and annotates only the
# LATER alert - that's the one an AM is reading when the context is actually useful. ENG-02 is
# the only trigger rule for now (the one real instance observed); not generalized to every
# ratio-family rule without more real examples to check it against. Project-level only, same
# scope as every other mechanism in this section.
#
# FIX (2026-11-11, user feedback: "it is not that it's not a new issue, they are connected
# (and you need to check if they actually are)"): the original version only checked TIMING -
# same project, trigger rule fired exactly one day before a followup rule - and then asserted
# "the same underlying event continuing" regardless of whether any actual evidence backed that
# up. That's exactly backwards: two alerts a day apart are a coincidence until something
# ties them together, not connected until proven otherwise. Now checks for a REAL shared
# driver before claiming one: if annotate_ratio_numerator_hint() (which must run BEFORE this
# function - see apply_correlation()) found a dominant customer's partner behind yesterday's
# ENG-02, and that same partner also shows up in TODAY's own dominant-partner data (solo/
# linked/top-3, from correlate_alerts() above), that's real cross-day evidence, not just
# adjacency - `prior_day_shared_partner` is set, and the rendered note names the partner
# directly as the basis for the claim. When no such overlap is found (most days, including
# whenever ENG-02 didn't clear the ratio-hint's own 20% floor), this only records that the
# TIMING lines up (`prior_day_rule_ids`) - the note for that case has to say "possible", not
# "is", and should say so explicitly rather than asserting a cause with no evidence behind it.
LAG1_TRIGGER_RULE = 'ENG-02'
LAG1_FOLLOWUP_RULES = ['WDR-02', 'NGR-01', 'FGR-01', 'GGR-03']


def correlate_lag1(res_df):
    """Annotates a Project-level LAG1_FOLLOWUP_RULES alert with `prior_day_rule_ids` and
    `prior_day_metric` whenever LAG1_TRIGGER_RULE fired at Project level for the same project
    exactly one day earlier, and - separately - with `prior_day_shared_partner` only when a
    real shared dominant partner was found (see the finding #122 fix note above). Must run
    AFTER both correlate_alerts() (today's dominant-partner columns) and
    annotate_ratio_numerator_hint() (yesterday's ratio_hint_partner) - see apply_correlation().
    Annotate-only - never suppresses, never changes severity."""
    res_df = res_df.copy()
    res_df['prior_day_rule_ids'] = ''
    res_df['prior_day_metric'] = ''
    res_df['prior_day_shared_partner'] = np.nan
    proj = res_df[res_df['level'] == 'Project']
    if proj.empty:
        return res_df

    trigger_rows = proj[proj['rule_id'] == LAG1_TRIGGER_RULE]
    if trigger_rows.empty:
        return res_df
    # date -> (human metric label, that day's ratio-hint dominant partner, if any)
    trigger_info = {r['date']: (r['metric'], r.get('ratio_hint_partner')) for _, r in trigger_rows.iterrows()}

    followup_next_day = proj['rule_id'].isin(LAG1_FOLLOWUP_RULES) & proj['date'].apply(
        lambda d: (d - pd.Timedelta(days=1)) in trigger_info)
    for i in proj.index[followup_next_day]:
        row = res_df.loc[i]
        metric, hint_partner = trigger_info[row['date'] - pd.Timedelta(days=1)]
        res_df.loc[i, 'prior_day_rule_ids'] = LAG1_TRIGGER_RULE
        res_df.loc[i, 'prior_day_metric'] = metric
        if hint_partner is None or pd.isna(hint_partner):
            continue  # yesterday's ENG-02 never cleared the ratio-hint floor - nothing to check against
        today_partners = set()
        for col in ('solo_dominant_partner', 'linked_dominant_partner'):
            v = row.get(col)
            if v is not None and not pd.isna(v):
                today_partners.add(int(v))
        t3 = row.get('top3_dominant_partners')
        if t3:
            today_partners |= {int(tok.strip().split('_')[1]) for tok in str(t3).split(',')}
        if int(hint_partner) in today_partners:
            res_df.loc[i, 'prior_day_shared_partner'] = int(hint_partner)
    return res_df


# finding #123 (2026-11-11, same investigation): ratio-family rules (ENG-01/02, FBT-01, FBN-01,
# NGG-01) are deliberately excluded from CORRELATION_RULE_METRIC/correlate_alerts() above - "a
# rolling-sum ratio has no well-defined per-customer share" (design report section 1b, still
# true, not reopened here). Real cost of that exclusion: ENG-02 fired on project 6 on
# 2026-11-09 (+51.7%) with zero partner/customer context, even though one customer
# (customer_18134263) accounted for 20.3% of that day's bets - the ratio's numerator - a real,
# if partial, explanation. This does NOT attempt the "share of the ratio" math that section 1b
# correctly says isn't well-defined - it's a softer, explicitly-labeled HINT: who contributed
# most to the numerator column that day, shown only when that contribution clears both an
# absolute floor (>=20% of the day's numerator total) and a relative one (>=3x what an even
# split across that day's active customers would give, so a thin 2-3-customer day doesn't
# trivially clear it). STD-01 stays out of scope even here - its "numerator" is a count of
# distinct returning depositors, not a summable $/count column, so this same per-customer-sum
# mechanism doesn't apply to it. Thresholds are a first pass checked only against the one real
# case above (20.3% share did clear them) - not validated against real history beyond that,
# same caveat as every other number this investigation introduced.
#   numer_col, denom_col, ftd_only. denom_col is the column to also sum per customer for the
#   FIX below (2026-11-13) - a real summable $ column, the sentinel '__FTD_COUNT__' for FBT-01
#   (whose denominator is a COUNT of FTDs, not a $ sum - each FTD customer contributes exactly
#   1), or None for NGG-01 (a 7-day ROLLING ratio, not this function's single-day
#   ratio_sum_baseline() shape - the two-sided check below needs a same-day denominator total
#   it can attribute per customer, which NGG-01 doesn't have in this form; left on the
#   numerator-only path, unchanged, rather than forcing an ill-fitting check onto a differently-
#   shaped metric).
RATIO_NUMERATOR_HINT = {
    'ENG-01': ('bets', 'payments', False), 'ENG-02': ('bets', 'payments', False),
    'FBT-01': ('bets', '__FTD_COUNT__', True),
    'FBN-01': ('bonuses', 'first_deposit_amount', True),
    'NGG-01': ('ngr', None, False),
}
RATIO_HINT_MIN_SHARE = 0.20
RATIO_HINT_MIN_CONCENTRATION = 3.0  # top customer's share must be >= this many times an equal split

# FIX (2026-11-13, user feedback on a real example - one customer at 90% of a day's bets,
# rendered as "Possible hint, not confirmed": "but you can check how the whole ratio moved, not
# just numerator"): the hint above only ever looked at the NUMERATOR - a customer dominating
# bets says nothing about whether they (or someone else entirely) drove the DEPOSITS side too, so
# it could never honestly confirm causation for the ratio itself. This adds the other side: given
# the already-identified top numerator customer, recompute the ratio with them removed from BOTH
# numerator and denominator, and check how much closer that gets to the rule's own trailing
# baseline. If removing them explains most of the day's move, that's real two-sided evidence, not
# just a numerator-concentration guess - same "clears 70%, call it confirmed" bar
# correlate_alerts() already validated for customer-identity clustering (CORRELATION_ANCHOR_SHARE),
# reused here rather than inventing a new one. Unvalidated against real history beyond the
# motivating example - same caveat as the numerator-only hint's own thresholds above.
RATIO_HINT_CONFIRM_EXPLAINED = 0.70


def annotate_ratio_numerator_hint(res_df, ps):
    """Adds `ratio_hint_partner`/`ratio_hint_share`/`ratio_hint_confirmed` to Project-level
    RATIO_NUMERATOR_HINT rows - see finding #123 above for the numerator-only hint, and the FIX
    comment above RATIO_HINT_CONFIRM_EXPLAINED for the two-sided upgrade. Annotate-only."""
    res_df = res_df.copy()
    res_df['ratio_hint_partner'] = np.nan
    res_df['ratio_hint_share'] = np.nan
    res_df['ratio_hint_confirmed'] = False
    in_scope = res_df[(res_df['level'] == 'Project') & (res_df['rule_id'].isin(RATIO_NUMERATOR_HINT))]
    if in_scope.empty:
        return res_df

    ps = ps.copy()
    ps['ngr'] = ps['bets'] - ps['wins'] - ps['bonuses'] - ps['chargebacks'] - ps['admin_fee']

    for idx, row in in_scope.iterrows():
        rid, d = row['rule_id'], row['date']
        col, denom_col, ftd_only = RATIO_NUMERATOR_HINT[rid]
        day_ps = ps[ps['date'] == d]
        rows = day_ps[day_ps['is_first_depositor'] == 1] if ftd_only else day_ps
        if rows.empty:
            continue
        by_cust = rows.groupby('customer_id')[col].sum()
        total = by_cust.sum()
        n = len(by_cust)
        if n == 0 or abs(total) < 1.0:
            continue
        top_cust = by_cust.abs().idxmax()
        share = by_cust.loc[top_cust] / total
        if not (abs(share) >= RATIO_HINT_MIN_SHARE and abs(share) >= RATIO_HINT_MIN_CONCENTRATION / n):
            continue
        top_partner = rows.loc[rows['customer_id'] == top_cust, 'partner_id'].iloc[0]
        res_df.loc[idx, 'ratio_hint_partner'] = top_partner
        res_df.loc[idx, 'ratio_hint_share'] = share

        # Two-sided check: skip entirely for NGG-01 (denom_col is None, see the dict's own
        # comment) - stays on the numerator-only hint above, unchanged.
        if denom_col is None:
            continue
        customer_numer = by_cust.loc[top_cust]
        if denom_col == '__FTD_COUNT__':
            total_denom = n  # one FTD row == one customer == 1 unit of the denominator
            customer_denom = 1.0
        else:
            by_cust_denom = rows.groupby('customer_id')[denom_col].sum()
            total_denom = by_cust_denom.sum()
            customer_denom = by_cust_denom.get(top_cust, 0.0)
        remaining_numer = total - customer_numer
        remaining_denom = total_denom - customer_denom
        if remaining_denom == 0 or pd.isna(row['baseline']) or pd.isna(row['value']):
            continue
        ratio_actual = row['value']  # already *100-scaled, same convention as ratio_sum_baseline()
        ratio_baseline = row['baseline']
        ratio_excluding = remaining_numer / remaining_denom * 100
        gap_actual = abs(ratio_actual - ratio_baseline)
        if gap_actual < 1e-9:
            continue  # no real gap to explain (shouldn't happen - the rule only fires on a real move)
        gap_excluding = abs(ratio_excluding - ratio_baseline)
        explained = 1 - gap_excluding / gap_actual
        if explained >= RATIO_HINT_CONFIRM_EXPLAINED:
            res_df.loc[idx, 'ratio_hint_confirmed'] = True
            res_df.loc[idx, 'ratio_hint_share'] = max(0.0, min(1.0, explained))
    return res_df


# finding #73 (connections audit, 2026-11-09): a Project-level "systemic" CLK/REG/FTD-02/03
# alert is, 77% of the time in a real 69-day/6-project sweep, really driven by one partner
# accounting for a large share of the project's own 7-day baseline for that metric (several real
# examples at 85-100%) - "Project-wide" mislabels what is really a single-partner event most of
# the time. Annotate-only, same philosophy as correlate_alerts(): never suppresses, just names
# the dominant partner, reusing partner_clicks/regs/ftds already in memory (zero new query).
PROJECT_DOMINANT_RULES = {
    'CLK-02': 'partner_clicks', 'CLK-03': 'partner_clicks',
    'REG-02': 'partner_regs', 'REG-03': 'partner_regs',
    'FTD-02': 'partner_ftds', 'FTD-03': 'partner_ftds',
}
PROJECT_DOMINANT_SHARE = 0.30  # validated: 72/94 (77%) of real rows clear this


def annotate_project_dominant_partner(res_df, eng):
    """`eng` is the RuleEngine instance that produced res_df (for its partner_clicks/regs/ftds
    series) - called right after correlate_alerts(), same per-project loop, in run_for_date.py
    and daily_health_check.py's main()."""
    res_df = res_df.copy()
    res_df['dominant_partner'] = np.nan
    res_df['dominant_partner_share'] = np.nan
    in_scope = res_df[(res_df['level'] == 'Project') & (res_df['rule_id'].isin(PROJECT_DOMINANT_RULES))]
    if in_scope.empty:
        return res_df

    for idx, row in in_scope.iterrows():
        partner_series = getattr(eng, PROJECT_DOMINANT_RULES[row['rule_id']])
        d = row['date']
        if d not in partner_series.index:
            continue
        day_base = baseline(partner_series).loc[d]
        total = day_base.sum()
        if total <= 0:
            continue
        top_pid = day_base.idxmax()
        share = day_base.loc[top_pid] / total
        if share >= PROJECT_DOMINANT_SHARE:
            res_df.loc[idx, 'dominant_partner'] = top_pid
            res_df.loc[idx, 'dominant_partner_share'] = share
    return res_df


# FIX 16 (2026-11-10, AUDIT_HISTORY.md finding #84): the three correlation/outlier mechanisms
# above were three independent calls in main()/run_for_date.py with no stated reason for their
# order. This formalizes the declared priority - identity match where customer_id is available,
# else a declared partner/GUID/LP hierarchy share, else a declared static pair - as one call.
# Each mechanism's own internals are untouched and reused exactly as already validated; this is
# an ordering/documentation fix, not a rewrite of any of the three.
def apply_correlation(res_df, ps, eng):
    """Runs correlate_alerts() (customer-identity + top-3-combined-share, Project level), then
    annotate_project_dominant_partner() (partner-id hierarchy share, Project level), then
    correlate_entity_pairs() (declared static pairs, Partner level), then
    annotate_ratio_numerator_hint() (soft numerator-contributor hint for ratio-family rules,
    Project level) and finally correlate_lag1() (one-day-lag ENG-02 -> cash-out rules, Project
    level) - findings #121-123, 2026-11-11. The ratio-hint step MUST run before correlate_lag1():
    the lag check reads yesterday's `ratio_hint_partner` to decide whether a same-partner link
    is real evidence or just same-project timing (finding #122's fix, same date). `ps` is the
    project's own projects_statistics pull (for correlate_alerts() and
    annotate_ratio_numerator_hint()); `eng` is the RuleEngine instance that produced res_df (for
    annotate_project_dominant_partner()'s partner_clicks/regs/ftds series)."""
    res_df = correlate_alerts(res_df, ps)
    res_df = annotate_project_dominant_partner(res_df, eng)
    res_df = correlate_entity_pairs(res_df)
    res_df = annotate_ratio_numerator_hint(res_df, ps)
    res_df = correlate_lag1(res_df)
    return res_df


def daily_notifications(df, reminder_every_days=7):
    """NEW/ESCALATION/REMINDER event stream for the `_today` sheet and Telegram feed.

    FIX (2026-11-06, AUDIT_HISTORY.md finding #23 - "permanent silence" bug): `prev_rank`
    used to be carried across the ENTIRE lookback window with no reset, so a chronic
    (rule_id, entity) pair that hits its TIERS ceiling on day 1 and then just stays open at
    the same severity for weeks produced exactly one NEW event, ever, in that window - never
    reappearing in `_today`/Telegram again unless it escalated. Confirmed empirically against
    partner_9552/partner_1441's CLK-13/REG-13 (open on nearly every day of three real 35-day
    windows, never once on an actionable day after day one). Two changes:
      1. Episode-boundary reset: a real gap (>1 day) since this pair's last trigger date means
         the condition cleared and came back - a fresh episode, not a silent continuation of
         the old one - so `prev_rank` resets and it gets its own NEW.
      2. Periodic re-surface: even with no gap and no rank increase, emit a REMINDER every
         `reminder_every_days` days the pair has been continuously open, carrying the
         `open_days` count, so a still-open chronic alert doesn't vanish from the AM's view for
         the rest of its life. 7 days was finding #23's own suggested cadence; not validated
         against real AM feedback yet - revisit if it turns out too chatty or too quiet.

    FIX 17 (2026-11-10, AUDIT_HISTORY.md finding #81): ESCALATION events now also carry
    `escalated_from`, the prior row's severity - so the render side (daily_health_check.py's new
    _escalation_note()) can say "escalated from WARNING to CRITICAL" instead of nothing at all.
    No change to which events fire or when - purely an added field on the existing ESCALATION
    event.
    """
    sev_rank = {'INFO': 0, 'WARNING': 1, 'CRITICAL': 2}
    events = []
    for (rid, ent), grp in df.groupby(['rule_id', 'entity']):
        grp = grp.sort_values('date')
        prev_rank = -1
        prev_severity = None
        prev_date = None
        open_days = 0
        for _, row in grp.iterrows():
            date = row['date']
            r = sev_rank[row['severity']]
            if prev_date is None or (date - prev_date).days > 1:
                prev_rank = -1  # episode boundary: real gap since this pair's last trigger
                prev_severity = None
                open_days = 0
            open_days += 1
            # 'escalated_from' always present (None when not applicable) - a conditionally-added
            # dict key would make pandas drop the column entirely from the resulting DataFrame on
            # any day/project with zero ESCALATION events, breaking any fixed-column selection
            # downstream (e.g. daily_health_check.py main()'s `cols` list).
            event_row = {**row.to_dict(), 'open_days': open_days, 'escalated_from': None}
            if prev_rank == -1:
                events.append({**event_row, 'event_type': 'NEW'})
            elif r > prev_rank:
                events.append({**event_row, 'event_type': 'ESCALATION', 'escalated_from': prev_severity})
            elif open_days % reminder_every_days == 0:
                events.append({**event_row, 'event_type': 'REMINDER'})
            prev_rank = r
            prev_severity = row['severity']
            prev_date = date
    return pd.DataFrame(events)
