"""Additive v6 schema. PostgreSQL DDL is migration-only, never runtime DDL."""
from __future__ import annotations
import hashlib
import json
from .contracts import NEW_TABLES, REVISION, SCHEMA

IDENTITY_TABLES = frozenset({"expansion_record_revisions", "expansion_meals", "expansion_analyses", "expansion_memories", "expansion_observations", "expansion_goals", "expansion_habit_entries", "expansion_point_events", "expansion_referrals"})
_DEFINITIONS = {
 "expansion_record_revisions": "id @ID@, user_id BIGINT NOT NULL @USER@, entity_type TEXT NOT NULL, entity_id BIGINT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('create','update','delete')), before_json TEXT, after_json TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL",
 "expansion_foods": "id TEXT PRIMARY KEY, owner_user_id BIGINT @USER@, name TEXT NOT NULL, category TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(length(payload_json)<=40000), created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
 "expansion_food_favorites": "user_id BIGINT NOT NULL @USER@, food_id TEXT NOT NULL REFERENCES @S@expansion_foods(id) ON DELETE CASCADE, created_at TEXT NOT NULL, PRIMARY KEY(user_id,food_id)",
 "expansion_meals": "id @ID@, user_id BIGINT NOT NULL @USER@, life_record_id BIGINT NOT NULL UNIQUE REFERENCES @S@life_records(id) ON DELETE CASCADE, occurred_at TEXT NOT NULL, meal_type TEXT NOT NULL CHECK(meal_type IN ('breakfast','lunch','dinner','snack')), payload_json TEXT NOT NULL CHECK(length(payload_json)<=80000), created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
 "expansion_analyses": "id @ID@, user_id BIGINT NOT NULL @USER@, kind TEXT NOT NULL CHECK(kind IN ('meal_photo','report_prefill','memory_extract','meal_review','daily_review','weekly_review','monthly_review','nutrition_check')), status TEXT NOT NULL CHECK(status IN ('pending','ready','confirmed','dismissed')), payload_json TEXT NOT NULL CHECK(length(payload_json)<=110000), created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
 "expansion_memories": "id @ID@, user_id BIGINT NOT NULL @USER@, memory_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('preference','routine','explicit_fact')), status TEXT NOT NULL CHECK(status IN ('pending','confirmed','revoked')), payload_json TEXT NOT NULL CHECK(length(payload_json)<=12000), created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
 "expansion_observations": "id @ID@, user_id BIGINT NOT NULL @USER@, kind TEXT NOT NULL CHECK(kind IN ('weight','glucose','blood_pressure','lipids','height','custom')), measured_at TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(length(payload_json)<=16000), created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
 "expansion_goals": "id @ID@, user_id BIGINT NOT NULL @USER@, kind TEXT NOT NULL CHECK(kind IN ('water','diet','weight','glucose','reading','custom')), status TEXT NOT NULL CHECK(status IN ('active','archived')), payload_json TEXT NOT NULL CHECK(length(payload_json)<=12000), created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
 "expansion_habit_entries": "id @ID@, user_id BIGINT NOT NULL @USER@, goal_id BIGINT NOT NULL REFERENCES @S@expansion_goals(id) ON DELETE CASCADE, day TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id,goal_id,day)",
 "expansion_content_progress": "user_id BIGINT NOT NULL @USER@, content_id TEXT NOT NULL, progress DOUBLE PRECISION NOT NULL CHECK(progress BETWEEN 0 AND 1), saved SMALLINT NOT NULL CHECK(saved IN (0,1)), completed_at TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(user_id,content_id)",
 "expansion_point_events": "id @ID@, user_id BIGINT NOT NULL @USER@, delta INTEGER NOT NULL CHECK(delta BETWEEN -100 AND 100), event_key TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id,event_key)",
 "expansion_invitation_codes": "user_id BIGINT PRIMARY KEY @USER@, code TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL",
 "expansion_referrals": "id @ID@, inviter_user_id BIGINT NOT NULL @USER@, invitee_user_id BIGINT NOT NULL UNIQUE @USER@, code TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('accepted','qualified')), created_at TEXT NOT NULL, qualified_at TEXT, CHECK(inviter_user_id<>invitee_user_id)",
 "expansion_settings": "user_id BIGINT PRIMARY KEY @USER@, payload_json TEXT NOT NULL CHECK(length(payload_json)<=12000), updated_at TEXT NOT NULL",
}
INDEXES = (
 ("idx_expansion_revisions_owner", "expansion_record_revisions", "user_id,entity_type,entity_id,id"),
 ("idx_expansion_foods_owner", "expansion_foods", "owner_user_id,category,name"),
 ("idx_expansion_meals_owner", "expansion_meals", "user_id,occurred_at,id"),
 ("idx_expansion_analyses_owner", "expansion_analyses", "user_id,kind,status,id"),
 ("idx_expansion_memories_owner", "expansion_memories", "user_id,status,id"),
 ("idx_expansion_observations_owner", "expansion_observations", "user_id,kind,measured_at,id"),
 ("idx_expansion_goals_owner", "expansion_goals", "user_id,status,id"),
 ("idx_expansion_habits_owner", "expansion_habit_entries", "user_id,day,goal_id"),
 ("idx_expansion_points_owner", "expansion_point_events", "user_id,created_at,id"),
 ("idx_expansion_referrals_inviter", "expansion_referrals", "inviter_user_id,status"),
)

def table_ddl(postgres=True):
    prefix = SCHEMA + "." if postgres else ""
    identity = "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    user = "REFERENCES " + prefix + "users(id) ON DELETE CASCADE"
    return tuple("CREATE TABLE " + prefix + table + " (" + definition.replace("@ID@", identity).replace("@USER@", user).replace("@S@", prefix) + ")" for table, definition in _DEFINITIONS.items())

TABLE_DDL = table_ddl()
INDEX_DDL = tuple(f"CREATE INDEX {name} ON {SCHEMA}.{table}({columns})" for name,table,columns in INDEXES)
SCHEMA_COLUMNS={table:tuple(part.split()[0] for part in definition.split(", ") if not part.startswith(("PRIMARY ","UNIQUE(","CHECK("))) for table,definition in _DEFINITIONS.items()}

def policy_ddl():
    from server.platform_schema import ACTOR_ID, RUNTIME, SIGNED_IN
    member = f"{RUNTIME} AND {SIGNED_IN} AND current_setting('medical_app.role_code',true)='member'"
    result = []
    for table in NEW_TABLES:
        result.extend((f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY", f"ALTER TABLE {SCHEMA}.{table} FORCE ROW LEVEL SECURITY"))
        own = f"user_id={ACTOR_ID}"
        if table == "expansion_foods":
            own = f"owner_user_id={ACTOR_ID}"
            read = f"({own} OR owner_user_id IS NULL)"
        elif table == "expansion_invitation_codes":
            read = "true"  # Only opaque invitation code + identifier; no health or PII.
        elif table == "expansion_referrals":
            own = f"invitee_user_id={ACTOR_ID}"
            read = f"({own} OR inviter_user_id={ACTOR_ID})"
        elif table == "expansion_point_events":
            read = f"({own} OR EXISTS (SELECT 1 FROM {SCHEMA}.expansion_referrals r WHERE r.invitee_user_id={ACTOR_ID} AND r.inviter_user_id=expansion_point_events.user_id AND r.status='qualified' AND expansion_point_events.event_key='invite:'||r.id::text AND expansion_point_events.delta=20))"
        else:
            read = own
        result.append(f"CREATE POLICY {table}_read ON {SCHEMA}.{table} FOR SELECT TO PUBLIC USING ({member} AND {read})")
        insert = own
        if table == "expansion_point_events":
            insert = f"({own} OR EXISTS (SELECT 1 FROM {SCHEMA}.expansion_referrals r WHERE r.invitee_user_id={ACTOR_ID} AND r.inviter_user_id=expansion_point_events.user_id AND r.status='qualified' AND expansion_point_events.event_key='invite:'||r.id::text AND expansion_point_events.delta=20))"
        result.append(f"CREATE POLICY {table}_insert ON {SCHEMA}.{table} FOR INSERT TO PUBLIC WITH CHECK ({member} AND {insert})")
        if table not in {"expansion_record_revisions", "expansion_point_events", "expansion_invitation_codes"}:
            result.append(f"CREATE POLICY {table}_update ON {SCHEMA}.{table} FOR UPDATE TO PUBLIC USING ({member} AND {own}) WITH CHECK ({member} AND {own})")
            result.append(f"CREATE POLICY {table}_delete ON {SCHEMA}.{table} FOR DELETE TO PUBLIC USING ({member} AND {own})")
    return tuple(result)

def grant_ddl():
    from server.platform_schema import PLATFORM_RUNTIME_ROLE
    result=[]
    for table in NEW_TABLES:
        verbs = "SELECT,INSERT" if table in {"expansion_record_revisions", "expansion_point_events", "expansion_invitation_codes"} else "SELECT,INSERT,UPDATE,DELETE"
        result.append(f"GRANT {verbs} ON TABLE {SCHEMA}.{table} TO {PLATFORM_RUNTIME_ROLE}")
        if table in IDENTITY_TABLES:
            result.append(f"GRANT USAGE,SELECT ON SEQUENCE {SCHEMA}.{table}_id_seq TO {PLATFORM_RUNTIME_ROLE}")
    return tuple(result)

def manifest():
    sql = "\n".join((*TABLE_DDL,*INDEX_DDL,*policy_ddl(),*grant_ddl()))
    return {"schema_version":1,"revision":REVISION,"down_revision":"platform_0005","schema":SCHEMA,"previous_table_count":41,"table_count":55,"new_tables":list(NEW_TABLES),"identity_tables":sorted(IDENTITY_TABLES),"ddl_sha256":hashlib.sha256(sql.encode()).hexdigest(),"sync":{"mode":"online_only","resources":list(NEW_TABLES),"offline_mutations":False},"backup":{"include_all_new_tables":True,"exact_table_set_required":True}}

def initialize_sqlite(database):
    """Explicit test/local-fixture helper. The production PostgreSQL adapter is refused."""
    if database.__class__.__module__.startswith("server."):
        raise RuntimeError("runtime_schema_upgrade_forbidden")
    with database.transaction() as connection:
        existing={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()} & set(NEW_TABLES)
        if existing:
            if existing!=set(NEW_TABLES): raise RuntimeError("partial_expansion_schema")
            for table,columns in SCHEMA_COLUMNS.items():
                actual=tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall())
                if actual!=columns: raise RuntimeError("expansion_schema_mismatch")
            return
        for statement in table_ddl(False):
            connection.execute(statement)
        for name,table,columns in INDEXES:
            connection.execute(f"CREATE INDEX {name} ON {table}({columns})")
