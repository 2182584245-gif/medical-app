"""Member expansion; exact 55-table backup support must precede promotion."""
from alembic import op
from server.platform_schema import PLATFORM_SCHEMA,PLATFORM_MARKER
from expansion.schema import TABLE_DDL,INDEX_DDL,policy_ddl,grant_ddl
from expansion.contracts import NEW_TABLES
revision="platform_0006_fawnsee_expansion"
down_revision="platform_0005"
branch_labels=None
depends_on=None

def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    op.execute(f"""DO $guard$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname='{PLATFORM_SCHEMA}' AND pg_catalog.obj_description(oid,'pg_namespace')='{PLATFORM_MARKER}' AND pg_catalog.pg_get_userbyid(nspowner)=current_user) THEN RAISE EXCEPTION 'Refusing unowned expansion migration'; END IF; END; $guard$;""")
    for statement in TABLE_DDL: op.execute(statement)
    from pathlib import Path
    from expansion.seed import load_bundled_food_seeds
    from server.platform_database import compile_sql
    class SeedConnection:
        def execute(self,statement,parameters=()):
            compiled,bound,_implicit=compile_sql(statement,parameters)
            return op.get_bind().exec_driver_sql(compiled,bound)
    load_bundled_food_seeds(SeedConnection(),Path(__file__).resolve().parents[3]/"data"/"nutrition-catalog")
    for table in NEW_TABLES: op.execute(f"COMMENT ON TABLE {PLATFORM_SCHEMA}.{table} IS '{PLATFORM_MARKER}'")
    for statement in (*INDEX_DDL,*policy_ddl(),*grant_ddl()): op.execute(statement)

def downgrade():
    raise RuntimeError("Automatic expansion downgrade is blocked; preserve member records and append-only ledgers.")
