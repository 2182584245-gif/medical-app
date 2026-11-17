"""Explicit, additive administrator migration for member data resets.

No production DDL runs at import/startup. Existing identity, phone binding and
shared catalog grants are unchanged. Extra deletes require the private erasing
identity and the authenticated owner; there is no arbitrary-table HTTP surface.
"""
from __future__ import annotations

from .account_erasure import audit_owner_predicate

SCHEMA = 'medical_app_platform'
ROLE = 'medical_app_platform_runtime'
RECEIPT_TABLE = 'account_reset_receipts11831'
RECEIPT_COLUMNS = ('receipt_digest', 'user_id', 'request_id', 'old_token_digest',
                   'new_token_digest', 'password_marker', 'response_envelope',
                   'completed_at', 'expires_at')

# Authentication state survives a personal-data reset. Full account deletion
# still cascades these owner rows through users(id), including phone-only state.
AUTH_PRESERVED_TABLES11838 = ('phone_auth_bindings', 'phone_auth_credentials11838')

# Order is intentional: explicit history before parents and their FK children.
PERSONAL_OWNERS = (
    ('member_conversation_memory11844','user_id'),
    ('member_multimodal_vectors11842','user_id'),
    ('member_profile_vectors11841','user_id'),
    ('rpc_requests', 'user_id'),
    ('developer_pending_changes', 'target_user_id'),
    ('member_memory_events', 'user_id'), ('member_memory_daily', 'user_id'),
    ('member_memory_preferences', 'user_id'), ('member_memory_jobs', 'user_id'),
    ('member_memory_controls', 'user_id'), ('member_ai_preferences', 'user_id'),
    ('member_ai_preference_categories', 'user_id'),
    ('reading_comment_likes', 'user_id'), ('reading_comments', 'user_id'),
    ('reading_engagement', 'user_id'), ('reading_follows', 'user_id'),
    ('reading_shares', 'user_id'),
    ('forum_comment_requests', 'user_id'), ('forum_bookmarks', 'user_id'),
    ('forum_follows', 'user_id'), ('forum_likes', 'user_id'),
    ('forum_comments', 'author_user_id'), ('forum_posts', 'author_user_id'),
    ('expansion_record_revisions', 'user_id'), ('expansion_meals', 'user_id'),
    ('expansion_analyses', 'user_id'), ('expansion_memories', 'user_id'),
    ('expansion_observations', 'user_id'), ('expansion_habit_entries', 'user_id'),
    ('expansion_goals', 'user_id'), ('expansion_content_progress', 'user_id'),
    ('expansion_point_events', 'user_id'), ('expansion_invitation_codes', 'user_id'),
    ('expansion_settings', 'user_id'), ('expansion_food_favorites', 'user_id'),
    ('expansion_foods', 'owner_user_id'),
    ('orders', 'member_user_id'), ('product_recommendations', 'member_user_id'),
    ('visit_records', 'member_user_id'), ('visit_tasks', 'member_user_id'),
    ('advisor_summaries', 'member_user_id'), ('advisor_bindings', 'member_user_id'),
    ('memberships', 'user_id'), ('conversations', 'user_id'),
    ('medical_reports', 'user_id'), ('user_files', 'user_id'),
    ('ai_insights', 'user_id'), ('life_records', 'user_id'),
    ('reminders', 'user_id'), ('profile_facts', 'user_id'),
    ('member_profiles', 'user_id'), ('app_settings', 'user_id'),
    ('user_preferences', 'user_id'),
)

SQLITE_DDL = f'''CREATE TABLE {RECEIPT_TABLE}(
 receipt_digest TEXT PRIMARY KEY CHECK(length(receipt_digest)=64),
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 request_id TEXT NOT NULL CHECK(length(request_id)=36),
 old_token_digest TEXT NOT NULL CHECK(length(old_token_digest)=64),
 new_token_digest TEXT CHECK(new_token_digest IS NULL OR length(new_token_digest)=64),
 password_marker TEXT CHECK(password_marker IS NULL OR length(password_marker)=64),
 response_envelope TEXT CHECK(response_envelope IS NULL OR length(response_envelope)<=32768),
 completed_at INTEGER NOT NULL,
 expires_at INTEGER NOT NULL CHECK(expires_at>completed_at),
 UNIQUE(user_id,request_id))'''


def initialize_sqlite(database):
    """Disposable SQLite fixtures only; never a production auto-migration."""
    if getattr(database, 'dialect', 'sqlite') == 'postgresql':
        raise RuntimeError('Account reset requires an administrator migration')
    with database.transaction() as connection:
        connection.execute(SQLITE_DDL)


def verify_shape(database):
    with database.connect() as connection:
        connection.execute('SELECT '+','.join(RECEIPT_COLUMNS)+' FROM '+RECEIPT_TABLE+' LIMIT 0')


def postgres_statements():
    """Repeatable administrator-only DDL; callers execute in one transaction."""
    actor = "NULLIF(current_setting('medical_app.user_id',true),'')::bigint"
    token = "NULLIF(current_setting('medical_app.token_digest',true),'')"
    runtime = f"current_user='{ROLE}'"
    private = f"{runtime} AND current_setting('medical_app.role_code',true)='member' AND current_setting('medical_app.erasing',true)='true'"
    actual = SCHEMA+'.'+RECEIPT_TABLE
    sql = SQLITE_DDL.replace('INTEGER', 'BIGINT').replace('REFERENCES users(', 'REFERENCES '+SCHEMA+'.users(')
    # PostgreSQL temp tables cannot reference a permanent users table. Compare
    # scalar/PK/UNIQUE/CHECK structure here; check the one FK separately below.
    expected = sql.replace(' REFERENCES '+SCHEMA+'.users(id) ON DELETE CASCADE', '').replace('CREATE TABLE '+RECEIPT_TABLE+'(', 'CREATE TEMP TABLE account_reset_expected11831(') + ' ON COMMIT DROP'
    create = sql.replace('CREATE TABLE '+RECEIPT_TABLE+'(', 'CREATE TABLE IF NOT EXISTS '+actual+'(')
    statements = [expected, create, f'''DO $reset_shape$
DECLARE expected oid:='pg_temp.account_reset_expected11831'::regclass; actual oid:='{actual}'::regclass;
BEGIN
 IF (SELECT jsonb_agg(jsonb_build_array(attname,format_type(atttypid,atttypmod),attnotnull) ORDER BY attnum) FROM pg_attribute WHERE attrelid=actual AND attnum>0 AND NOT attisdropped)
 IS DISTINCT FROM (SELECT jsonb_agg(jsonb_build_array(attname,format_type(atttypid,atttypmod),attnotnull) ORDER BY attnum) FROM pg_attribute WHERE attrelid=expected AND attnum>0 AND NOT attisdropped)
 OR (SELECT array_agg(pg_get_constraintdef(oid,true) ORDER BY pg_get_constraintdef(oid,true)) FROM pg_constraint WHERE conrelid=actual AND contype<>'f')
 IS DISTINCT FROM (SELECT array_agg(pg_get_constraintdef(oid,true) ORDER BY pg_get_constraintdef(oid,true)) FROM pg_constraint WHERE conrelid=expected)
 OR (SELECT count(*) FROM pg_constraint WHERE conrelid=actual AND contype='f')<>1
 OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=actual AND contype='f' AND confrelid='{SCHEMA}.users'::regclass AND confdeltype='c' AND conkey=ARRAY[2]::smallint[] AND confkey=ARRAY[1]::smallint[])
 THEN RAISE EXCEPTION 'Account reset receipt schema differs from reviewed structure'; END IF;
END $reset_shape$''',
        f'ALTER TABLE {actual} ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE {actual} FORCE ROW LEVEL SECURITY',
        f'REVOKE ALL ON {actual} FROM PUBLIC',
        f'GRANT SELECT,INSERT,UPDATE ON {actual} TO {ROLE}',
        f'DROP POLICY IF EXISTS account_reset_read ON {actual}',
        f"CREATE POLICY account_reset_read ON {actual} FOR SELECT TO {ROLE} USING ({runtime} AND (old_token_digest={token} OR user_id={actor} OR expires_at<=floor(extract(epoch FROM clock_timestamp()))::bigint))",
        f'DROP POLICY IF EXISTS account_reset_insert ON {actual}',
        f'CREATE POLICY account_reset_insert ON {actual} FOR INSERT TO {ROLE} WITH CHECK ({private} AND user_id={actor})',
        f'DROP POLICY IF EXISTS account_reset_update ON {actual}',
        f'CREATE POLICY account_reset_update ON {actual} FOR UPDATE TO {ROLE} USING (({private} AND user_id={actor}) OR ({runtime} AND expires_at<=floor(extract(epoch FROM clock_timestamp()))::bigint)) WITH CHECK (({private} AND user_id={actor}) OR ({runtime} AND expires_at<=floor(extract(epoch FROM clock_timestamp()))::bigint AND new_token_digest IS NULL AND password_marker IS NULL AND response_envelope IS NULL))',
    ]
    # Derived vector indexes are created later by their explicit migrations
    # and already grant owner DELETE. Keep this older migration independently
    # runnable before those additive tables exist.
    predicates = {name:column+'='+actor for name,column in PERSONAL_OWNERS
                  if name not in {'member_profile_vectors11841','member_multimodal_vectors11842','member_conversation_memory11844'}}
    predicates['expansion_referrals'] = f'inviter_user_id={actor} OR invitee_user_id={actor}'
    predicates['audit_logs'] = audit_owner_predicate(actor, qualifier='audit_logs', qualified_tables=True)
    # No grants or policies are added for users, bindings, shared catalogs or
    # phone credentials. Their existing authentication policies remain intact.
    for name, predicate in predicates.items():
        table=SCHEMA+'.'+name
        statements.extend((f'GRANT SELECT,DELETE ON {table} TO {ROLE}',
            f'DROP POLICY IF EXISTS account_reset_read ON {table}',
            f'CREATE POLICY account_reset_read ON {table} FOR SELECT TO {ROLE} USING ({private} AND ({predicate}))',
            f'DROP POLICY IF EXISTS account_reset_delete ON {table}',
            f'CREATE POLICY account_reset_delete ON {table} FOR DELETE TO {ROLE} USING ({private} AND ({predicate}))'))
    return tuple(statements)
