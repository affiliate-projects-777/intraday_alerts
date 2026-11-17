"""One-off manual validation (2026-11-18): confirms send_am_chat_messages() actually delivers
to the 4 real per-AM Telegram chats. Not part of the pipeline - run once, then delete.
Expected: Fotini's chat gets both lines; jackie's/Willard's/Christie's chats get only the global
line (am=None)."""
import datetime
import main

alerts = [
    {'rule_id': 'TEST', 'metric': 'Clicks', 'level': 'Landing Page', 'entity': 'landing_TEST',
     'project_id': 4, 'severity': 'WARNING',
     'note': 'VALIDATION TEST - please ignore - personal alert, should only reach Fotini',
     'am': 'Fotini', 'dominant_partner': 9552},
    {'rule_id': 'TEST', 'metric': 'Deposits', 'level': 'Project', 'entity': 'Brazino777.io',
     'project_id': 4, 'severity': 'WARNING',
     'note': 'VALIDATION TEST - please ignore - global alert, should reach all 4 AM chats',
     'am': None, 'dominant_partner': None},
]

main.send_am_chat_messages(alerts, datetime.datetime.now())
print("done")
