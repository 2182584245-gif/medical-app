-- member_ai_preferences_0001; additive literal user-preference documents.
-- No business-table changes, model calls, triggers, functions or role changes.
DO $prerequisites$
BEGIN
  IF current_user = 'medical_app_platform_runtime'
     OR to_regclass('medical_app_platform.users') IS NULL
     OR to_regclass('medical_app_platform.member_memory_controls') IS NULL
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='medical_app_platform_runtime') THEN
    RAISE EXCEPTION 'Existing reviewed platform and migration owner required';
  END IF;
END
$prerequisites$;

-- statement boundary

-- A transaction-local structural reference lets PostgreSQL compare its own
-- canonical definitions without trusting a pre-existing table's marker alone.
CREATE TEMP TABLE member_ai_preferences_expected (
    user_id bigint NOT NULL,
    entries_json text NOT NULL CHECK (octet_length(entries_json) >= 2 AND octet_length(entries_json) <= 32768 AND jsonb_typeof(entries_json::jsonb) = 'array' AND jsonb_array_length(entries_json::jsonb) <= 20),
    revision bigint NOT NULL CHECK (revision > 0),
    last_request_id text NOT NULL CHECK (last_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    last_request_hash text NOT NULL CHECK (last_request_hash ~ '^[0-9a-f]{64}$'),
    created_at text NOT NULL CHECK (created_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND created_at::timestamptz IS NOT NULL),
    updated_at text NOT NULL CHECK (updated_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND updated_at::timestamptz IS NOT NULL),
    CONSTRAINT member_ai_preferences_expected_timestamp_order CHECK (created_at::timestamptz <= updated_at::timestamptz),
    PRIMARY KEY (user_id)
) ON COMMIT DROP;

-- statement boundary

DO $member_ai_preferences_v1$
DECLARE
  actual oid;
  expected oid := 'pg_temp.member_ai_preferences_expected'::regclass;
  policy_expression text := '((CURRENT_USER = ''medical_app_platform_runtime''::name) AND (current_setting(''medical_app.role_code''::text, true) = ''member''::text) AND (user_id = (NULLIF(current_setting(''medical_app.user_id''::text, true), ''''::text))::bigint))';
BEGIN
  actual := to_regclass('medical_app_platform.member_ai_preferences');
  IF actual IS NULL THEN
    CREATE TABLE medical_app_platform.member_ai_preferences (
      user_id bigint NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
      entries_json text NOT NULL CHECK (octet_length(entries_json) >= 2 AND octet_length(entries_json) <= 32768 AND jsonb_typeof(entries_json::jsonb) = 'array' AND jsonb_array_length(entries_json::jsonb) <= 20),
      revision bigint NOT NULL CHECK (revision > 0),
      last_request_id text NOT NULL CHECK (last_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
      last_request_hash text NOT NULL CHECK (last_request_hash ~ '^[0-9a-f]{64}$'),
      created_at text NOT NULL CHECK (created_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND created_at::timestamptz IS NOT NULL),
      updated_at text NOT NULL CHECK (updated_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND updated_at::timestamptz IS NOT NULL),
      CONSTRAINT member_ai_preferences_timestamp_order CHECK (created_at::timestamptz <= updated_at::timestamptz),
      PRIMARY KEY (user_id)
    );
    ALTER TABLE medical_app_platform.member_ai_preferences ENABLE ROW LEVEL SECURITY;
    ALTER TABLE medical_app_platform.member_ai_preferences FORCE ROW LEVEL SECURITY;
    REVOKE ALL ON TABLE medical_app_platform.member_ai_preferences FROM PUBLIC;
    GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE medical_app_platform.member_ai_preferences TO medical_app_platform_runtime;
    CREATE POLICY member_ai_preferences_owner ON medical_app_platform.member_ai_preferences FOR ALL TO medical_app_platform_runtime
      USING (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint)
      WITH CHECK (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint);
    COMMENT ON TABLE medical_app_platform.member_ai_preferences IS 'member_ai_preferences_0001:user-explicit-v1';
    actual := 'medical_app_platform.member_ai_preferences'::regclass;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid=actual AND relkind='r'
                 AND pg_get_userbyid(relowner)=current_user AND relrowsecurity AND relforcerowsecurity
                 AND obj_description(oid,'pg_class')='member_ai_preferences_0001:user-explicit-v1')
     OR (SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY a.attnum)
         FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
         WHERE a.attrelid=actual AND a.attnum>0 AND NOT a.attisdropped)
        IS DISTINCT FROM
        (SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY a.attnum)
         FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
         WHERE a.attrelid=expected AND a.attnum>0 AND NOT a.attisdropped)
     OR (SELECT jsonb_agg(jsonb_build_array(contype,pg_get_constraintdef(oid,false)) ORDER BY contype,pg_get_constraintdef(oid,false)) FROM pg_constraint WHERE conrelid=actual AND contype<>'f')
        IS DISTINCT FROM
        (SELECT jsonb_agg(jsonb_build_array(contype,pg_get_constraintdef(oid,false)) ORDER BY contype,pg_get_constraintdef(oid,false)) FROM pg_constraint WHERE conrelid=expected)
     OR (SELECT count(*) FROM pg_constraint WHERE conrelid=actual AND contype='f') <> 1
     OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=actual AND contype='f' AND convalidated
                    AND confrelid='medical_app_platform.users'::regclass AND confdeltype='c'
                    AND confupdtype='a' AND confmatchtype='s' AND NOT condeferrable AND NOT condeferred
                    AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid=actual AND attname='user_id')]
                    AND confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='medical_app_platform.users'::regclass AND attname='id')])
     OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=actual AND NOT convalidated)
     OR (SELECT count(*) FROM pg_policy WHERE polrelid=actual) <> 1
     OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid=actual AND polname='member_ai_preferences_owner'
                    AND polcmd='*' AND polpermissive
                    AND polroles=ARRAY[(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')]
                    AND pg_get_expr(polqual,polrelid)=policy_expression AND pg_get_expr(polwithcheck,polrelid)=policy_expression)
     OR EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=actual AND NOT tgisinternal)
     OR (SELECT count(*) FROM pg_index WHERE indrelid=actual) <> 1
     OR NOT EXISTS (SELECT 1 FROM pg_index WHERE indrelid=actual AND indisprimary AND indisunique AND indisvalid)
     OR NOT (has_table_privilege('medical_app_platform_runtime',actual,'SELECT') AND has_table_privilege('medical_app_platform_runtime',actual,'INSERT')
              AND has_table_privilege('medical_app_platform_runtime',actual,'UPDATE') AND has_table_privilege('medical_app_platform_runtime',actual,'DELETE'))
     OR EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
                WHERE c.oid=actual AND a.grantee NOT IN ((SELECT oid FROM pg_roles WHERE rolname=current_user),(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')))
     OR EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
                WHERE c.oid=actual AND a.grantee=(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')
                  AND (a.privilege_type NOT IN ('SELECT','INSERT','UPDATE','DELETE') OR a.is_grantable))
  THEN RAISE EXCEPTION 'Existing user-preference table differs from reviewed schema'; END IF;
END
$member_ai_preferences_v1$;

-- statement boundary
DROP TABLE pg_temp.member_ai_preferences_expected;
