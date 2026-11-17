"""Reviewed additive RLS policy change for member appointment requests.

The historical v1/v2 schema modules stay frozen. This file is an independent
operator migration applied only after the 55-table backup. It changes exactly
the UPDATE policy on ``visit_tasks`` and ``visit_task_details``.
"""
from __future__ import annotations

import hashlib

SCHEMA = "medical_app_platform"
RUNTIME = "current_user = 'medical_app_platform_runtime'"
ACTOR = "NULLIF(current_setting('medical_app.user_id', true), '')::bigint"
MEMBER = f"({ACTOR} IS NOT NULL AND current_setting('medical_app.role_code', true) = 'member')"
OPERATOR = f"({ACTOR} IS NOT NULL AND current_setting('medical_app.role_code', true) = 'operator')"
ADVISOR = (
    f"(visit_tasks.advisor_user_id = {ACTOR} AND EXISTS "
    f"(SELECT 1 FROM {SCHEMA}.advisor_bindings binding "
    f"WHERE binding.member_user_id = visit_tasks.member_user_id "
    f"AND binding.advisor_user_id = {ACTOR} AND binding.status = 'active'))"
)
LEGACY_TASK_SCOPE = f"({OPERATOR} OR {ADVISOR})"

TASK_SCOPE = (
    f"{MEMBER} AND visit_tasks.member_user_id = {ACTOR} "
    "AND visit_tasks.status = 'pending' "
    f"AND EXISTS (SELECT 1 FROM {SCHEMA}.visit_task_details details "
    f"WHERE details.task_id = visit_tasks.id AND details.requested_by = {ACTOR} "
    "AND details.outcome_status IS NULL)"
)
DETAIL_SCOPE = (
    f"{MEMBER} AND visit_task_details.requested_by = {ACTOR} "
    f"AND visit_task_details.outcome_status IS NULL AND EXISTS "
    f"(SELECT 1 FROM {SCHEMA}.visit_tasks task "
    f"WHERE task.id = visit_task_details.task_id AND task.member_user_id = {ACTOR} "
    "AND task.status = 'pending')"
)

POLICY_DDL = (
    f"DROP POLICY platform_update ON {SCHEMA}.visit_tasks",
    f"CREATE POLICY platform_update ON {SCHEMA}.visit_tasks FOR UPDATE TO PUBLIC "
    f"USING ({RUNTIME} AND ({LEGACY_TASK_SCOPE} OR {TASK_SCOPE})) "
    f"WITH CHECK ({RUNTIME} AND ({LEGACY_TASK_SCOPE} OR {TASK_SCOPE}))",
    f"DROP POLICY platform_update ON {SCHEMA}.visit_task_details",
    f"CREATE POLICY platform_update ON {SCHEMA}.visit_task_details FOR UPDATE TO PUBLIC "
    f"USING ({RUNTIME} AND ({OPERATOR} OR {DETAIL_SCOPE})) "
    f"WITH CHECK ({RUNTIME} AND ({OPERATOR} OR {DETAIL_SCOPE}))",
)
POLICY_REVISION = "member_booking_policy_0001"
POLICY_DDL_SHA256 = hashlib.sha256("\n".join(POLICY_DDL).encode()).hexdigest()


def policy_fingerprint(rows) -> str:
    """Hash only the two reviewed UPDATE rows from pg_policies."""
    canonical = [
        {
            "tablename": row[0],
            "policyname": row[1],
            "cmd": row[2],
            "qual": row[3],
            "with_check": row[4],
        }
        for row in rows
    ]
    import json

    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def current_update_policies(connection):
    return connection.execute(
        "SELECT tablename,policyname,cmd,qual,with_check FROM pg_policies "
        "WHERE schemaname=%s AND tablename IN ('visit_tasks','visit_task_details') "
        "AND policyname='platform_update' AND cmd='UPDATE' "
        "ORDER BY tablename,policyname", (SCHEMA,)
    ).fetchall()


def verify_member_policy_rows(rows) -> bool:
    """Check the normalized PostgreSQL predicates after DDL application."""
    if len(rows) != 2:
        return False
    by_table = {row[0]: row for row in rows}
    task = by_table.get("visit_tasks")
    detail = by_table.get("visit_task_details")
    if task is None or detail is None or task[1] != "platform_update" or detail[1] != "platform_update":
        return False
    task_sql = " ".join(str(task[index] or "") for index in (3, 4))
    detail_sql = " ".join(str(detail[index] or "") for index in (3, 4))
    return (
        "role_code" in task_sql and "member" in task_sql and "operator" in task_sql
        and "advisor_user_id" in task_sql and "status" in task_sql and "pending" in task_sql
        and "details.requested_by" in task_sql
        and "role_code" in detail_sql and "member" in detail_sql and "operator" in detail_sql
        and "requested_by" in detail_sql and "outcome_status" in detail_sql
        and "status" in detail_sql and "pending" in detail_sql
    )

