"""Additive, administrator-only migration for optional phone-account credentials."""
from __future__ import annotations

SCHEMA = 'medical_app_platform'
ROLE = 'medical_app_platform_runtime'
TABLE = 'phone_auth_credentials11838'
COLUMNS = ('user_id', 'credentials_set', 'created_at', 'completed_at', 'setup_request_id')
SQLITE_DDL = f'''CREATE TABLE {TABLE}(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 credentials_set INTEGER NOT NULL CHECK(credentials_set IN (0,1)),
 created_at TEXT NOT NULL,
 completed_at TEXT,
 setup_request_id TEXT,
 CHECK((credentials_set=0 AND completed_at IS NULL AND setup_request_id IS NULL)
 OR (credentials_set=1 AND completed_at IS NOT NULL AND setup_request_id IS NOT NULL AND length(setup_request_id)=36)))'''


def verify_shape(database):
    """Read only: production startup must never create or alter tables."""
    with database.connect() as connection:
        connection.execute('SELECT '+','.join(COLUMNS)+' FROM '+TABLE+' LIMIT 0')


def postgres_statements():
    """Run explicitly as the migration administrator in one transaction."""
    actual = SCHEMA+'.'+TABLE
    sql = SQLITE_DDL.replace('INTEGER', 'BIGINT').replace('REFERENCES users(', 'REFERENCES '+SCHEMA+'.users(')
    expected = sql.replace(' REFERENCES '+SCHEMA+'.users(id) ON DELETE CASCADE', '').replace('CREATE TABLE '+TABLE+'(', 'CREATE TEMP TABLE phone_credentials_expected11838(')+' ON COMMIT DROP'
    create = sql.replace('CREATE TABLE '+TABLE+'(', 'CREATE TABLE IF NOT EXISTS '+actual+'(')
    actor = "NULLIF(current_setting('medical_app.user_id',true),'')::bigint"
    owner = f"current_user='{ROLE}' AND current_setting('medical_app.role_code',true)='member' AND user_id={actor}"
    return (expected, create, f'''DO $credentials_shape$
DECLARE expected oid:='pg_temp.phone_credentials_expected11838'::regclass; actual oid:='{actual}'::regclass;
BEGIN
 IF (SELECT jsonb_agg(jsonb_build_array(attname,format_type(atttypid,atttypmod),attnotnull) ORDER BY attnum) FROM pg_attribute WHERE attrelid=actual AND attnum>0 AND NOT attisdropped)
 IS DISTINCT FROM (SELECT jsonb_agg(jsonb_build_array(attname,format_type(atttypid,atttypmod),attnotnull) ORDER BY attnum) FROM pg_attribute WHERE attrelid=expected AND attnum>0 AND NOT attisdropped)
 OR (SELECT array_agg(pg_get_constraintdef(oid,true) ORDER BY pg_get_constraintdef(oid,true)) FROM pg_constraint WHERE conrelid=actual AND contype<>'f')
 IS DISTINCT FROM (SELECT array_agg(pg_get_constraintdef(oid,true) ORDER BY pg_get_constraintdef(oid,true)) FROM pg_constraint WHERE conrelid=expected)
 OR (SELECT count(*) FROM pg_constraint WHERE conrelid=actual AND contype='f')<>1
 OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=actual AND contype='f' AND confrelid='{SCHEMA}.users'::regclass AND confdeltype='c' AND conkey=ARRAY[1]::smallint[] AND confkey=ARRAY[1]::smallint[])
 THEN RAISE EXCEPTION 'Phone credentials schema differs from reviewed structure'; END IF;
END $credentials_shape$''',
        f'ALTER TABLE {actual} ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE {actual} FORCE ROW LEVEL SECURITY',
        f'REVOKE ALL ON {actual} FROM PUBLIC',
        f'GRANT SELECT,INSERT,UPDATE,DELETE ON {actual} TO {ROLE}',
        f'DROP POLICY IF EXISTS credentials_read ON {actual}',
        f'CREATE POLICY credentials_read ON {actual} FOR SELECT TO {ROLE} USING ({owner})',
        f'DROP POLICY IF EXISTS credentials_insert ON {actual}',
        f"CREATE POLICY credentials_insert ON {actual} FOR INSERT TO {ROLE} WITH CHECK ({owner} AND current_setting('medical_app.registering',true)='true' AND credentials_set=0)",
        f'DROP POLICY IF EXISTS credentials_update ON {actual}',
        f'CREATE POLICY credentials_update ON {actual} FOR UPDATE TO {ROLE} USING ({owner}) WITH CHECK ({owner})',
        f'DROP POLICY IF EXISTS credentials_delete ON {actual}',
        f"CREATE POLICY credentials_delete ON {actual} FOR DELETE TO {ROLE} USING ({owner} AND current_setting('medical_app.erasing',true)='true')")
