"""Platform v2 additions; platform_schema remains the immutable v1 snapshot.

New legacy-compatible timestamps remain canonical UTC text with SQL validation,
because desktop service contracts compare and decode them as ISO strings. New
tables stay private and use the reviewed backend identity plus forced RLS.
"""

from __future__ import annotations

from . import platform_schema as v1

PLATFORM_SCHEMA = v1.PLATFORM_SCHEMA
PLATFORM_RUNTIME_ROLE = v1.PLATFORM_RUNTIME_ROLE
PLATFORM_MARKER = v1.PLATFORM_MARKER  # Stable schema ownership, independent of version.
PLATFORM_REVISION = "platform_0002"
IDENTITY_TABLES = v1.IDENTITY_TABLES
NEW_COLUMNS = {
    "user_preferences": ("user_id", "preferences_json", "updated_at"),
    "member_cart": ("user_id", "product_id", "quantity"),
    "member_favorites": ("user_id", "product_id"),
    "staff_account_terms": ("user_id", "starts_at", "ends_at", "created_at", "updated_at"),
    "visit_task_details": (
        "task_id",
        "service_type",
        "address",
        "latitude",
        "longitude",
        "outcome_status",
        "requested_by",
        "created_at",
        "updated_at",
    ),
}
BUSINESS_COLUMNS = {**v1.BUSINESS_COLUMNS, **NEW_COLUMNS}
BUSINESS_TABLES = tuple(BUSINESS_COLUMNS)
PLATFORM_COLUMNS = {**v1.PLATFORM_COLUMNS, **NEW_COLUMNS}
PLATFORM_TABLES = tuple(PLATFORM_COLUMNS)
NEW_TABLE_DDL = (
    f"""CREATE TABLE {PLATFORM_SCHEMA}.user_preferences (
        user_id BIGINT PRIMARY KEY REFERENCES {PLATFORM_SCHEMA}.users(id) ON DELETE CASCADE,
        preferences_json TEXT NOT NULL DEFAULT '{{}}'
            CHECK (preferences_json IS JSON OBJECT AND octet_length(preferences_json) <= 400000),
        updated_at TEXT NOT NULL
    )""",
    f"""CREATE TABLE {PLATFORM_SCHEMA}.member_cart (
        user_id BIGINT NOT NULL REFERENCES {PLATFORM_SCHEMA}.users(id) ON DELETE CASCADE,
        product_id BIGINT NOT NULL REFERENCES {PLATFORM_SCHEMA}.products(id) ON DELETE CASCADE,
        quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999),
        PRIMARY KEY(user_id, product_id)
    )""",
    f"""CREATE TABLE {PLATFORM_SCHEMA}.member_favorites (
        user_id BIGINT NOT NULL REFERENCES {PLATFORM_SCHEMA}.users(id) ON DELETE CASCADE,
        product_id BIGINT NOT NULL REFERENCES {PLATFORM_SCHEMA}.products(id) ON DELETE CASCADE,
        PRIMARY KEY(user_id, product_id)
    )""",
    f"""CREATE TABLE {PLATFORM_SCHEMA}.staff_account_terms (
        user_id BIGINT PRIMARY KEY REFERENCES {PLATFORM_SCHEMA}.users(id) ON DELETE CASCADE,
        starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK (starts_at < ends_at)
    )""",
    f"""CREATE TABLE {PLATFORM_SCHEMA}.visit_task_details (
        task_id BIGINT PRIMARY KEY REFERENCES {PLATFORM_SCHEMA}.visit_tasks(id) ON DELETE CASCADE,
        service_type TEXT NOT NULL CHECK(length(service_type) BETWEEN 1 AND 200),
        address TEXT CHECK(length(address) <= 2000),
        latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
        outcome_status TEXT CHECK(outcome_status IN ('incomplete', 'disabled')),
        requested_by BIGINT REFERENCES {PLATFORM_SCHEMA}.users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(latitude IS NULL OR latitude BETWEEN -90 AND 90),
        CHECK(longitude IS NULL OR longitude BETWEEN -180 AND 180),
        CHECK((latitude IS NULL) = (longitude IS NULL))
    )""",
)
NEW_INDEX_DDL = tuple(
    f"CREATE INDEX {name} ON {PLATFORM_SCHEMA}.{table}({column})"
    for name, table, column in (
        ("idx_member_cart_product", "member_cart", "product_id"),
        ("idx_member_favorites_product", "member_favorites", "product_id"),
        ("idx_visit_details_requester", "visit_task_details", "requested_by"),
        ("idx_staff_terms_expiry", "staff_account_terms", "ends_at"),
    )
)


def compatibility_constraints() -> tuple[str, ...]:
    pattern = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
    return tuple(
        f"ALTER TABLE {PLATFORM_SCHEMA}.{table} ADD CONSTRAINT ck_{table}_{column}_encoding "
        f"CHECK ({column} ~ '{pattern}' AND {column}::timestamptz IS NOT NULL)"
        for table, columns in NEW_COLUMNS.items()
        for column in columns
        if column.endswith("_at")
    )


def policy_rules() -> dict:
    rules = v1.policy_rules()
    actor, role, operator = v1.ACTOR_ID, v1.ACTOR_ROLE, v1.OPERATOR

    def access(table, read, write, *, delete=False):
        rules[table] = {
            "SELECT": (read, None),
            "INSERT": ("", write),
            "UPDATE": (write, write),
        }
        if delete:
            rules[table]["DELETE"] = (write, None)

    access("user_preferences", f"user_id = {actor}", f"user_id = {actor}")
    for table in ("member_cart", "member_favorites"):
        own = f"({role} = 'member' AND user_id = {actor})"
        access(table, own, own, delete=True)
    # Self-read has no users/bindings dependency, so expiry checks cannot recurse.
    access("staff_account_terms", f"(user_id = {actor} OR {operator})", operator)
    visible_task = f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.visit_tasks t WHERE t.id = task_id)"
    member_request = (
        f"({role} = 'member' AND requested_by = {actor} AND outcome_status IS NULL AND "
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.visit_tasks t WHERE t.id = task_id "
        f"AND t.member_user_id = {actor} AND t.status = 'pending'))"
    )
    rules["visit_task_details"] = {
        "SELECT": (visible_task, None),
        "INSERT": ("", f"({operator} OR {member_request})"),
        "UPDATE": (operator, operator),
    }
    old_insert = rules["visit_tasks"]["INSERT"][1]
    assigned = (
        f"(advisor_user_id IS NULL OR EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.advisor_bindings b "
        f"WHERE b.member_user_id = {actor} AND b.advisor_user_id = visit_tasks.advisor_user_id "
        "AND b.status = 'active'))"
    )
    rules["visit_tasks"]["INSERT"] = (
        "",
        f"({old_insert} OR ({role} = 'member' AND member_user_id = {actor} "
        f"AND status = 'pending' AND {assigned}))",
    )
    old_product_read = rules["products"]["SELECT"][0]
    cart_history = " OR ".join(
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.{table} mine WHERE mine.product_id = products.id)"
        for table in ("member_cart", "member_favorites")
    )
    rules["products"]["SELECT"] = (f"({old_product_read} OR ({cart_history}))", None)
    # The fixed operator password-reset RPC also revokes the target's sessions.
    # No client accepts a session row or arbitrary SQL to edit.
    session_write = f"(user_id = {actor} OR {operator})"
    rules["platform_sessions"]["UPDATE"] = (session_write, session_write)
    return rules


EXPIRY_TABLES = tuple(
    table
    for table in PLATFORM_TABLES
    if table
    not in {
        "users",
        "staff_account_terms",
        "platform_sessions",
        "auth_rate_buckets",
    }
)


def expiry_predicate() -> str:
    # Existing staff without terms retain their prior access until an operator
    # sets terms. Every new advisor receives explicit terms in the service layer.
    return (
        f"(COALESCE({v1.ACTOR_ROLE}, '') <> 'advisor' OR NOT EXISTS "
        f"(SELECT 1 FROM {PLATFORM_SCHEMA}.staff_account_terms term "
        f"WHERE term.user_id = {v1.ACTOR_ID}) OR EXISTS "
        f"(SELECT 1 FROM {PLATFORM_SCHEMA}.staff_account_terms term "
        f"WHERE term.user_id = {v1.ACTOR_ID} "
        "AND term.starts_at::timestamptz <= statement_timestamp() "
        "AND statement_timestamp() < term.ends_at::timestamptz))"
    )


def incremental_policy_ddl() -> tuple[str, ...]:
    statements = []
    changed = {
        **{key: policy_rules()[key] for key in NEW_COLUMNS},
        "visit_tasks": {"INSERT": policy_rules()["visit_tasks"]["INSERT"]},
        "products": {"SELECT": policy_rules()["products"]["SELECT"]},
        "platform_sessions": {"UPDATE": policy_rules()["platform_sessions"]["UPDATE"]},
    }
    for table, commands in changed.items():
        qualified = f"{PLATFORM_SCHEMA}.{table}"
        if table in NEW_COLUMNS:
            statements.extend(
                (
                    f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY",
                    f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY",
                )
            )
        for command, (using, check) in commands.items():
            if table not in NEW_COLUMNS:
                statements.append(f"DROP POLICY platform_{command.lower()} ON {qualified}")
            sql = f"CREATE POLICY platform_{command.lower()} ON {qualified} FOR {command} TO PUBLIC"
            if command != "INSERT":
                sql += f" USING ({v1.RUNTIME} AND ({using}))"
            if check is not None:
                sql += f" WITH CHECK ({v1.RUNTIME} AND ({check}))"
            statements.append(sql)
    for table in EXPIRY_TABLES:
        statements.append(
            f"CREATE POLICY platform_staff_term ON {PLATFORM_SCHEMA}.{table} AS RESTRICTIVE "
            f"FOR ALL TO PUBLIC USING ({expiry_predicate()}) WITH CHECK ({expiry_predicate()})"
        )
    return tuple(statements)


def runtime_grant_ddl() -> tuple[str, ...]:
    return (
        *v1.runtime_grant_ddl(),
        *(
            f"GRANT {', '.join(policy_rules()[table])} ON TABLE {PLATFORM_SCHEMA}.{table} "
            f"TO {PLATFORM_RUNTIME_ROLE}"
            for table in NEW_COLUMNS
        ),
    )
