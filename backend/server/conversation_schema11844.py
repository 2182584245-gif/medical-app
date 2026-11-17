"""Explicit additive schema for source-bound extractive conversation memory.

No DDL runs at import or application startup. The reviewed 84-to-85 migration
owns the administrator transaction; original messages remain authoritative.
"""
from __future__ import annotations

SCHEMA = 'medical_app_platform'
RUNTIME = 'medical_app_platform_runtime'
TABLE = 'member_conversation_memory11844'
TABLES = (TABLE,)
INDEX = 'idx_conversation_memory_owner_status_time11844'
COLUMNS = ('user_id', 'conversation_id', 'segment_start', 'segment_end',
           'source_digest', 'source_refs_json', 'summary_json',
           'settings_revision', 'model_revision', 'status', 'created_at',
           'updated_at', 'last_error_code')
OWNED_MARKER = 'medical-app:platform_0001:owned'
OWNER = (f"current_user='{RUNTIME}' AND "
         "current_setting('medical_app.role_code',true)='member' AND "
         "user_id=NULLIF(current_setting('medical_app.user_id',true),'')::bigint")


def postgres_statements():
    target = SCHEMA + '.' + TABLE
    same_conversation = ("EXISTS(SELECT 1 FROM " + SCHEMA + ".conversations c "
                         "WHERE c.id=conversation_id AND c.user_id=user_id)")
    # Qualify the outer owner, otherwise c.user_id=user_id would compare the
    # conversation row with itself inside the correlated subquery.
    same_conversation = same_conversation.replace('c.user_id=user_id', 'c.user_id=' + TABLE + '.user_id')
    writable = '(' + OWNER + ') AND ' + same_conversation
    statements = [
        f'''CREATE TABLE {target}(
 user_id bigint NOT NULL REFERENCES {SCHEMA}.users(id) ON DELETE CASCADE CHECK(user_id>0),
 conversation_id bigint NOT NULL REFERENCES {SCHEMA}.conversations(id) ON DELETE CASCADE CHECK(conversation_id>0),
 segment_start bigint NOT NULL CHECK(segment_start>0),
 segment_end bigint NOT NULL CHECK(segment_end>=segment_start),
 source_digest text NOT NULL CHECK(source_digest ~ '^[0-9a-f]{{64}}$'),
 source_refs_json text NOT NULL CHECK(octet_length(source_refs_json)<=65536 AND source_refs_json IS JSON ARRAY),
 summary_json text NOT NULL CHECK(octet_length(summary_json)<=32768 AND summary_json IS JSON OBJECT),
 settings_revision text NOT NULL CHECK(length(settings_revision) BETWEEN 1 AND 128),
 model_revision text CHECK(model_revision IS NULL OR length(model_revision) BETWEEN 1 AND 160),
 status text NOT NULL CHECK(status IN('pending','ready','failed','stale')),
 created_at text NOT NULL CHECK(length(created_at) BETWEEN 1 AND 40),
 updated_at text NOT NULL CHECK(length(updated_at) BETWEEN 1 AND 40),
 last_error_code text CHECK(last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 120),
 CHECK(CASE WHEN source_refs_json IS JSON ARRAY THEN json_array_length(source_refs_json::json) BETWEEN 1 AND 40 ELSE false END),
 PRIMARY KEY(user_id,conversation_id,segment_start))''',
        f'CREATE INDEX {INDEX} ON {target}(user_id,status,updated_at DESC)',
        f"COMMENT ON TABLE {target} IS '{OWNED_MARKER}'",
        f'ALTER TABLE {target} ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE {target} FORCE ROW LEVEL SECURITY',
        f'REVOKE ALL ON {target} FROM PUBLIC',
        f'GRANT SELECT,INSERT,UPDATE,DELETE ON {target} TO {RUNTIME}',
    ]
    for command, using, check in (('SELECT', OWNER, None), ('INSERT', None, writable),
                                  ('UPDATE', OWNER, writable), ('DELETE', OWNER, None)):
        statements.append(f'CREATE POLICY conversation_owner_{command.lower()} ON {target} FOR {command} TO {RUNTIME}' +
                          (f' USING({using})' if using else '') +
                          (f' WITH CHECK({check})' if check else ''))
    return tuple(statements)


def verify_shape(database):
    """Read-only presence check; never creates or repairs production schema."""
    with database.connect() as connection:
        connection.execute('SELECT ' + ','.join(COLUMNS) + ' FROM ' + TABLE + ' LIMIT 0')
