"""Explicit additive try1 profile/vector/account migration. No startup DDL."""
from __future__ import annotations

SCHEMA='medical_app_platform'
RUNTIME='medical_app_platform_runtime'
EXTENSION_SCHEMA='medical_app_extensions'
VECTOR_VERSION='0.8.7'
TABLES=('member_profile_vectors11841','member_account_metadata11841','member_usage_sessions11841')
PROFILE_COLUMNS=('weight_kg','personal_introduction','phone_default_initialized','personal_introduction_updated_at')
OWNER=(f"current_user='{RUNTIME}' AND current_setting('medical_app.role_code',true)='member' "
       "AND user_id=NULLIF(current_setting('medical_app.user_id',true),'')::bigint")

SQLITE_ACCOUNT_DDL=(
 '''CREATE TABLE member_account_metadata11841(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 last_login_ip TEXT,foreground_ms INTEGER NOT NULL DEFAULT 0 CHECK(foreground_ms>=0),
 last_credited_at TEXT,usage_updated_at TEXT,updated_at TEXT NOT NULL)''',
 '''CREATE TABLE member_usage_sessions11841(
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 session_id TEXT NOT NULL CHECK(length(session_id)=36),event_id TEXT NOT NULL CHECK(length(event_id)=36),
 sequence_no INTEGER NOT NULL CHECK(sequence_no>=0),foreground INTEGER NOT NULL CHECK(foreground IN (0,1)),
 last_seen_at TEXT NOT NULL,PRIMARY KEY(user_id,session_id))''')

def initialize_sqlite_accounts(database):
    if getattr(database,'dialect','sqlite')=='postgresql':raise RuntimeError('Explicit administrator migration required')
    with database.transaction()as conn:
        for ddl in SQLITE_ACCOUNT_DDL:conn.execute(ddl.replace('CREATE TABLE ','CREATE TABLE IF NOT EXISTS ',1))

def verify_shape(database):
    with database.connect()as conn:
        conn.execute('SELECT '+','.join(PROFILE_COLUMNS)+' FROM member_profiles LIMIT 0')
        conn.execute('SELECT user_id,last_login_ip,foreground_ms,last_credited_at,usage_updated_at,updated_at FROM '+TABLES[1]+' LIMIT 0')
        conn.execute('SELECT user_id,session_id,event_id,sequence_no,foreground,last_seen_at FROM '+TABLES[2]+' LIMIT 0')
        if getattr(database,'dialect','sqlite')=='postgresql':
            conn.execute('SELECT user_id,profile_revision,model_revision,chunk_no,content,content_hash,embedding,updated_at FROM '+TABLES[0]+' LIMIT 0')

def postgres_statements():
    ext=EXTENSION_SCHEMA;s=SCHEMA
    statements=[
      f'''DO $try1_preflight$ DECLARE tables_count integer; columns_count integer; BEGIN
 SELECT count(*) INTO tables_count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='{s}' AND c.relname IN('{TABLES[0]}','{TABLES[1]}','{TABLES[2]}') AND c.relkind='r';
 SELECT count(*) INTO columns_count FROM information_schema.columns
 WHERE table_schema='{s}' AND table_name='member_profiles' AND column_name IN('weight_kg','personal_introduction','phone_default_initialized','personal_introduction_updated_at');
 IF tables_count NOT IN(0,3) OR columns_count NOT IN(0,4) OR (tables_count=0)<>(columns_count=0)
 THEN RAISE EXCEPTION 'Partial try1 migration must be reviewed'; END IF;
END $try1_preflight$''',
      f'CREATE SCHEMA IF NOT EXISTS {ext}',
      f'REVOKE ALL ON SCHEMA {ext} FROM PUBLIC',
      f'GRANT USAGE ON SCHEMA {ext} TO {RUNTIME}',
      f"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {ext} VERSION '{VECTOR_VERSION}'",
      f'''DO $try1_vector_extension$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
 WHERE e.extname='vector' AND e.extversion='{VECTOR_VERSION}' AND n.nspname='{ext}')
 THEN RAISE EXCEPTION 'Unexpected vector extension version or namespace'; END IF;
END $try1_vector_extension$''',
      f'''DO $try1_profile_columns$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='{s}' AND table_name='member_profiles' AND column_name='weight_kg') THEN
  ALTER TABLE {s}.member_profiles ADD COLUMN weight_kg double precision CHECK(weight_kg IS NULL OR (weight_kg>0 AND weight_kg<=1000));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='{s}' AND table_name='member_profiles' AND column_name='personal_introduction') THEN
  ALTER TABLE {s}.member_profiles ADD COLUMN personal_introduction text NOT NULL DEFAULT '' CHECK(length(personal_introduction)<=4000);
  UPDATE {s}.member_profiles SET personal_introduction=COALESCE(dietary_preferences,'');
 END IF;
 IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='{s}' AND table_name='member_profiles' AND column_name='phone_default_initialized') THEN
  ALTER TABLE {s}.member_profiles ADD COLUMN phone_default_initialized smallint NOT NULL DEFAULT 0 CHECK(phone_default_initialized IN(0,1));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='{s}' AND table_name='member_profiles' AND column_name='personal_introduction_updated_at') THEN
  ALTER TABLE {s}.member_profiles ADD COLUMN personal_introduction_updated_at text;
  -- The old schema cannot prove which profile field changed after a forget.
  -- Preserve every existing profile tombstone conservatively until a real
  -- introduction edit under the new schema supplies a fresh confirmation.
  UPDATE {s}.member_profiles p SET personal_introduction_updated_at=CASE
   WHEN EXISTS(SELECT 1 FROM {s}.member_memory_controls c
    WHERE c.user_id=p.user_id AND c.domain='profile' AND c.forget_before IS NOT NULL)
   THEN NULL ELSE p.updated_at END;
 END IF;
END $try1_profile_columns$''',
      f'''CREATE TABLE IF NOT EXISTS {s}.{TABLES[0]}(
 user_id bigint NOT NULL REFERENCES {s}.users(id) ON DELETE CASCADE,
 profile_revision text NOT NULL CHECK(profile_revision ~ '^[0-9a-f]{{64}}$'),
 model_revision text NOT NULL CHECK(length(model_revision)=40),
 chunk_no integer NOT NULL CHECK(chunk_no BETWEEN 0 AND 31),
 content text NOT NULL CHECK(length(content) BETWEEN 1 AND 450),
 content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{{64}}$'),
 embedding {ext}.vector(512) NOT NULL,
 updated_at text NOT NULL,
 PRIMARY KEY(user_id,profile_revision,model_revision,chunk_no))''',
      f'''CREATE TABLE IF NOT EXISTS {s}.{TABLES[1]}(
 user_id bigint PRIMARY KEY REFERENCES {s}.users(id) ON DELETE CASCADE,
 last_login_ip text CHECK(last_login_ip IS NULL OR (length(last_login_ip)<=45 AND last_login_ip::inet IS NOT NULL)),
 foreground_ms bigint NOT NULL DEFAULT 0 CHECK(foreground_ms>=0),
 last_credited_at text,usage_updated_at text,updated_at text NOT NULL)''',
      f'''CREATE TABLE IF NOT EXISTS {s}.{TABLES[2]}(
 user_id bigint NOT NULL REFERENCES {s}.users(id) ON DELETE CASCADE,
 session_id text NOT NULL CHECK(length(session_id)=36),
 event_id text NOT NULL CHECK(length(event_id)=36),
 sequence_no bigint NOT NULL CHECK(sequence_no BETWEEN 0 AND 9007199254740991),
 foreground smallint NOT NULL CHECK(foreground IN(0,1)),last_seen_at text NOT NULL,
 PRIMARY KEY(user_id,session_id))''',
      f'CREATE INDEX IF NOT EXISTS idx_usage_session_seen11841 ON {s}.{TABLES[2]}(user_id,last_seen_at)',
    ]
    for table in TABLES:
        target=s+'.'+table
        statements.extend((f'ALTER TABLE {target} ENABLE ROW LEVEL SECURITY',f'ALTER TABLE {target} FORCE ROW LEVEL SECURITY',
           f'REVOKE ALL ON {target} FROM PUBLIC',f'GRANT SELECT,INSERT,UPDATE,DELETE ON {target} TO {RUNTIME}'))
        for command,using,check in(('SELECT',OWNER,None),('INSERT',None,OWNER),('UPDATE',OWNER,OWNER),('DELETE',OWNER,None)):
            name='try1_owner_'+command.lower()
            statements.extend((f'DROP POLICY IF EXISTS {name} ON {target}',
                f'CREATE POLICY {name} ON {target} FOR {command} TO {RUNTIME}'+(f' USING({using})'if using else'')+(f' WITH CHECK({check})'if check else'')))
    from .goal_points_delete11841 import postgres_statements as goal_points_statements
    statements.extend(goal_points_statements())
    return tuple(statements)
