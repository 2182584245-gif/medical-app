-- member_memory_0001; fingerprint a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014
-- Five additive owner-only tables. No business rows, functions, triggers or roles changed.
DO $member_memory_v1$
DECLARE present_count integer;
        expected_policy text := '((CURRENT_USER = ''medical_app_platform_runtime''::name) AND (current_setting(''medical_app.role_code''::text, true) = ''member''::text) AND (user_id = (NULLIF(current_setting(''medical_app.user_id''::text, true), ''''::text))::bigint))';
BEGIN
  IF current_user = 'medical_app_platform_runtime' THEN RAISE EXCEPTION 'Reviewed migration owner required'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='medical_app_platform_runtime')
     OR to_regclass('medical_app_platform.users') IS NULL THEN
    RAISE EXCEPTION 'Existing platform deployment required';
  END IF;
  SELECT count(*) INTO present_count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='medical_app_platform' AND c.relname IN ('member_memory_events','member_memory_daily','member_memory_preferences','member_memory_controls','member_memory_jobs') AND c.relkind='r';
  IF present_count NOT IN (0,5) THEN RAISE EXCEPTION 'Partial memory migration must be reviewed'; END IF;
  IF present_count=5 THEN
    IF (SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
               COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY a.attnum)
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='medical_app_platform.member_memory_events'::regclass AND a.attnum>0 AND NOT a.attisdropped) <> '[["user_id","bigint",true,""],["event_id","text",true,""],["domain","text",true,""],["event_type","text",true,""],["occurred_at","text",true,""],["received_at","text",true,""],["entity_type","text",false,""],["entity_id","text",false,""],["payload_json","text",true,"''{}''::text"],["schema_version","integer",true,"1"]]'::jsonb
      OR (SELECT jsonb_agg(a.attname ORDER BY k.ordinality)
        FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum,ordinality)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid='medical_app_platform.member_memory_events'::regclass AND c.contype='p') <> '["user_id","event_id"]'::jsonb
      OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid='medical_app_platform.member_memory_events'::regclass
          AND pg_get_userbyid(relowner)=current_user AND relrowsecurity AND relforcerowsecurity
          AND obj_description(oid,'pg_class')='member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014')
      OR (SELECT count(*) FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_events'::regclass) <> 1
      OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_events'::regclass
          AND polname='member_memory_owner' AND polcmd='*' AND polpermissive
          AND polroles=ARRAY[(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')]
          AND pg_get_expr(polqual,polrelid)=pg_get_expr(polwithcheck,polrelid)
          AND pg_get_expr(polqual,polrelid) = expected_policy)
      OR (SELECT jsonb_agg(conname ORDER BY conname) FROM pg_constraint
          WHERE conrelid='medical_app_platform.member_memory_events'::regclass) <> '["member_memory_events_domain_check","member_memory_events_entity_id_check","member_memory_events_entity_type_check","member_memory_events_event_id_check","member_memory_events_event_type_check","member_memory_events_occurred_at_check","member_memory_events_payload_json_check","member_memory_events_pkey","member_memory_events_received_at_check","member_memory_events_schema_version_check","member_memory_events_user_id_fkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_events'::regclass
          AND (NOT convalidated OR obj_description(oid,'pg_constraint') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_events'::regclass
          AND contype='f' AND confrelid='medical_app_platform.users'::regclass AND confdeltype='c')
      OR (SELECT jsonb_agg(indexname ORDER BY indexname) FROM pg_indexes
          WHERE schemaname='medical_app_platform' AND tablename='member_memory_events') <> '["member_memory_events_day_idx","member_memory_events_entity_idx","member_memory_events_pkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
          WHERE i.indrelid='medical_app_platform.member_memory_events'::regclass
          AND (NOT i.indisvalid OR obj_description(c.oid,'pg_class') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT (has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_events','SELECT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_events','INSERT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_events','UPDATE')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_events','DELETE'))
      OR EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
          WHERE c.oid='medical_app_platform.member_memory_events'::regclass AND a.grantee=0)

      OR NOT EXISTS (SELECT 1 FROM pg_constraint k
          WHERE k.conrelid='medical_app_platform.member_memory_events'::regclass
          AND k.conname='member_memory_events_payload_json_check' AND k.convalidated
          AND pg_get_constraintdef(k.oid,false)='CHECK (((octet_length(payload_json) <= 4096) AND (jsonb_typeof((payload_json)::jsonb) = ''object''::text) AND (((payload_json)::jsonb - ''duration_ms''::text) = ''{}''::jsonb) AND
CASE
    WHEN ((payload_json)::jsonb ? ''duration_ms''::text) THEN
    CASE
        WHEN (jsonb_typeof(((payload_json)::jsonb -> ''duration_ms''::text)) = ''number''::text) THEN (((((payload_json)::jsonb ->> ''duration_ms''::text))::numeric >= (0)::numeric) AND ((((payload_json)::jsonb ->> ''duration_ms''::text))::numeric <= (86400000)::numeric) AND ((((payload_json)::jsonb ->> ''duration_ms''::text))::numeric = trunc((((payload_json)::jsonb ->> ''duration_ms''::text))::numeric)))
        ELSE false
    END
    ELSE true
END))')
    THEN RAISE EXCEPTION 'Existing member_memory_events differs from reviewed schema'; END IF;
    IF (SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
               COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY a.attnum)
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='medical_app_platform.member_memory_daily'::regclass AND a.attnum>0 AND NOT a.attisdropped) <> '[["user_id","bigint",true,""],["domain","text",true,""],["day","text",true,""],["category","text",true,"''all''::text"],["metrics_json","text",true,"''{}''::text"],["evidence_json","text",true,"''[]''::text"],["source_digest","text",true,""],["coverage_json","text",true,"''{}''::text"],["updated_at","text",true,""]]'::jsonb
      OR (SELECT jsonb_agg(a.attname ORDER BY k.ordinality)
        FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum,ordinality)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid='medical_app_platform.member_memory_daily'::regclass AND c.contype='p') <> '["user_id","domain","day","category"]'::jsonb
      OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid='medical_app_platform.member_memory_daily'::regclass
          AND pg_get_userbyid(relowner)=current_user AND relrowsecurity AND relforcerowsecurity
          AND obj_description(oid,'pg_class')='member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014')
      OR (SELECT count(*) FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_daily'::regclass) <> 1
      OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_daily'::regclass
          AND polname='member_memory_owner' AND polcmd='*' AND polpermissive
          AND polroles=ARRAY[(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')]
          AND pg_get_expr(polqual,polrelid)=pg_get_expr(polwithcheck,polrelid)
          AND pg_get_expr(polqual,polrelid) = expected_policy)
      OR (SELECT jsonb_agg(conname ORDER BY conname) FROM pg_constraint
          WHERE conrelid='medical_app_platform.member_memory_daily'::regclass) <> '["member_memory_daily_category_check","member_memory_daily_coverage_json_check","member_memory_daily_day_check","member_memory_daily_domain_check","member_memory_daily_evidence_json_check","member_memory_daily_metrics_json_check","member_memory_daily_pkey","member_memory_daily_source_digest_check","member_memory_daily_updated_at_check","member_memory_daily_user_id_fkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_daily'::regclass
          AND (NOT convalidated OR obj_description(oid,'pg_constraint') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_daily'::regclass
          AND contype='f' AND confrelid='medical_app_platform.users'::regclass AND confdeltype='c')
      OR (SELECT jsonb_agg(indexname ORDER BY indexname) FROM pg_indexes
          WHERE schemaname='medical_app_platform' AND tablename='member_memory_daily') <> '["member_memory_daily_day_idx","member_memory_daily_pkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
          WHERE i.indrelid='medical_app_platform.member_memory_daily'::regclass
          AND (NOT i.indisvalid OR obj_description(c.oid,'pg_class') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT (has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_daily','SELECT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_daily','INSERT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_daily','UPDATE')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_daily','DELETE'))
      OR EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
          WHERE c.oid='medical_app_platform.member_memory_daily'::regclass AND a.grantee=0)

    THEN RAISE EXCEPTION 'Existing member_memory_daily differs from reviewed schema'; END IF;
    IF (SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
               COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY a.attnum)
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='medical_app_platform.member_memory_preferences'::regclass AND a.attnum>0 AND NOT a.attisdropped) <> '[["user_id","bigint",true,""],["preference_key","text",true,""],["statement","text",true,""],["status","text",true,"''inferred''::text"],["confidence","double precision",true,"0"],["evidence_json","text",true,"''[]''::text"],["support_start","text",false,""],["support_end","text",false,""],["expires_at","text",false,""],["model","text",false,""],["source_digest","text",true,""],["revision","bigint",true,"1"],["created_at","text",true,""],["updated_at","text",true,""]]'::jsonb
      OR (SELECT jsonb_agg(a.attname ORDER BY k.ordinality)
        FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum,ordinality)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid='medical_app_platform.member_memory_preferences'::regclass AND c.contype='p') <> '["user_id","preference_key"]'::jsonb
      OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid='medical_app_platform.member_memory_preferences'::regclass
          AND pg_get_userbyid(relowner)=current_user AND relrowsecurity AND relforcerowsecurity
          AND obj_description(oid,'pg_class')='member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014')
      OR (SELECT count(*) FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_preferences'::regclass) <> 1
      OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_preferences'::regclass
          AND polname='member_memory_owner' AND polcmd='*' AND polpermissive
          AND polroles=ARRAY[(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')]
          AND pg_get_expr(polqual,polrelid)=pg_get_expr(polwithcheck,polrelid)
          AND pg_get_expr(polqual,polrelid) = expected_policy)
      OR (SELECT jsonb_agg(conname ORDER BY conname) FROM pg_constraint
          WHERE conrelid='medical_app_platform.member_memory_preferences'::regclass) <> '["member_memory_preferences_confidence_check","member_memory_preferences_created_at_check","member_memory_preferences_evidence_json_check","member_memory_preferences_expires_at_check","member_memory_preferences_model_check","member_memory_preferences_pkey","member_memory_preferences_preference_key_check","member_memory_preferences_revision_check","member_memory_preferences_source_digest_check","member_memory_preferences_statement_check","member_memory_preferences_status_check","member_memory_preferences_support_end_check","member_memory_preferences_support_range","member_memory_preferences_support_start_check","member_memory_preferences_updated_at_check","member_memory_preferences_user_id_fkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_preferences'::regclass
          AND (NOT convalidated OR obj_description(oid,'pg_constraint') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_preferences'::regclass
          AND contype='f' AND confrelid='medical_app_platform.users'::regclass AND confdeltype='c')
      OR (SELECT jsonb_agg(indexname ORDER BY indexname) FROM pg_indexes
          WHERE schemaname='medical_app_platform' AND tablename='member_memory_preferences') <> '["member_memory_preferences_pkey","member_memory_preferences_status_idx"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
          WHERE i.indrelid='medical_app_platform.member_memory_preferences'::regclass
          AND (NOT i.indisvalid OR obj_description(c.oid,'pg_class') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT (has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_preferences','SELECT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_preferences','INSERT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_preferences','UPDATE')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_preferences','DELETE'))
      OR EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
          WHERE c.oid='medical_app_platform.member_memory_preferences'::regclass AND a.grantee=0)

    THEN RAISE EXCEPTION 'Existing member_memory_preferences differs from reviewed schema'; END IF;
    IF (SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
               COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY a.attnum)
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='medical_app_platform.member_memory_controls'::regclass AND a.attnum>0 AND NOT a.attisdropped) <> '[["user_id","bigint",true,""],["domain","text",true,""],["enabled_for_ai","boolean",true,"true"],["collect_events","boolean",true,"true"],["forget_before","text",false,""],["revision","bigint",true,"1"],["updated_at","text",true,""]]'::jsonb
      OR (SELECT jsonb_agg(a.attname ORDER BY k.ordinality)
        FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum,ordinality)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid='medical_app_platform.member_memory_controls'::regclass AND c.contype='p') <> '["user_id","domain"]'::jsonb
      OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid='medical_app_platform.member_memory_controls'::regclass
          AND pg_get_userbyid(relowner)=current_user AND relrowsecurity AND relforcerowsecurity
          AND obj_description(oid,'pg_class')='member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014')
      OR (SELECT count(*) FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_controls'::regclass) <> 1
      OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_controls'::regclass
          AND polname='member_memory_owner' AND polcmd='*' AND polpermissive
          AND polroles=ARRAY[(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')]
          AND pg_get_expr(polqual,polrelid)=pg_get_expr(polwithcheck,polrelid)
          AND pg_get_expr(polqual,polrelid) = expected_policy)
      OR (SELECT jsonb_agg(conname ORDER BY conname) FROM pg_constraint
          WHERE conrelid='medical_app_platform.member_memory_controls'::regclass) <> '["member_memory_controls_domain_check","member_memory_controls_forget_before_check","member_memory_controls_pkey","member_memory_controls_revision_check","member_memory_controls_updated_at_check","member_memory_controls_user_id_fkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_controls'::regclass
          AND (NOT convalidated OR obj_description(oid,'pg_constraint') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_controls'::regclass
          AND contype='f' AND confrelid='medical_app_platform.users'::regclass AND confdeltype='c')
      OR (SELECT jsonb_agg(indexname ORDER BY indexname) FROM pg_indexes
          WHERE schemaname='medical_app_platform' AND tablename='member_memory_controls') <> '["member_memory_controls_pkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
          WHERE i.indrelid='medical_app_platform.member_memory_controls'::regclass
          AND (NOT i.indisvalid OR obj_description(c.oid,'pg_class') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT (has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_controls','SELECT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_controls','INSERT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_controls','UPDATE')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_controls','DELETE'))
      OR EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
          WHERE c.oid='medical_app_platform.member_memory_controls'::regclass AND a.grantee=0)

    THEN RAISE EXCEPTION 'Existing member_memory_controls differs from reviewed schema'; END IF;
    IF (SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
               COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY a.attnum)
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='medical_app_platform.member_memory_jobs'::regclass AND a.attnum>0 AND NOT a.attisdropped) <> '[["user_id","bigint",true,""],["job_key","text",true,""],["day","text",true,""],["source_digest","text",true,""],["status","text",true,"''pending''::text"],["started_at","text",true,""],["finished_at","text",false,""],["last_error_code","text",false,""],["revision","bigint",true,"1"]]'::jsonb
      OR (SELECT jsonb_agg(a.attname ORDER BY k.ordinality)
        FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum,ordinality)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid='medical_app_platform.member_memory_jobs'::regclass AND c.contype='p') <> '["user_id","job_key"]'::jsonb
      OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid='medical_app_platform.member_memory_jobs'::regclass
          AND pg_get_userbyid(relowner)=current_user AND relrowsecurity AND relforcerowsecurity
          AND obj_description(oid,'pg_class')='member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014')
      OR (SELECT count(*) FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_jobs'::regclass) <> 1
      OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid='medical_app_platform.member_memory_jobs'::regclass
          AND polname='member_memory_owner' AND polcmd='*' AND polpermissive
          AND polroles=ARRAY[(SELECT oid FROM pg_roles WHERE rolname='medical_app_platform_runtime')]
          AND pg_get_expr(polqual,polrelid)=pg_get_expr(polwithcheck,polrelid)
          AND pg_get_expr(polqual,polrelid) = expected_policy)
      OR (SELECT jsonb_agg(conname ORDER BY conname) FROM pg_constraint
          WHERE conrelid='medical_app_platform.member_memory_jobs'::regclass) <> '["member_memory_jobs_day_check","member_memory_jobs_finished_at_check","member_memory_jobs_job_key_check","member_memory_jobs_last_error_code_check","member_memory_jobs_pkey","member_memory_jobs_revision_check","member_memory_jobs_source_digest_check","member_memory_jobs_started_at_check","member_memory_jobs_status_check","member_memory_jobs_user_id_fkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_jobs'::regclass
          AND (NOT convalidated OR obj_description(oid,'pg_constraint') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='medical_app_platform.member_memory_jobs'::regclass
          AND contype='f' AND confrelid='medical_app_platform.users'::regclass AND confdeltype='c')
      OR (SELECT jsonb_agg(indexname ORDER BY indexname) FROM pg_indexes
          WHERE schemaname='medical_app_platform' AND tablename='member_memory_jobs') <> '["member_memory_jobs_pkey"]'::jsonb
      OR EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
          WHERE i.indrelid='medical_app_platform.member_memory_jobs'::regclass
          AND (NOT i.indisvalid OR obj_description(c.oid,'pg_class') IS DISTINCT FROM 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014'))
      OR NOT (has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_jobs','SELECT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_jobs','INSERT')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_jobs','UPDATE')
          AND has_table_privilege('medical_app_platform_runtime','medical_app_platform.member_memory_jobs','DELETE'))
      OR EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
          WHERE c.oid='medical_app_platform.member_memory_jobs'::regclass AND a.grantee=0)

    THEN RAISE EXCEPTION 'Existing member_memory_jobs differs from reviewed schema'; END IF;
    RETURN;
  END IF;
CREATE TABLE medical_app_platform.member_memory_events (
    user_id bigint NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
    event_id text NOT NULL CHECK (event_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    domain text NOT NULL CHECK (domain IN ('profile','life_records','reminders','goals_habits','services_appointments','reading','shopping','forum','operations','preferences','ai_records')),
    event_type text NOT NULL CHECK (event_type IN ('page_view','button_click','content_view','post_view')),
    occurred_at text NOT NULL CHECK (occurred_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND occurred_at::timestamptz IS NOT NULL),
    received_at text NOT NULL CHECK (received_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND received_at::timestamptz IS NOT NULL),
    entity_type text CHECK (entity_type IS NULL OR entity_type ~ '^[a-z][a-z0-9_.-]{0,79}$'),
    entity_id text CHECK (entity_id IS NULL OR char_length(entity_id) BETWEEN 1 AND 160),
    payload_json text NOT NULL DEFAULT '{}' CHECK (octet_length(payload_json) <= 4096 AND jsonb_typeof(payload_json::jsonb) = 'object'
      AND (payload_json::jsonb - 'duration_ms') = '{}'::jsonb
      AND CASE WHEN payload_json::jsonb ? 'duration_ms' THEN
        CASE WHEN jsonb_typeof(payload_json::jsonb -> 'duration_ms') = 'number' THEN
          (payload_json::jsonb ->> 'duration_ms')::numeric >= 0
          AND (payload_json::jsonb ->> 'duration_ms')::numeric <= 86400000
          AND (payload_json::jsonb ->> 'duration_ms')::numeric = trunc((payload_json::jsonb ->> 'duration_ms')::numeric)
        ELSE false END ELSE true END),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    PRIMARY KEY (user_id,event_id)
);
ALTER TABLE medical_app_platform.member_memory_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_app_platform.member_memory_events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE medical_app_platform.member_memory_events FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE medical_app_platform.member_memory_events TO medical_app_platform_runtime;
CREATE POLICY member_memory_owner ON medical_app_platform.member_memory_events FOR ALL TO medical_app_platform_runtime
  USING (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint) WITH CHECK (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint);
COMMENT ON TABLE medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_event_id_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_domain_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_event_type_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_occurred_at_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_received_at_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_entity_type_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_entity_id_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_payload_json_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_schema_version_check ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_pkey ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_events_user_id_fkey ON medical_app_platform.member_memory_events IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
CREATE INDEX member_memory_events_day_idx ON medical_app_platform.member_memory_events (user_id,domain,occurred_at);
CREATE INDEX member_memory_events_entity_idx ON medical_app_platform.member_memory_events (user_id,entity_type,entity_id,occurred_at);
COMMENT ON INDEX medical_app_platform.member_memory_events_pkey IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON INDEX medical_app_platform.member_memory_events_day_idx IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON INDEX medical_app_platform.member_memory_events_entity_idx IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
CREATE TABLE medical_app_platform.member_memory_daily (
    user_id bigint NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
    domain text NOT NULL CHECK (domain IN ('profile','life_records','reminders','goals_habits','services_appointments','reading','shopping','forum','operations','preferences','ai_records')),
    day text NOT NULL CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND day::date::text = day),
    category text NOT NULL DEFAULT 'all' CONSTRAINT member_memory_daily_category_check CHECK (
      (domain = 'life_records' AND category IN ('all','diet','water','activity','sleep','environment','medical','metric'))
      OR (domain <> 'life_records' AND category = 'all')),
    metrics_json text NOT NULL DEFAULT '{}' CHECK (octet_length(metrics_json) <= 65536 AND jsonb_typeof(metrics_json::jsonb) = 'object'),
    evidence_json text NOT NULL DEFAULT '[]' CHECK (octet_length(evidence_json) <= 65536 AND jsonb_typeof(evidence_json::jsonb) = 'array'),
    source_digest text NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    coverage_json text NOT NULL DEFAULT '{}' CHECK (octet_length(coverage_json) <= 16384 AND jsonb_typeof(coverage_json::jsonb) = 'object'),
    updated_at text NOT NULL CHECK (updated_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND updated_at::timestamptz IS NOT NULL),
    PRIMARY KEY (user_id,domain,day,category)
);
ALTER TABLE medical_app_platform.member_memory_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_app_platform.member_memory_daily FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE medical_app_platform.member_memory_daily FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE medical_app_platform.member_memory_daily TO medical_app_platform_runtime;
CREATE POLICY member_memory_owner ON medical_app_platform.member_memory_daily FOR ALL TO medical_app_platform_runtime
  USING (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint) WITH CHECK (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint);
COMMENT ON TABLE medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_domain_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_day_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_category_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_metrics_json_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_evidence_json_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_source_digest_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_coverage_json_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_updated_at_check ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_pkey ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_daily_user_id_fkey ON medical_app_platform.member_memory_daily IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
CREATE INDEX member_memory_daily_day_idx ON medical_app_platform.member_memory_daily (user_id,day,domain);
COMMENT ON INDEX medical_app_platform.member_memory_daily_pkey IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON INDEX medical_app_platform.member_memory_daily_day_idx IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
CREATE TABLE medical_app_platform.member_memory_preferences (
    user_id bigint NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
    preference_key text NOT NULL CHECK (preference_key ~ '^[a-z][a-z0-9_.-]{0,119}$'),
    statement text NOT NULL CHECK (char_length(statement) BETWEEN 1 AND 2000),
    status text NOT NULL DEFAULT 'inferred' CHECK (status IN ('inferred','confirmed','rejected','stale','revoked')),
    confidence double precision NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1 AND confidence <> 'NaN'::double precision),
    evidence_json text NOT NULL DEFAULT '[]' CHECK (octet_length(evidence_json) <= 32768 AND jsonb_typeof(evidence_json::jsonb) = 'array'),
    support_start text CHECK (support_start IS NULL OR (support_start ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND support_start::timestamptz IS NOT NULL)),
    support_end text CHECK (support_end IS NULL OR (support_end ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND support_end::timestamptz IS NOT NULL)),
    expires_at text CHECK (expires_at IS NULL OR (expires_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND expires_at::timestamptz IS NOT NULL)),
    model text CHECK (model IS NULL OR char_length(model) BETWEEN 1 AND 120),
    source_digest text NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at text NOT NULL CHECK (created_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND created_at::timestamptz IS NOT NULL),
    updated_at text NOT NULL CHECK (updated_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND updated_at::timestamptz IS NOT NULL),
    PRIMARY KEY (user_id,preference_key),
    CONSTRAINT member_memory_preferences_support_range CHECK (support_start IS NULL OR support_end IS NULL OR support_start::timestamptz <= support_end::timestamptz)
);
ALTER TABLE medical_app_platform.member_memory_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_app_platform.member_memory_preferences FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE medical_app_platform.member_memory_preferences FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE medical_app_platform.member_memory_preferences TO medical_app_platform_runtime;
CREATE POLICY member_memory_owner ON medical_app_platform.member_memory_preferences FOR ALL TO medical_app_platform_runtime
  USING (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint) WITH CHECK (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint);
COMMENT ON TABLE medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_preference_key_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_statement_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_status_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_confidence_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_evidence_json_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_support_start_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_support_end_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_expires_at_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_model_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_source_digest_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_revision_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_created_at_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_updated_at_check ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_pkey ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_user_id_fkey ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_preferences_support_range ON medical_app_platform.member_memory_preferences IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
CREATE INDEX member_memory_preferences_status_idx ON medical_app_platform.member_memory_preferences (user_id,status,updated_at);
COMMENT ON INDEX medical_app_platform.member_memory_preferences_pkey IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON INDEX medical_app_platform.member_memory_preferences_status_idx IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
CREATE TABLE medical_app_platform.member_memory_controls (
    user_id bigint NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
    domain text NOT NULL CHECK (domain IN ('profile','life_records','reminders','goals_habits','services_appointments','reading','shopping','forum','operations','preferences','ai_records')),
    enabled_for_ai boolean NOT NULL DEFAULT true,
    collect_events boolean NOT NULL DEFAULT true,
    forget_before text CHECK (forget_before IS NULL OR (forget_before ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND forget_before::timestamptz IS NOT NULL)),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at text NOT NULL CHECK (updated_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND updated_at::timestamptz IS NOT NULL),
    PRIMARY KEY (user_id,domain)
);
ALTER TABLE medical_app_platform.member_memory_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_app_platform.member_memory_controls FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE medical_app_platform.member_memory_controls FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE medical_app_platform.member_memory_controls TO medical_app_platform_runtime;
CREATE POLICY member_memory_owner ON medical_app_platform.member_memory_controls FOR ALL TO medical_app_platform_runtime
  USING (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint) WITH CHECK (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint);
COMMENT ON TABLE medical_app_platform.member_memory_controls IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_controls_domain_check ON medical_app_platform.member_memory_controls IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_controls_forget_before_check ON medical_app_platform.member_memory_controls IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_controls_revision_check ON medical_app_platform.member_memory_controls IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_controls_updated_at_check ON medical_app_platform.member_memory_controls IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_controls_pkey ON medical_app_platform.member_memory_controls IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_controls_user_id_fkey ON medical_app_platform.member_memory_controls IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON INDEX medical_app_platform.member_memory_controls_pkey IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
CREATE TABLE medical_app_platform.member_memory_jobs (
    user_id bigint NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
    job_key text NOT NULL CHECK (job_key = 'preference_summary'),
    day text NOT NULL CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND day::date::text = day),
    source_digest text NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ready','failed','stale')),
    started_at text NOT NULL CHECK (started_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND started_at::timestamptz IS NOT NULL),
    finished_at text CHECK (finished_at IS NULL OR (finished_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' AND finished_at::timestamptz IS NOT NULL)),
    last_error_code text CHECK (last_error_code IS NULL OR last_error_code ~ '^[A-Za-z0-9_.-]{1,64}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    PRIMARY KEY (user_id,job_key)
);
ALTER TABLE medical_app_platform.member_memory_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE medical_app_platform.member_memory_jobs FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE medical_app_platform.member_memory_jobs FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE medical_app_platform.member_memory_jobs TO medical_app_platform_runtime;
CREATE POLICY member_memory_owner ON medical_app_platform.member_memory_jobs FOR ALL TO medical_app_platform_runtime
  USING (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint) WITH CHECK (current_user = 'medical_app_platform_runtime' AND current_setting('medical_app.role_code',true) = 'member' AND user_id = NULLIF(current_setting('medical_app.user_id',true),'')::bigint);
COMMENT ON TABLE medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_job_key_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_day_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_source_digest_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_status_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_started_at_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_finished_at_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_last_error_code_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_revision_check ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_pkey ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON CONSTRAINT member_memory_jobs_user_id_fkey ON medical_app_platform.member_memory_jobs IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
COMMENT ON INDEX medical_app_platform.member_memory_jobs_pkey IS 'member_memory_0001:a8643d7f96e7529bdb43662931e138ce6bebeb585c54d52d10974439896e4014';
END
$member_memory_v1$;
