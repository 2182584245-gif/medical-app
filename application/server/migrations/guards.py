"""Server-side guards are emitted into offline SQL and also used online.

Only literal project-owned identifiers are used here, never user input. These
guards have offline/structural tests; PostgreSQL execution needs a test project.
"""

from server.schema import PRIVATE_SCHEMA

SCHEMA_MARKER = "medical-app:private-pilot-schema:v1"
REVISION_MARKER = "medical-app:pilot_0001:managed"
VERSION_TABLE = "alembic_version"


def _names(values) -> str:
    return "ARRAY[" + ", ".join(f"'{value}'" for value in values) + "]::text[]"


def ownership_sql() -> str:
    return f"""
DO $medical_owner$
BEGIN
    IF pg_catalog.obj_description(
        pg_catalog.to_regnamespace('{PRIVATE_SCHEMA}'), 'pg_namespace'
    ) IS DISTINCT FROM '{SCHEMA_MARKER}' THEN
        RAISE EXCEPTION 'Refusing unowned or absent medical_app_private schema';
    END IF;
    IF pg_catalog.obj_description(
        pg_catalog.to_regclass('{PRIVATE_SCHEMA}.{VERSION_TABLE}'), 'pg_class'
    ) IS DISTINCT FROM '{REVISION_MARKER}' THEN
        RAISE EXCEPTION 'Refusing unowned or absent pilot migration version table';
    END IF;
END;
$medical_owner$;
"""


def bootstrap_sql() -> str:
    return f"""
DO $medical_bootstrap$
BEGIN
    IF pg_catalog.to_regnamespace('{PRIVATE_SCHEMA}') IS NULL THEN
        CREATE SCHEMA {PRIVATE_SCHEMA};
        COMMENT ON SCHEMA {PRIVATE_SCHEMA} IS '{SCHEMA_MARKER}';
    ELSE
        IF pg_catalog.obj_description(
            pg_catalog.to_regnamespace('{PRIVATE_SCHEMA}'), 'pg_namespace'
        ) IS DISTINCT FROM '{SCHEMA_MARKER}' OR pg_catalog.obj_description(
            pg_catalog.to_regclass('{PRIVATE_SCHEMA}.{VERSION_TABLE}'), 'pg_class'
        ) IS DISTINCT FROM '{REVISION_MARKER}' THEN
            RAISE EXCEPTION 'Refusing to adopt an existing unowned schema or version table';
        END IF;
    END IF;
END;
$medical_bootstrap$;
"""


def revoke_sql(*, include_tables: bool) -> str:
    table_revoke = (
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {PRIVATE_SCHEMA} FROM PUBLIC;"
        if include_tables
        else ""
    )
    role_table_revoke = (
        f"EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA "
        f"{PRIVATE_SCHEMA} FROM %I', role_name);"
        if include_tables
        else ""
    )
    return f"""
REVOKE ALL ON SCHEMA {PRIVATE_SCHEMA} FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA {PRIVATE_SCHEMA} REVOKE ALL ON TABLES FROM PUBLIC;
{table_revoke}
DO $medical_privileges$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = role_name) THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON SCHEMA {PRIVATE_SCHEMA} FROM %I', role_name);
            EXECUTE pg_catalog.format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA {PRIVATE_SCHEMA} '
                'REVOKE ALL ON TABLES FROM %I', role_name);
            {role_table_revoke}
        END IF;
    END LOOP;
END;
$medical_privileges$;
"""


def objects_guard_sql(
    *, tables: dict[str, tuple[str, ...]], indexes: tuple[str, ...], constraints: tuple[str, ...]
) -> str:
    """Fail closed on extra objects, including objects DROP TABLE removes implicitly."""
    relation_names = _names((*tables, *indexes))
    table_names = _names(tables)
    constraint_names = _names(constraints)
    column_checks = "\n".join(
        f"""
    IF (SELECT pg_catalog.array_agg(a.attname::text ORDER BY a.attname)
        FROM pg_catalog.pg_attribute a
        WHERE a.attrelid = '{PRIVATE_SCHEMA}.{table}'::regclass
          AND a.attnum > 0 AND NOT a.attisdropped)
        IS DISTINCT FROM {_names(sorted(columns))} THEN
        RAISE EXCEPTION 'Refusing changed or extra columns in {table}';
    END IF;"""
        for table, columns in tables.items()
    )
    return f"""
DO $medical_objects$
DECLARE schema_oid oid := pg_catalog.to_regnamespace('{PRIVATE_SCHEMA}');
BEGIN
    IF (SELECT pg_catalog.array_agg(c.relname::text ORDER BY c.relname)
        FROM pg_catalog.pg_class c WHERE c.relnamespace = schema_oid)
        IS DISTINCT FROM (SELECT pg_catalog.array_agg(n ORDER BY n)
                          FROM pg_catalog.unnest({relation_names}) AS n) THEN
        RAISE EXCEPTION 'Refusing missing or extra relations in private pilot schema';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace = schema_oid
        AND ((c.relname = ANY({table_names}) AND c.relkind <> 'r')
             OR (c.relname <> ALL({table_names}) AND c.relkind <> 'i'))) THEN
        RAISE EXCEPTION 'Refusing changed pilot relation kinds';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace = schema_oid
               AND (c.relrowsecurity OR c.relforcerowsecurity)) THEN
        RAISE EXCEPTION 'Refusing unexpected row-level security on pilot tables';
    END IF;
    IF (SELECT pg_catalog.array_agg(c.conname::text ORDER BY c.conname)
        FROM pg_catalog.pg_constraint c WHERE c.connamespace = schema_oid AND c.contype <> 'n')
        IS DISTINCT FROM (SELECT pg_catalog.array_agg(n ORDER BY n)
                          FROM pg_catalog.unnest({constraint_names}) AS n) THEN
        RAISE EXCEPTION 'Refusing missing or extra pilot constraints';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_proc WHERE pronamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_type t WHERE t.typnamespace = schema_oid
           AND t.typrelid NOT IN (SELECT oid FROM pg_catalog.pg_class
                                 WHERE relnamespace = schema_oid AND relkind = 'r')
           AND NOT (t.typelem IN (SELECT oid FROM pg_catalog.pg_type
                WHERE typnamespace = schema_oid AND typrelid <> 0) AND t.typcategory = 'A'))
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_collation WHERE collnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_conversion WHERE connamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_operator WHERE oprnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_opclass WHERE opcnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_opfamily WHERE opfnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_config WHERE cfgnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_dict WHERE dictnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_parser WHERE prsnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_ts_template WHERE tmplnamespace = schema_oid)
    THEN
        RAISE EXCEPTION 'Refusing extra schema objects';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c
               ON c.oid = t.tgrelid WHERE c.relnamespace = schema_oid AND NOT t.tgisinternal)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c
                  ON c.oid = p.polrelid WHERE c.relnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite r JOIN pg_catalog.pg_class c
                  ON c.oid = r.ev_class WHERE c.relnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_attrdef a JOIN pg_catalog.pg_class c
                  ON c.oid = a.adrelid WHERE c.relnamespace = schema_oid)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i JOIN pg_catalog.pg_class c
                  ON c.oid IN (i.inhrelid, i.inhparent) WHERE c.relnamespace = schema_oid)
    THEN
        RAISE EXCEPTION 'Refusing added triggers, policies, rules, defaults or inheritance';
    END IF;
    {column_checks}
END;
$medical_objects$;
"""
