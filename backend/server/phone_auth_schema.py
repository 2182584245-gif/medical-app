"""Reviewed additive phone-auth schema. Production migration is administrator-only."""
from __future__ import annotations

from .account_erasure import audit_owner_predicate

SCHEMA = 'medical_app_platform'
ROLE = 'medical_app_platform_runtime'
COLUMNS = {
    'phone_auth_bindings': ('user_id','phone','verified_at'),
    'phone_auth_challenges': ('challenge_id','phone','purpose','user_id','code_digest','created_at','expires_at','attempts','state'),
    'phone_auth_limits': ('scope_key','window_start','attempts'),
    'phone_auth_refreshes': ('token_digest','user_id','family_id','access_digest','password_marker','created_at','expires_at','revoked_at','rotated_to','request_digest'),
    'phone_auth_erase_receipts': ('receipt_digest','completed_at','expires_at'),
}
SQLITE_DDL = (
    'CREATE TABLE phone_auth_bindings(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,phone TEXT NOT NULL UNIQUE,verified_at TEXT NOT NULL)',
    "CREATE TABLE phone_auth_challenges(challenge_id TEXT PRIMARY KEY,phone TEXT NOT NULL,purpose TEXT NOT NULL CHECK(purpose IN ('register','login','bind','change')),user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,code_digest TEXT NOT NULL,created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),state TEXT NOT NULL CHECK(state IN ('pending','sent','failed','consumed')))",
    'CREATE TABLE phone_auth_limits(scope_key TEXT PRIMARY KEY,window_start INTEGER NOT NULL,attempts INTEGER NOT NULL CHECK(attempts>=0))',
    'CREATE TABLE phone_auth_refreshes(token_digest TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,family_id TEXT NOT NULL,access_digest TEXT NOT NULL,password_marker TEXT NOT NULL,created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,revoked_at INTEGER,rotated_to TEXT,request_digest TEXT)',
    'CREATE TABLE phone_auth_erase_receipts(receipt_digest TEXT PRIMARY KEY,completed_at INTEGER NOT NULL,expires_at INTEGER NOT NULL)',
    'CREATE INDEX idx_phone_challenge_phone ON phone_auth_challenges(phone,purpose,created_at)',
    'CREATE INDEX idx_phone_refresh_user ON phone_auth_refreshes(user_id,expires_at)',
)


def initialize_sqlite(database):
    """Explicit disposable-test setup, never called by production startup."""
    if getattr(database, 'dialect', 'sqlite') == 'postgresql':
        raise RuntimeError('Production phone-auth schema migration is administrator-only')
    with database.transaction() as connection:
        for statement in SQLITE_DDL:
            connection.execute(statement)
        # Explicit disposable/local fixture setup stays complete for existing
        # callers. Production still uses only the administrator migration.
        from .account_reset_schema11831 import SQLITE_DDL as reset_ddl
        connection.execute(reset_ddl)
        from .phone_credentials_schema11838 import SQLITE_DDL as credentials_ddl
        connection.execute(credentials_ddl)
        connection.execute('CREATE TABLE password_signup_members (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE)')


def postgres_statements():
    actor = "NULLIF(current_setting('medical_app.user_id',true),'')::bigint"
    phone = "NULLIF(current_setting('medical_app.auth_phone',true),'')"
    refresh = "NULLIF(current_setting('medical_app.auth_refresh_digest',true),'')"
    erasing = "current_setting('medical_app.erasing',true)='true'"
    runtime = f"current_user='{ROLE}'"
    statements = []
    for statement in SQLITE_DDL:
        sql = statement.replace('INTEGER', 'BIGINT')
        for name in (*COLUMNS, 'users'):
            sql = sql.replace('TABLE ' + name + '(', 'TABLE ' + SCHEMA + '.' + name + '(')
            sql = sql.replace('REFERENCES ' + name + '(', 'REFERENCES ' + SCHEMA + '.' + name + '(')
            sql = sql.replace(' ON ' + name + '(', ' ON ' + SCHEMA + '.' + name + '(')
        statements.append(sql)
    statements += [
        f"ALTER TABLE {SCHEMA}.phone_auth_bindings ADD CONSTRAINT phone_auth_e164 CHECK(phone ~ '^\\+861[3-9][0-9]{{9}}$')",
        f"ALTER TABLE {SCHEMA}.phone_auth_challenges ADD CONSTRAINT phone_auth_digest CHECK(code_digest ~ '^[a-f0-9]{{64}}$' AND expires_at>created_at)",
        f"ALTER TABLE {SCHEMA}.phone_auth_refreshes ADD CONSTRAINT phone_refresh_digest CHECK(token_digest ~ '^[a-f0-9]{{64}}$' AND expires_at>created_at)",
    ]
    predicates = {
        'phone_auth_bindings': (f'user_id={actor} OR phone={phone}', f'user_id={actor}'),
        'phone_auth_challenges': (f'(phone={phone} AND (user_id IS NULL OR user_id={actor})) OR ({erasing} AND user_id={actor})', f'phone={phone} AND (user_id IS NULL OR user_id={actor})'),
        'phone_auth_limits': ('TRUE','TRUE'),
        'phone_auth_refreshes': (f'user_id={actor} OR token_digest={refresh}', f'user_id={actor}'),
        'phone_auth_erase_receipts': ("receipt_digest=NULLIF(current_setting('medical_app.erase_receipt_digest',true),'')", f"{erasing} AND receipt_digest=NULLIF(current_setting('medical_app.erase_receipt_digest',true),'')"),
    }
    for name, (read, write) in predicates.items():
        table = SCHEMA + '.' + name
        statements += [f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY', f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY',
                       f'REVOKE ALL ON {table} FROM PUBLIC',
                       f'GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO {ROLE}',
                       f'CREATE POLICY phone_read ON {table} FOR SELECT TO {ROLE} USING ({runtime} AND ({read}))',
                       f'CREATE POLICY phone_insert ON {table} FOR INSERT TO {ROLE} WITH CHECK ({runtime} AND ({write}))',
                       f'CREATE POLICY phone_update ON {table} FOR UPDATE TO {ROLE} USING ({runtime} AND ({write})) WITH CHECK ({runtime} AND ({write}))',
                       f'CREATE POLICY phone_delete ON {table} FOR DELETE TO {ROLE} USING ({runtime} AND ({write}))']
    explicit = {
        'users': f'id={actor}', 'orders': f'member_user_id={actor}',
        'developer_pending_changes': f'target_user_id={actor} OR created_by={actor}',
        'developer_settings_snapshots': f'created_by={actor}',
        'audit_logs': audit_owner_predicate(actor, qualifier='audit_logs', qualified_tables=True),
    }
    for name, predicate in explicit.items():
        statements += [f'GRANT DELETE ON {SCHEMA}.{name} TO {ROLE}',
                       f'CREATE POLICY phone_erasure_read ON {SCHEMA}.{name} FOR SELECT TO {ROLE} USING ({runtime} AND {erasing} AND ({predicate}))',
                       f'CREATE POLICY phone_self_erasure ON {SCHEMA}.{name} FOR DELETE TO {ROLE} USING ({runtime} AND {erasing} AND ({predicate}))']
    # PostgreSQL DELETE with a WHERE clause also applies SELECT visibility.
    # Expose only expired rows for bounded TTL removal, never live proofs or
    # active device credentials. No lifecycle record is retained indefinitely.
    expired = 'expires_at<=floor(extract(epoch FROM clock_timestamp()))::bigint'
    for name in ('phone_auth_challenges','phone_auth_refreshes','phone_auth_erase_receipts'):
        statements += [f'CREATE POLICY phone_expired_read ON {SCHEMA}.{name} FOR SELECT TO {ROLE} USING ({runtime} AND {expired})',
                       f'CREATE POLICY phone_expired_delete ON {SCHEMA}.{name} FOR DELETE TO {ROLE} USING ({runtime} AND {expired})']
    return tuple(statements)


def verify_shape(database):
    """Read-only startup verification; never auto-create/migrate live tables."""
    with database.connect() as connection:
        for name, columns in COLUMNS.items():
            connection.execute('SELECT ' + ','.join(columns) + ' FROM ' + name + ' LIMIT 0')
