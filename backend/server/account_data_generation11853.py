"""Owner-bound data generations derived from permanent reset tombstones.

No new schema, credentials or mutable preferences are used as an epoch.
Expiry sweeps remove encrypted credentials but retain each reset request UUID.
"""
from __future__ import annotations
import hashlib,hmac,json,re
from datetime import datetime,timezone
from uuid import UUID
from ollama_chat_app.data.database import timestamp_from_db,timestamp_to_db
from .platform_leases import server_instance_id

HEX64=re.compile(r'[a-f0-9]{64}\Z')

def account_data_state(connection,actor,pepper):
    if type(actor)is not int or actor<=0:raise ValueError('account_identity_unavailable')
    user=connection.execute('SELECT id,created_at FROM users WHERE id=?',(actor,)).fetchone()
    if user is None:raise ValueError('account_identity_unavailable')
    rows=connection.execute('SELECT request_id FROM account_reset_receipts11831 WHERE user_id=? ORDER BY request_id',(actor,)).fetchall()
    identifiers=[]
    for row in rows:
        value=row['request_id']
        if type(value)is not str or str(UUID(value))!=value:raise ValueError('invalid_account_reset_tombstone')
        identifiers.append(value)
    instance=server_instance_id(pepper)
    created=user['created_at']
    created=created if isinstance(created,datetime)else timestamp_from_db(created)
    if created is None:raise ValueError('account_creation_time_unavailable')
    if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
    creation=timestamp_to_db(created)
    raw=json.dumps(['account-data-generation11853-v1',instance,actor,creation,identifiers],
        ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')
    return {'actor_id':actor,'server_instance_id':instance,
        'data_generation':hashlib.sha256(raw).hexdigest(),'has_reset':bool(identifiers)}

def generation_matches(expected,state,*,require_after_reset=True):
    if expected is None:return not(require_after_reset and state['has_reset'])
    return type(expected)is str and HEX64.fullmatch(expected)is not None and hmac.compare_digest(expected,state['data_generation'])
