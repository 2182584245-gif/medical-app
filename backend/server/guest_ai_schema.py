"""Additive Guest-only schema. DDL is administrator reviewed, never startup DDL."""
from __future__ import annotations

SCHEMA='medical_app_platform'
ROLE='medical_app_platform_runtime'
TABLES=('guest_ai_sessions','guest_ai_media','guest_ai_retired')
SQLITE_DDL=(
 "CREATE TABLE guest_ai_sessions(token_digest TEXT PRIMARY KEY,guest_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),state_json TEXT NOT NULL)",
 "CREATE TABLE guest_ai_media(token_digest TEXT NOT NULL REFERENCES guest_ai_sessions(token_digest) ON DELETE CASCADE,file_id INTEGER NOT NULL,original_name TEXT NOT NULL,media_type TEXT NOT NULL,content BLOB NOT NULL,extracted_text TEXT NOT NULL,sha256 TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(token_digest,file_id))",
 "CREATE TABLE guest_ai_retired(token_digest TEXT PRIMARY KEY,retired_at TEXT NOT NULL)",
)


def initialize_sqlite(database):
 if getattr(database,'dialect','sqlite')=='postgresql':raise RuntimeError('Guest schema migration is administrator-only')
 with database.transaction()as connection:
  for statement in SQLITE_DDL:connection.execute(statement)


def postgres_statements():
 actor="NULLIF(current_setting('medical_app.guest_digest',true),'')"
 statements=[]
 for statement in SQLITE_DDL:
  sql=statement.replace('INTEGER','BIGINT').replace(' BLOB ',' BYTEA ')
  for name in TABLES:sql=sql.replace('TABLE '+name+'(', 'TABLE '+SCHEMA+'.'+name+'(').replace('REFERENCES '+name+'(', 'REFERENCES '+SCHEMA+'.'+name+'(')
  statements.append(sql)
 statements += [
  f"ALTER TABLE {SCHEMA}.guest_ai_sessions ADD CONSTRAINT guest_digest_shape CHECK(token_digest ~ '^[a-f0-9]{{64}}$')",
  f"ALTER TABLE {SCHEMA}.guest_ai_sessions ADD CONSTRAINT guest_state_bound CHECK(state_json IS JSON OBJECT AND octet_length(state_json)<=33554432)",
  f"ALTER TABLE {SCHEMA}.guest_ai_media ADD CONSTRAINT guest_media_bound CHECK(octet_length(content)>0 AND octet_length(content)<=15728640 AND sha256 ~ '^[a-f0-9]{{64}}$')",
 ]
 for name in TABLES:
  table=SCHEMA+'.'+name;predicate=f"current_user='{ROLE}' AND token_digest={actor}"
  statements += [f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY',f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY',
   f'REVOKE ALL ON {table} FROM PUBLIC',f'GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO {ROLE}',
   f'CREATE POLICY guest_read ON {table} FOR SELECT TO {ROLE} USING ({predicate})',
   f'CREATE POLICY guest_insert ON {table} FOR INSERT TO {ROLE} WITH CHECK ({predicate})',
   f'CREATE POLICY guest_update ON {table} FOR UPDATE TO {ROLE} USING ({predicate}) WITH CHECK ({predicate})',
   f'CREATE POLICY guest_delete ON {table} FOR DELETE TO {ROLE} USING ({predicate})']
 return tuple(statements)


def backup_exclude_arguments():
 """Every app-owned dump/export must exclude Guest content while keeping DDL."""
 return tuple('--exclude-table-data='+SCHEMA+'.'+name for name in ('guest_ai_sessions','guest_ai_media'))
