"""Catalog-only verification helpers; no member-facing migration API."""
import hashlib,json
from .contracts import SCHEMA,NEW_TABLES
REQUIRED_PG_CHECKS=frozenset({'baseline_41_plus_14_ddl','goal_linked_legacy_reminder_atomic','report_prefill_legacy_draft_atomic','runtime_member_own_crud','legacy_diet_atomic_meal','memory_revocation','cross_member_read_denied','referral_bonus_cross_member_rls_insert','forged_owner_insert_denied','ledger_append_only_enforced','referral_bonus_idempotent_balance','nonmember_expansion_rls_denied','exact55_and_force_rls14','catalog_hash_independent_of_search_path','server_side_existing_row_fingerprints','no_fake_alembic_head','reviewed_v4_baseline_frozen_hash','reviewed_v4_plus_exact_forum_grant'})

FORUM_DEVELOPER_SEQUENCE_GRANT=('developer_pending_changes_id_seq','<owner>','medical_app_platform_runtime','SELECT',False)

def canonical_developer_inventory(connection):
    from server.platform_developer_catalog import inventory as legacy_inventory
    with connection.transaction():
        previous=connection.execute("SELECT current_setting('search_path')").fetchone()[0]
        connection.execute("SELECT set_config('search_path','pg_catalog',true)")
        value=legacy_inventory(connection)
        connection.execute("SELECT set_config('search_path',%s,true)",(previous,))
        return value

def verify_forum_developer_inventory(value):
    """Prove frozen v4 plus only the explicit, reviewed v5 sequence SELECT.

    This is a proof view in memory. Never use it for the complete old-41 before/
    after catalog comparison, and never change live ACLs or the legacy constant.
    """
    from server.platform_developer_catalog import REVIEWED_V4_SHA256,inventory_digest
    rows=value.get('grants',[])
    require(sum(tuple(row)==FORUM_DEVELOPER_SEQUENCE_GRANT for row in rows)==1,'exact_forum_developer_sequence_grant_required')
    baseline={**value,'grants':[row for row in rows if tuple(row)!=FORUM_DEVELOPER_SEQUENCE_GRANT]}
    require(inventory_digest(baseline)==REVIEWED_V4_SHA256,'reviewed_v4_plus_forum_catalog_mismatch')
    return {'baseline_v4_sha256':REVIEWED_V4_SHA256,'approved_forum_grant':list(FORUM_DEVELOPER_SEQUENCE_GRANT)}

def require_reviewed_v4_with_forum(connection):
    return verify_forum_developer_inventory(canonical_developer_inventory(connection))

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def inventory(connection,tables):
    # PostgreSQL deparsers qualify names according to search_path. Normalize it
    # so identical catalogs hash identically during plan and after seed queries.
    with connection.transaction():
        previous=connection.execute("SELECT current_setting('search_path')").fetchone()[0]
        connection.execute("SELECT set_config('search_path','pg_catalog',true)")
        result=_inventory(connection,tables)
        connection.execute("SELECT set_config('search_path',%s,true)",(previous,))
        return result

def _inventory(connection,tables):
    names=sorted(tables); args=(SCHEMA,names)
    return {
      'relations':connection.execute("SELECT c.relname,c.relkind,pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity,obj_description(c.oid,'pg_class'),c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname",args).fetchall(),
      'columns':connection.execute("SELECT c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid,false) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname=%s AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",args).fetchall(),
      'constraints':connection.execute("SELECT c.relname,k.conname,k.convalidated,pg_get_constraintdef(k.oid,false) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=ANY(%s) ORDER BY c.relname,k.conname",args).fetchall(),
      'indexes':connection.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=ANY(%s) ORDER BY tablename,indexname",args).fetchall(),
      'policies':connection.execute("SELECT tablename,policyname,permissive,roles::text,cmd,qual,with_check FROM pg_policies WHERE schemaname=%s AND tablename=ANY(%s) ORDER BY tablename,policyname",args).fetchall(),
      'sequences':connection.execute("SELECT DISTINCT t.relname,s.relname,pg_get_userbyid(s.relowner),s.relacl::text,q.seqtypid::regtype::text,q.seqstart,q.seqincrement,q.seqmax,q.seqmin,q.seqcache,q.seqcycle FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace JOIN pg_sequence q ON q.seqrelid=s.oid JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=s.oid AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') JOIN pg_class t ON t.oid=d.refobjid WHERE n.nspname=%s AND t.relname=ANY(%s) ORDER BY t.relname,s.relname",args).fetchall(),
    }

def table_set(connection):
    return {row[0] for row in connection.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')",(SCHEMA,)).fetchall()}

def require(condition,reason):
    if not condition: raise ValueError(reason)

def validate_relations(value,tables,owner):
    require({r[0] for r in value['relations']}==set(tables),'exact_table_set_required')
    require(all(tuple(r[1:6])==('r',owner,True,True,'medical-app:platform_0001:owned') for r in value['relations']),'owner_marker_or_force_rls_mismatch')
