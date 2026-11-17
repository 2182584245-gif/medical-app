"""Frozen v4 metadata checks shared by owned maintenance and business transfer.

Only catalog definitions are inspected. No passwords, session values, encrypted
keys, settings text or personal rows are read by this module.
"""

import hashlib
import json
import re

from .platform_developer_schema import NEW_TABLES, TABLE_DDL, policy_ddl
from .platform_schema import PLATFORM_MARKER, PLATFORM_SCHEMA

NEW_COLUMNS = {
    "developer_grants": ("user_id", "enabled", "granted_at"),
    "developer_sessions": ("token_digest", "user_id", "expires_at"),
    "developer_settings_snapshots": (
        "revision",
        "settings_json",
        "encrypted_key",
        "created_at",
        "created_by",
    ),
    "developer_pending_changes": (
        "id",
        "target_user_id",
        "table_name",
        "row_id",
        "changes_json",
        "base_fingerprint",
        "state",
        "created_by",
        "created_at",
    ),
    "developer_client_sessions": ("token_digest", "user_id", "revision", "captured_at"),
}
NEW_RELATIONS = frozenset(
    {
        *NEW_TABLES,
        *(name + "_pkey" for name in NEW_TABLES),
        "developer_pending_changes_id_seq",
        *(
            re.search(r"CREATE (?:UNIQUE )?INDEX (\w+)", statement).group(1)
            for statement in TABLE_DDL
            if re.match(r"CREATE (?:UNIQUE )?INDEX ", statement)
        ),
    }
)
POLICY_KEYS = frozenset(
    (match.group(2), match.group(1))
    for statement in policy_ddl()
    if (match := re.match(r"CREATE POLICY (\w+) ON medical_app_platform\.(\w+)", statement))
)

# Computed from the frozen v4 DDL on isolated PostgreSQL 17, including canonical
# expressions, all new columns/types/defaults, constraints, indexes and grants.
# Changing this constant requires a reviewed migration and fresh PG evidence.
REVIEWED_V4_SHA256 = "0e4c4f2582ce12bb39868521a0a63215c863622d08b3d45b5c6476b23733644f"


def inventory(connection):
    names = list(NEW_TABLES)
    relations = connection.execute(
        "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,"
        "pg_get_userbyid(c.relowner)=current_user,obj_description(c.oid,'pg_class') "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname",
        (PLATFORM_SCHEMA, list(NEW_RELATIONS)),
    ).fetchall()
    columns = connection.execute(
        "SELECT c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod),"
        "a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid,false) "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE n.nspname=%s AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped "
        "ORDER BY c.relname,a.attnum",
        (PLATFORM_SCHEMA, names),
    ).fetchall()
    constraints = connection.execute(
        "SELECT c.relname,k.conname,k.convalidated,pg_get_constraintdef(k.oid,false) "
        "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname,k.conname",
        (PLATFORM_SCHEMA, names),
    ).fetchall()
    indexes = connection.execute(
        "SELECT tablename,indexname,indexdef FROM pg_indexes "
        "WHERE schemaname=%s AND tablename=ANY(%s) ORDER BY tablename,indexname",
        (PLATFORM_SCHEMA, names),
    ).fetchall()
    policies = connection.execute(
        "SELECT c.relname,p.polname,p.polcmd,p.polpermissive,"
        "ARRAY(SELECT CASE WHEN r=0 THEN 'PUBLIC' ELSE pg_get_userbyid(r) END "
        "FROM unnest(p.polroles) r ORDER BY 1),"
        "pg_get_expr(p.polqual,p.polrelid,false),pg_get_expr(p.polwithcheck,p.polrelid,false) "
        "FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s "
        "AND (c.relname=ANY(%s) OR p.polname LIKE 'developer_%%') ORDER BY c.relname,p.polname",
        (PLATFORM_SCHEMA, names),
    ).fetchall()
    grants = connection.execute(
        "SELECT c.relname,CASE WHEN a.grantor=c.relowner THEN '<owner>' "
        "ELSE pg_get_userbyid(a.grantor) END,"
        "CASE WHEN a.grantee=c.relowner THEN '<owner>' WHEN a.grantee=0 THEN 'PUBLIC' "
        "ELSE pg_get_userbyid(a.grantee) END,a.privilege_type,a.is_grantable "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,"
        "acldefault(CASE WHEN c.relkind='S' THEN 's'::\"char\" "
        "ELSE 'r'::\"char\" END,c.relowner))) a "
        "WHERE n.nspname=%s AND c.relname=ANY(%s) AND c.relkind IN ('r','S') "
        "ORDER BY 1,2,3,4,5",
        (PLATFORM_SCHEMA, list(NEW_RELATIONS)),
    ).fetchall()
    return {
        "relations": relations,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "policies": policies,
        "grants": grants,
    }


def inventory_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def require_reviewed_v4(connection):
    value = inventory(connection)
    if inventory_digest(value) != REVIEWED_V4_SHA256:
        raise ValueError("Developer v4 catalog differs from the reviewed PostgreSQL 17 metadata.")
    if {row[0] for row in value["relations"] if row[1] == "r"} != set(NEW_TABLES):
        raise ValueError("Developer v4 table set differs.")
    if any(
        row[2:] != (True, True, True, PLATFORM_MARKER)
        for row in value["relations"]
        if row[1] == "r"
    ):
        raise ValueError("Developer v4 table ownership or forced RLS differs.")
    return inventory_digest(value)
