"""VPS-only replica access shim - exposes just `replica_run_query(query)`, the ONE function
this deploy's pipeline (health_check_engine.py / daily_health_check.py) actually calls from
the real `customers/tqa_model/db.py` (confirmed by grep before writing this: fetch_account_
managers()/fetch_partner_names()/fetch_p4_country_breakdown() and health_check_engine.py's
module-level `from db import replica_run_query` are the only callers in this pipeline).

The real db.py also builds VIEWS/AZINO/ADMIRAL engines and an SSH-tunneled Azino connection,
none of which this deploy's files ever call - importing the real file as-is would require
VIEWS_*/AZINO_*/ADMIRAL_*/AZ_SSH_* env vars just to succeed the module-level os.environ[...]
reads, for nothing this job uses. This shim needs only REPLICA_* - same 5 vars already used by
intraday_alerts/main.py's own get_engine() on this same VPS, since this host has direct
(no-tunnel) network access to the replica.
"""
import os
import pandas as pd
from sqlalchemy import create_engine
from urllib.parse import quote_plus

_host = os.environ['REPLICA_HOST']
_port = int(os.environ.get('REPLICA_PORT', 3306))
_user = os.environ['REPLICA_USER']
_password = os.environ['REPLICA_PASSWORD']
_database = os.environ['REPLICA_DATABASE']

_engine = create_engine(f"mysql+pymysql://{quote_plus(_user)}:{quote_plus(_password)}@{_host}:{_port}/{_database}")


def replica_run_query(query):
    return pd.read_sql(query, _engine)
