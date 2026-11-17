-- member_ai_preference_categories_0001; additive evidence-backed editable category documents.
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
CREATE TEMP TABLE member_ai_preference_categories_expected (
    user_id bigint NOT NULL,
    domain text NOT NULL CHECK (domain IN ('profile','life_records','reminders','goals_habits','services_appointments','reading','shopping','forum','operations','preferences','ai_records')),
    text text NOT NULL CHECK (char_length(text) <= CASE WHEN domain='preferences' THEN 6400 ELSE 2000 END),
    source text NOT NULL CHECK (source IN ('empty','ai_summary','user_confirmed')),
    status text NOT NULL CHECK (status IN ('empty','ready','pending','failed')),
    revision bigint NOT NULL CHECK (revision > 0),
    source_digest text NOT NULL CHECK (source_digest='' OR source_digest ~ '^[0-9a-f]{64}$'),
    evidence_json text NOT NULL CHECK (octet_length(evidence_json)<=32768 AND jsonb_typeof(evidence_json::jsonb)='array' AND jsonb_array_length(evidence_json::jsonb)<=96),
    range_start text,
    range_end text,
    model text CHECK (model IS NULL OR char_length(model)<=120),
    generated_at text,
    legacy_revision bigint NOT NULL CHECK (legacy_revision>=0),
    legacy_digest text NOT NULL CHECK (legacy_digest='' OR legacy_digest ~ '^[0-9a-f]{64}$'),
    job_request_id text CHECK (job_request_id IS NULL OR job_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    job_request_hash text CHECK (job_request_hash IS NULL OR job_request_hash ~ '^[0-9a-f]{64}$'),
    job_source_digest text CHECK (job_source_digest IS NULL OR job_source_digest ~ '^[0-9a-f]{64}$'),
    job_started_at text,
    last_error_code text CHECK (last_error_code IS NULL OR last_error_code IN ('summary_unavailable','source_changed')),
    last_request_id text CHECK (last_request_id IS NULL OR last_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    last_request_hash text CHECK (last_request_hash IS NULL OR last_request_hash ~ '^[0-9a-f]{64}$'),
    created_at text NOT NULL,
    updated_at text NOT NULL,
    CHECK (created_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' AND updated_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' AND created_at::timestamptz <= updated_at::timestamptz),
    CHECK (range_start IS NULL OR range_start::timestamptz IS NOT NULL),
    CHECK (range_end IS NULL OR range_end::timestamptz IS NOT NULL),
    CHECK (generated_at IS NULL OR generated_at::timestamptz IS NOT NULL),
    CHECK (job_started_at IS NULL OR job_started_at::timestamptz IS NOT NULL),
    CHECK (range_start IS NULL AND range_end IS NULL OR range_start::timestamptz<=range_end::timestamptz),
    PRIMARY KEY (user_id,domain)
) ON COMMIT DROP;

-- statement boundary

DO $member_ai_preference_categories_v1$
DECLARE
  actual oid;
  expected oid := 'pg_temp.member_ai_preference_categories_expected'::regclass;
  policy_expression text := '((CURRENT_USER = ''medical_app_platform_runtime''::name) AND (current_setting(''medical_app.role_code''::text, true) = ''member''::text) AND (user_id = (NULLIF(current_setting(''medical_app.user_id''::text, true), ''''::text))::bigint))';
BEGIN
  actual := to_regclass('medical_app_platform.member_ai_preference_categories');
  IF actual IS NULL THEN
    CREATE TABLE medical_app_platform.member_ai_preference_categories (
    user_id bigint NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
    domain text NOT NULL CHECK (domain IN ('profile','life_records','reminders','goals_habits','services_appointments','reading','shopping','forum','operations','preferences','ai_records')),
    text text NOT NULL CHECK (char_length(text) <= CASE WHEN domain='preferences' THEN 6400 ELSE 2000 END),
    source text NOT NULL CHECK (source IN ('empty','ai_summary','user_confirmed')),
    status text NOT NULL CHECK (status IN ('empty','ready','pending','failed')),
    revision bigint NOT NULL CHECK (revision > 0),
    source_digest text NOT NULL CHECK (source_digest='' OR source_digest ~ '^[0-9a-f]{64}$'),
    evidence_json text NOT NULL CHECK (octet_length(evidence_json)<=32768 AND jsonb_typeof(evidence_json::jsonb)='array' AND jsonb_array_length(evidence_json::jsonb)<=96),
    range_start text,
    range_end text,
    model text CHECK (model IS NULL OR char_length(model)<=120),
    generated_at text,
    legacy_revision bigint NOT NULL CHECK (legacy_revision>=0),
    legacy_digest text NOT NULL CHECK (legacy_digest='' OR legacy_digest ~ '^[0-9a-f]{64}$'),
    job_request_id text CHECK (job_request_id IS NULL OR job_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    job_request_hash text CHECK (job_request_hash IS NULL OR job_request_hash ~ '^[0-9a-f]{64}$'),
    job_source_digest text CHECK (job_source_digest IS NULL OR job_source_digest ~ '^[0-9a-f]{64}$'),
    job_started_at text,
    last_error_code text CHECK (last_error_code IS NULL OR last_error_code IN ('summary_unavailable','source_changed')),
    last_request_id text CHECK (last_request_id IS NULL OR last_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    last_request_hash text CHECK (last_request_hash IS NULL OR last_request_hash ~ '^[0-9a-f]{64}$'),
    created_at text NOT NULL,
    updated_at text NOT NULL,
    CHECK (created_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' AND updated_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' AND created_at::timestamptz <= updated_at::timestamptz),
    CHECK (range_start IS NULL OR range_start::timestamptz IS NOT NULL),
    CHECK (range_end IS NULL OR range_end::timestamptz IS NOT NULL),
    CHECK (generated_at IS NULL OR generated_at::timestamptz IS NOT NULL),
    CHECK (job_started_at IS NULL OR job_started_at::timestamptz IS NOT NULL),
    CHECK (range_start IS NULL AND range_end IS NULL OR range_start::timestamptz<=range_end::timestamptz),
    PRIMARY KEY (user_id,domain)
    );
    ALTER TABLE medical_app_platform.member_ai_preference_categories ENABLE ROW LEVEL SECURITY;
    ALTER TABLE medical_app_platform.member_ai_preference_categories FORCE ROW LEVEL SECURITY;
    REVOKE ALL ON TABLE medical_app_platform.member_ai_preference_categories FROM PUBLIC;
    GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE medical_app_platform.member_ai_preference_categories TO medical_app_platform_runtime;
    CREATE POLICY member_ai_preference_categories_owner ON medical_app_platform.member_ai_preference_categories FOR ALL TO medical_app_platform_runtime
      USING (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint)
      WITH CHECK (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint);
    COMMENT ON TABLE medical_app_platform.member_ai_preference_categories IS 'member_ai_preference_categories_0001:category-ai-editable-v1';
    actual := 'medical_app_platform.member_ai_preference_categories'::regclass;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid=actual AND relkind='r'
                 AND pg_get_userbyid(relowner)=current_user AND relrowsecurity AND relforcerowsecurity
                 AND obj_description(oid,'pg_class')='member_ai_preference_categories_0001:category-ai-editable-v1')
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
     OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid=actual AND polname='member_ai_preference_categories_owner'
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
$member_ai_preference_categories_v1$;

-- statement boundary
DROP TABLE pg_temp.member_ai_preference_categories_expected;
