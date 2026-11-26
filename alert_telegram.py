# -*- coding: utf-8 -*-
"""
§5: the Telegram-specific layer on top of alert_render.py's pure plain-text renderer - HTML
escaping/<code>-wrapping, the <=4096-char split (only at section/entity boundaries, repeating
the section header on a continuation message, never splitting inside one alert), and the actual
send. Nothing here re-derives a fact from an Alert - it only re-renders the SAME records in
HTML mode and packs the result.
"""
import os
from typing import List

import alert_render
from alert_types import Alert, PLATFORM

TELEGRAM_CHAR_LIMIT = 4096


def render_html_messages(alerts: List[Alert], date: str, *, render_block=None, render_header=None) -> List[str]:
    """list[Alert] -> list[str], each <= TELEGRAM_CHAR_LIMIT, HTML-escaped and <code>-wrapped,
    split only at section/entity boundaries (never inside one alert's own block), repeating the
    section header on any continuation message. Uses alert_render.build_sections() directly
    (the structured form) rather than re-parsing render()'s plain-text output - §5 is about
    layout/limits, not about re-deriving anything from text.

    `render_block`/`render_header` default to alert_render.py's own v1 functions - every
    existing caller (send_for_date(), send_am_chat_messages()) that doesn't pass them is
    unaffected. render_html_messages_v2() below is just this function called with
    alert_render_v2's block/header renderers, so the char-budget splitting logic is never
    duplicated between the two local-test formats."""
    render_block = render_block or alert_render.render_alert_block
    render_header = render_header or alert_render.render_message_header
    with alert_render.html_mode():
        sections = alert_render.build_sections(alerts, render_block=render_block)
        header_line = render_header(date, alerts)
        chunks = []  # (owner, section_header_line, entity_chunk_text)
        for owner, entities in sections:
            section_header = alert_render.render_section_header(owner)
            for ehead, blocks in entities:
                lines = [ehead]
                # Same blank-line-between-an-entity's-own-multiple-alerts fix as
                # alert_render.render()'s own loop - kept in sync so the two renderers still
                # share one structure (see test_html_shares_the_same_structure_as_plain_text_render).
                for j, b in enumerate(blocks):
                    if j > 0:
                        lines.append("")
                    lines.extend(b)
                chunks.append((owner, section_header, "\n".join(lines)))

    # FIX (caught by test_html_shares_the_same_structure_as_plain_text_render): a section
    # header and its OWN first entity are joined by a single newline, never a blank line (see
    # alert_render.render()'s own assembly) - treating `section_header` and `chunk_text` as two
    # SEPARATE items in the "\n\n"-joined list (as an earlier version of this function did)
    # inserted a blank line there that the plain-text renderer never has. Each "piece" below is
    # either just the chunk, or "section_header\nchunk" combined into ONE piece when it's the
    # first chunk of that owner in the current message - pieces themselves are still joined by
    # "\n\n" (the real entity-to-entity and section-to-section blank line).
    messages = []
    current_lines = [header_line]
    current_owner = None
    has_content = False

    for owner, section_header, chunk_text in chunks:
        needs_header = (owner != current_owner)
        piece = f"{section_header}\n{chunk_text}" if needs_header else chunk_text
        candidate = current_lines + [piece]
        if has_content and len("\n\n".join(candidate)) > TELEGRAM_CHAR_LIMIT:
            messages.append("\n\n".join(current_lines))
            current_lines = [f"{section_header}\n{chunk_text}"]
            current_owner = owner
        else:
            current_lines = candidate
            current_owner = owner
        has_content = True
    messages.append("\n\n".join(current_lines))
    return messages


# ================================================================= send layer
# Reuses the existing idempotency guard (daily_health_check.py's already_sent_telegram()/
# mark_telegram_sent()/send_telegram() - the actual HTTP POST, env-var lookup, and success/
# failure handling) rather than re-implementing it: that part of §5 ("Send with
# parse_mode=HTML") has nothing to do with the free-text-vs-structured-record problem this
# whole task is about, and daily_health_check.py's own version was already fixed once
# (AUDIT_HISTORY.md - send_telegram() returning a real bool, only marking "sent" on success).

def send_for_date(alerts: List[Alert], date: str, force: bool = False):
    import daily_health_check as dhc
    messages = render_html_messages(alerts, date)
    dhc.send_telegram_for_date(date, messages, force=force)
    return messages


# ================================================================= v2 (spec-draft format, local testing only)
# Added 2026-11-18 (user: "create a second version of messaging i can test locally... keep the
# original too... --v2 to test the new messaging so i can compare") - see alert_render_v2.py's
# own module docstring and AUDIT_HISTORY.md findings #157-162 (the 2026-11-18 Telegram-
# messaging-spec audit this is the draft implementation of). NOT used by daily_health_check.py's
# main() or run_for_date.py unless the `--v2` flag is passed - send_for_date()/
# send_am_chat_messages() above are completely unaffected, byte-for-byte, by this section.
def render_html_messages_v2(alerts: List[Alert], date: str) -> List[str]:
    import alert_render_v2 as v2
    return render_html_messages(alerts, date, render_block=v2.render_alert_block_v2,
                                 render_header=v2.render_message_header_v2)


def send_for_date_v2(alerts: List[Alert], date: str, force: bool = False):
    import daily_health_check as dhc
    messages = render_html_messages_v2(alerts, date)
    dhc.send_telegram_for_date(date, messages, force=force)
    return messages


def send_am_chat_messages_v2(alerts: List[Alert], date: str):
    """v2 counterpart of send_am_chat_messages() below - same per-AM routing, same
    AM_TELEGRAM_CHATS, only the rendering differs."""
    import daily_health_check as dhc
    out = {}
    for am, chat_id in AM_TELEGRAM_CHATS.items():
        subset = [a for a in alerts if a.owner == am or a.owner == PLATFORM]
        if not subset:
            continue
        messages = render_html_messages_v2(subset, date)
        out[am] = messages
        for msg in messages:
            dhc.send_telegram_to_chat(msg, chat_id)
    return out


# ================================================================= per-AM chat routing
# Added 2026-11-17 (user: "create a group of chats on telegram and have one chat for each AM to
# simulate exactly what they get"). Each AM's chat should see: their own alerts (Alert.owner ==
# that AM's name) plus every PLATFORM-level alert (the same "Not partner-specific" broadcast
# every AM already sees inside the single shared chat via send_for_date() above) - never
# UNASSIGNED alerts, which have no owner to route to. This reuses render_html_messages()
# unchanged, so a chat's content is rendered by the exact same code path as the real shared
# message - "simulate exactly what they get" is true by construction, not by separately
# maintaining a second renderer that could drift from the first.
#
# AM name must match Alert.owner exactly - alert_mapping.py sets owner from
# daily_health_check.fetch_account_managers()'s own partner_tags title spelling/case. Only AMs
# with a real test chat set up so far are listed; every other AM's alerts still only reach the
# single shared TELEGRAM_CHAT_ID via send_for_date(), unaffected by this - additive, not a
# replacement for the existing send.
AM_TELEGRAM_CHATS = {
    'jackie': -5487009071,
    'Fotini': -5195990482,
    'Willard': -5544224465,
    'Christie': -5420792053,
}


def send_am_chat_messages(alerts: List[Alert], date: str):
    """Splits `alerts` per configured AM (AM_TELEGRAM_CHATS) and sends each AM's own
    personal+global digest to their own Telegram chat - additive alongside send_for_date()'s
    existing single shared-chat send, not a replacement for it. An AM with nothing of their own
    AND no PLATFORM-level alerts today gets nothing sent (no empty "quiet day" message - matches
    build_sections()'s own behavior of simply omitting an owner with zero alerts). Returns
    {am_name: messages_sent} for the AMs that had something to send."""
    import daily_health_check as dhc
    out = {}
    for am, chat_id in AM_TELEGRAM_CHATS.items():
        subset = [a for a in alerts if a.owner == am or a.owner == PLATFORM]
        if not subset:
            continue
        messages = render_html_messages(subset, date)
        out[am] = messages
        for msg in messages:
            dhc.send_telegram_to_chat(msg, chat_id)
    return out
