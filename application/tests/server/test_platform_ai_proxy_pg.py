"""Opt-in new local PG17 container; actual runtime RLS and independent engines.

The reused fixture rejects nonlocal Docker endpoints, uses a random new tmpfs
database/container, and cleans only its label-verified resources. Never accepts
cloud URLs or existing user data. No upstream AI request is made by these tests.
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from server.platform_ai_proxy import AIProxyError, AIProxySettings, AIQuota
from server.platform_auth import PlatformAuth
from server.platform_config import PlatformSettings
from server.platform_database import PlatformDatabase
from server.platform_experience_schema import runtime_grant_ddl
from server.platform_security import SharedAuthLimiter
from tests.server import test_platform_transfer_pg as local_pg

pg_cluster = local_pg.pg_cluster
target_db = local_pg.target_db
PEPPER = bytes(range(32)).hex()


@pytest.fixture
def runtime_quotas(target_db, pg_cluster):
    with target_db.transaction():
        exists = target_db.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='medical_app_platform_runtime'"
        ).fetchone()
        if not exists:
            target_db.execute(
                "CREATE ROLE medical_app_platform_runtime LOGIN NOINHERIT "
                "NOSUPERUSER NOBYPASSRLS PASSWORD 'synthetic-ai-runtime-only'"
            )
        for statement in runtime_grant_ddl():
            target_db.execute(statement)
    url = URL.create(
        "postgresql+psycopg",
        host="127.0.0.1",
        port=pg_cluster["port"],
        database=target_db.info.dbname,
        username="medical_app_platform_runtime",
        password="synthetic-ai-runtime-only",
    )
    databases = [
        PlatformDatabase(
            engine=create_engine(
                url, hide_parameters=True, pool_size=4, max_overflow=0, pool_timeout=5
            )
        )
        for _ in range(2)
    ]
    settings = PlatformSettings(
        env="test", token_pepper=PEPPER, database_url="sqlite+pysqlite:///:memory:"
    )
    auth = [PlatformAuth(database, settings) for database in databases]
    quotas = [AIQuota(item, AIProxySettings()) for item in auth]
    try:
        yield target_db, databases, auth, quotas
    finally:
        for database in databases:
            database.close()


def reserve(quotas, index, user_id):
    try:
        quotas[index % 2].reserve(user_id)
        return True
    except AIProxyError as error:
        assert error.status == 429
        return False


def test_actual_postgres_runtime_and_two_engines_enforce_same_user_limit(runtime_quotas):
    admin, databases, _auth, quotas = runtime_quotas
    with databases[0].connect() as connection:
        row = connection.execute(
            "SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert row[:] == ("medical_app_platform_runtime", False, False)
    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(lambda index: reserve(quotas, index, 41), range(36)))
    assert sum(accepted) == 2
    with admin.transaction():
        assert (
            admin.execute(
                "SELECT attempts FROM medical_app_platform.auth_rate_buckets WHERE bucket_id=8191"
            ).fetchone()[0]
            == 2
        )
        assert admin.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='medical_app_platform.auth_rate_buckets'::regclass"
        ).fetchone() == (True, True)
    with pytest.raises(sqlite3.IntegrityError), databases[0].transaction() as connection:
        connection.execute("INSERT INTO auth_rate_buckets VALUES (16385,0,1)")


def test_actual_postgres_global_minute_multiple_accounts_and_no_cross_cleanup(runtime_quotas):
    admin, _databases, auth, quotas = runtime_quotas
    with ThreadPoolExecutor(max_workers=10) as pool:
        accepted = list(pool.map(lambda index: reserve(quotas, index, index + 100), range(30)))
    assert sum(accepted) == 6
    with admin.transaction():
        # Midnight day windows must survive authentication's minute-age cleanup.
        previous = admin.execute(
            "SELECT bucket_id,window_start,attempts FROM medical_app_platform.auth_rate_buckets "
            "WHERE bucket_id>=8191 ORDER BY bucket_id"
        ).fetchall()
    auth[0].limiter.check("synthetic-client", "synthetic-account")
    with admin.transaction():
        current = admin.execute(
            "SELECT bucket_id,window_start,attempts FROM medical_app_platform.auth_rate_buckets "
            "WHERE bucket_id>=8191 ORDER BY bucket_id"
        ).fetchall()
    assert current == previous


def test_actual_postgres_global_daily_final_slot_is_atomic(runtime_quotas):
    admin, _databases, _auth, quotas = runtime_quotas
    with admin.transaction():
        admin.execute(
            "INSERT INTO medical_app_platform.auth_rate_buckets VALUES "
            "(8191, floor(extract(epoch FROM clock_timestamp())/86400)::bigint*86400, 99)"
        )
    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(lambda index: reserve(quotas, index, index + 1000), range(36)))
    assert sum(accepted) == 1
    with admin.transaction():
        assert (
            admin.execute(
                "SELECT attempts FROM medical_app_platform.auth_rate_buckets WHERE bucket_id=8191"
            ).fetchone()[0]
            == 100
        )


def test_actual_postgres_previous_day_reset_and_future_day_fail_closed(runtime_quotas):
    admin, _databases, _auth, quotas = runtime_quotas
    with admin.transaction():
        today = admin.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())/86400)::bigint*86400"
        ).fetchone()[0]
        admin.execute(
            "INSERT INTO medical_app_platform.auth_rate_buckets VALUES (8191,%s,100)",
            (today - 86400,),
        )
    quotas[0].reserve(42)
    with admin.transaction():
        assert admin.execute(
            "SELECT window_start,attempts FROM medical_app_platform.auth_rate_buckets "
            "WHERE bucket_id=8191"
        ).fetchone() == (today, 1)
        admin.execute(
            "UPDATE medical_app_platform.auth_rate_buckets SET window_start=%s "
            "WHERE bucket_id=8191",
            (today + 86400,),
        )
    with pytest.raises(AIProxyError) as failure:
        quotas[1].reserve(43)
    assert failure.value.status == 429


def test_actual_postgres_legacy_auth_slots_remain_bounded_and_conservative(runtime_quotas):
    admin, _databases, auth, quotas = runtime_quotas
    with admin.transaction():
        today = admin.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())/86400)::bigint*86400"
        ).fetchone()[0]
        admin.execute(
            "INSERT INTO medical_app_platform.auth_rate_buckets VALUES (8191,%s,7)",
            (today + 3600,),
        )
    quotas[0].reserve(44)  # Old minute-shaped values do not impersonate future AI days.
    with admin.transaction():
        assert admin.execute(
            "SELECT window_start,attempts FROM medical_app_platform.auth_rate_buckets "
            "WHERE bucket_id=8191"
        ).fetchone() == (today, 1)
    limiter = SharedAuthLimiter(auth[0].database, auth[0].settings)
    assert max(limiter.bucket_ids("synthetic-client", "synthetic-account")) <= 8190
