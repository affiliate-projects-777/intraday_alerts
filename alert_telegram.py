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
from alert_types import Alert

TELEGRAM_CHAR_LIMIT = 4096


def render_html_messages(alerts: List[Alert], date: str) -> List[str]:
    """list[Alert] -> list[str], each <= TELEGRAM_CHAR_LIMIT, HTML-escaped and <code>-wrapped,
    split only at section/entity boundaries (never inside one alert's own block), repeating the
    section header on any continuation message. Uses alert_render.build_sections() directly
    (the structured form) rather than re-parsing render()'s plain-text output - §5 is about
    layout/limits, not about re-deriving anything from text."""
    with alert_render.html_mode():
        sections = alert_render.build_sections(alerts)
        header_line = alert_render.render_message_header(date, alerts)
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
