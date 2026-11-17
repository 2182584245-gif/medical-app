"""One explicit administrator migration for owner-scoped Qwen retrieval.

This table is a derived index. Original business rows and files remain the
authority. No binary, public URL, credential, startup DDL, or legacy BGE vector
is copied here. Network authorization and source revalidation belong to the
retrieval service; row-level security independently constrains every owner.
"""
from __future__ import annotations

SCHEMA = 'medical_app_platform'
RUNTIME = 'medical_app_platform_runtime'
EXTENSION_SCHEMA = 'medical_app_extensions'
TABLE = 'member_multimodal_vectors11842'
TABLES = (TABLE,)
DIMENSION = 512
OWNED_MARKER = 'medical-app:platform_0001:owned'
COLUMNS = ('user_id', 'source_ref', 'domain', 'source_hash', 'model_revision',
           'chunk_no', 'modality', 'content', 'content_hash', 'image_source_ref',
           'image_sha256', 'occurred_at', 'updated_at', 'embedding')
DOMAINS = ('profile', 'life_records', 'reminders', 'goals_habits',
           'services_appointments', 'reading', 'shopping', 'forum',
           'operations', 'preferences', 'ai_records')
SOURCE_TABLES = ('life_records', 'expansion_observations', 'reminders',
                 'expansion_goals', 'expansion_habit_entries', 'visit_records',
                 'visit_tasks', 'expansion_content_progress', 'orders',
                 'forum_posts', 'forum_comments', 'forum_likes',
                 'member_memory_events', 'messages', 'chat_attachments',
                 'expansion_meals', 'medical_reports')
OWNER = (f"current_user='{RUNTIME}' AND "
         "current_setting('medical_app.role_code',true)='member' AND "
         "user_id=NULLIF(current_setting('medical_app.user_id',true),'')::bigint")


def postgres_statements():
    """Fresh-table DDL only; the reviewed 83→84 runner owns the transaction.

    An existing or partial table is deliberately not repaired. The runner
    verifies the exact old catalog and the independently generated new typed
    contract before committing, or verifies an exact completed migration.
    """
    target = SCHEMA + '.' + TABLE
    source_pattern = ('^(member_profiles:current|(' + '|'.join(SOURCE_TABLES) +
                      '):[A-Za-z0-9][A-Za-z0-9_.:-]{0,159})$')
    domains = ','.join("'" + value + "'" for value in DOMAINS)
    statements = [
        f'''DO $qwen_extension_preflight$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
 WHERE e.extname='vector' AND e.extversion='0.8.7' AND n.nspname='{EXTENSION_SCHEMA}')
 THEN RAISE EXCEPTION 'Reviewed vector extension required'; END IF;
END $qwen_extension_preflight$''',
        f'''CREATE TABLE {target}(
 user_id bigint NOT NULL REFERENCES {SCHEMA}.users(id) ON DELETE CASCADE CHECK(user_id>0),
 source_ref text NOT NULL CHECK(length(source_ref)<=200 AND source_ref ~ '{source_pattern}'),
 domain text NOT NULL CHECK(domain IN({domains})),
 source_hash text NOT NULL CHECK(source_hash ~ '^[0-9a-f]{{64}}$'),
 model_revision text NOT NULL CHECK(model_revision ~ '^[0-9a-f]{{40}}$'),
 chunk_no integer NOT NULL CHECK(chunk_no BETWEEN 0 AND 31),
 modality text NOT NULL CHECK(modality IN('text','image')),
 content text NOT NULL CHECK(length(content)<=2000),
 content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{{64}}$'),
 image_source_ref text CHECK(image_source_ref IS NULL OR image_source_ref ~ '^(user_files|chat_attachments):[1-9][0-9]{{0,18}}$'),
 image_sha256 text CHECK(image_sha256 IS NULL OR image_sha256 ~ '^[0-9a-f]{{64}}$'),
 occurred_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 embedding {EXTENSION_SCHEMA}.vector({DIMENSION}) NOT NULL,
 CHECK((modality='text' AND length(content)>0 AND image_source_ref IS NULL AND image_sha256 IS NULL)
    OR (modality='image' AND image_source_ref IS NOT NULL AND image_sha256 IS NOT NULL)),
 PRIMARY KEY(user_id,source_ref,model_revision,modality,chunk_no))''',
        f'CREATE INDEX idx_multimodal_owner_model_domain_time11842 ON {target}(user_id,model_revision,domain,occurred_at)',
        f"COMMENT ON TABLE {target} IS '{OWNED_MARKER}'",
        f'ALTER TABLE {target} ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE {target} FORCE ROW LEVEL SECURITY',
        f'REVOKE ALL ON {target} FROM PUBLIC',
        f'GRANT SELECT,INSERT,UPDATE,DELETE ON {target} TO {RUNTIME}',
    ]
    for command, using, check in (('SELECT', OWNER, None), ('INSERT', None, OWNER),
                                  ('UPDATE', OWNER, OWNER), ('DELETE', OWNER, None)):
        statements.append(f'CREATE POLICY qwen_owner_{command.lower()} ON {target} FOR {command} TO {RUNTIME}' +
                          (f' USING({using})' if using else '') +
                          (f' WITH CHECK({check})' if check else ''))
    return tuple(statements)


def verify_shape(database):
    """Runtime read-only presence check; never creates or repairs a table."""
    if getattr(database, 'dialect', 'sqlite') != 'postgresql':
        raise RuntimeError('Qwen retrieval requires the reviewed cloud migration')
    with database.connect() as connection:
        connection.execute('SELECT ' + ','.join(COLUMNS) + ' FROM ' + TABLE + ' LIMIT 0')
