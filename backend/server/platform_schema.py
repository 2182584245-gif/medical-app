"""Frozen full-platform schema v1, reviewed from desktop SQLite schema v5.

This is additive to the private pilot schema; no runtime SQLite fallback exists.
UTC instants stay canonical ISO-8601 TEXT for compatibility, not TIMESTAMPTZ.
RLS is backend-identity defence in depth, not protection from a leaked DB password.
This module is frozen for platform_0001. Later migrations need a separate schema
module; do not retroactively change the DDL used by an applied revision.
"""

# Frozen DDL strings intentionally retain readable SQL clauses as single lines.
# ruff: noqa: E501
from __future__ import annotations

PLATFORM_SCHEMA = "medical_app_platform"
PLATFORM_RUNTIME_ROLE = "medical_app_platform_runtime"
PLATFORM_REVISION = "platform_0001"
PLATFORM_MARKER = "medical-app:platform_0001:owned"

BUSINESS_COLUMNS = {
    "users": (
        "id",
        "username",
        "username_normalized",
        "password_hash",
        "created_at",
        "last_login_at",
        "role_code",
        "account_status",
        "updated_at",
    ),
    "member_profiles": (
        "user_id",
        "display_name",
        "birth_date",
        "gender",
        "phone",
        "living_situation",
        "emergency_contact_name",
        "emergency_contact_phone",
        "ai_preferred_name",
        "reminder_frequency",
        "height_cm",
        "health_goals",
        "dietary_preferences",
        "medical_notes",
        "created_at",
        "updated_at",
    ),
    "profile_facts": (
        "id",
        "user_id",
        "fact_key",
        "value_json",
        "source",
        "status",
        "effective_at",
        "created_at",
        "updated_at",
    ),
    "life_records": (
        "id",
        "user_id",
        "category",
        "occurred_at",
        "local_date",
        "timezone_offset_minutes",
        "content",
        "details_json",
        "source",
        "created_at",
        "updated_at",
    ),
    "reminders": (
        "id",
        "user_id",
        "title",
        "reminder_type",
        "scheduled_at",
        "status",
        "created_at",
        "updated_at",
        "repeat_rule",
        "source",
        "paused_until",
    ),
    "advisor_profiles": (
        "user_id",
        "display_name",
        "organization",
        "specialty",
        "bio",
        "created_at",
        "updated_at",
    ),
    "advisor_bindings": (
        "id",
        "member_user_id",
        "advisor_user_id",
        "status",
        "started_at",
        "ended_at",
        "created_at",
        "updated_at",
    ),
    "memberships": (
        "id",
        "user_id",
        "plan_code",
        "status",
        "starts_at",
        "ends_at",
        "benefits_json",
        "created_at",
        "updated_at",
    ),
    "visit_tasks": (
        "id",
        "member_user_id",
        "advisor_user_id",
        "title",
        "scheduled_at",
        "status",
        "notes",
        "created_at",
        "updated_at",
    ),
    "visit_records": (
        "id",
        "task_id",
        "member_user_id",
        "advisor_user_id",
        "visited_at",
        "summary",
        "details_json",
        "created_at",
        "updated_at",
    ),
    "user_files": (
        "id",
        "user_id",
        "relative_path",
        "original_name",
        "media_type",
        "size_bytes",
        "sha256",
        "created_at",
    ),
    "medical_reports": (
        "id",
        "user_id",
        "file_id",
        "report_type",
        "report_date",
        "institution",
        "summary",
        "status",
        "created_at",
        "updated_at",
    ),
    "report_items": (
        "id",
        "report_id",
        "item_name",
        "result_value",
        "unit",
        "reference_range",
        "flag",
        "created_at",
        "status",
        "updated_at",
    ),
    "ai_insights": (
        "id",
        "user_id",
        "insight_type",
        "content",
        "evidence_json",
        "provider",
        "model",
        "status",
        "created_at",
        "updated_at",
    ),
    "advisor_summaries": (
        "id",
        "member_user_id",
        "advisor_user_id",
        "period_start",
        "period_end",
        "content",
        "source",
        "created_at",
        "updated_at",
    ),
    "app_settings": (
        "id",
        "user_id",
        "setting_key",
        "value_json",
        "created_at",
        "updated_at",
    ),
    "audit_logs": (
        "id",
        "actor_user_id",
        "action",
        "entity_type",
        "entity_id",
        "details_json",
        "created_at",
    ),
    "products": (
        "id",
        "sku",
        "name",
        "category",
        "brand",
        "specification",
        "unit",
        "image_path",
        "description",
        "price_cents",
        "source_type",
        "is_active",
        "created_by_user_id",
        "created_at",
        "updated_at",
    ),
    "product_recommendations": (
        "id",
        "product_id",
        "member_user_id",
        "advisor_user_id",
        "reason",
        "recommendation_source",
        "status",
        "viewed_at",
        "interested_at",
        "created_at",
        "updated_at",
    ),
    "orders": (
        "id",
        "order_no",
        "member_user_id",
        "product_id",
        "recommendation_id",
        "reordered_from_order_id",
        "product_name_snapshot",
        "quantity",
        "unit_price_cents",
        "total_amount_cents",
        "currency",
        "status",
        "delivered_at",
        "created_at",
        "updated_at",
    ),
    "user_file_contents": (
        "file_id",
        "content",
        "mime_type",
        "sha256",
        "created_at",
    ),
    "conversations": (
        "id",
        "user_id",
        "title",
        "provider",
        "model",
        "created_at",
        "updated_at",
    ),
    "messages": (
        "id",
        "conversation_id",
        "sequence_no",
        "role",
        "content",
        "status",
        "error_message",
        "provider",
        "model",
        "created_at",
        "updated_at",
    ),
    "chat_attachments": (
        "id",
        "message_id",
        "original_name",
        "media_type",
        "content",
        "extracted_text",
        "extraction_method",
        "sha256",
        "created_at",
    ),
}
BUSINESS_TABLES = tuple(BUSINESS_COLUMNS)
IDENTITY_TABLES = frozenset(
    name for name, columns in BUSINESS_COLUMNS.items() if columns[0] == "id"
)

# Static DDL, never generated from a downloaded client's SQL or database.
TABLE_DDL = (
    r"""CREATE TABLE medical_app_platform."users" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                username TEXT NOT NULL,
                username_normalized TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT, role_code TEXT NOT NULL DEFAULT 'member'
                CHECK (role_code IN ('member', 'advisor', 'operator')), account_status TEXT NOT NULL DEFAULT 'active'
                CHECK (account_status IN ('active', 'disabled', 'locked')), updated_at TEXT NOT NULL
                DEFAULT '1970-01-01T00:00:00.000000Z',
                CHECK (length(username_normalized) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."member_profiles" (
                user_id BIGINT PRIMARY KEY,
                display_name TEXT,
                birth_date TEXT,
                gender TEXT CHECK (
                    gender IS NULL OR gender IN ('female', 'male', 'other', 'unspecified')
                ),
                phone TEXT,
                living_situation TEXT,
                emergency_contact_name TEXT,
                emergency_contact_phone TEXT,
                ai_preferred_name TEXT,
                reminder_frequency TEXT DEFAULT 'normal' CHECK (
                    reminder_frequency IS NULL
                    OR reminder_frequency IN ('normal', 'low', 'off')
                ),
                height_cm DOUBLE PRECISION CHECK (height_cm IS NULL OR (height_cm >= 30 AND height_cm <= 300)),
                health_goals TEXT,
                dietary_preferences TEXT,
                medical_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE
            )""",
    r"""CREATE TABLE medical_app_platform."profile_facts" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                fact_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('draft', 'confirmed', 'superseded')),
                effective_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                CHECK (length(trim(fact_key)) > 0),
                CHECK (length(trim(value_json)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."life_records" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('diet', 'water', 'activity', 'sleep', 'environment')),
                occurred_at TEXT NOT NULL,
                local_date TEXT NOT NULL,
                timezone_offset_minutes BIGINT NOT NULL
                    CHECK (timezone_offset_minutes BETWEEN -840 AND 840),
                content TEXT NOT NULL,
                details_json TEXT,
                source TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual', 'import', 'device', 'ai_confirmed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                CHECK (length(trim(content)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."reminders" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                reminder_type TEXT NOT NULL DEFAULT 'custom',
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'completed', 'disabled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, repeat_rule TEXT NOT NULL DEFAULT 'none'
                CHECK (repeat_rule IN ('none', 'daily', 'weekly', 'monthly')), source TEXT NOT NULL DEFAULT 'manual'
                CHECK (source IN ('manual', 'ai_confirmed', 'advisor')), paused_until TEXT,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                CHECK (length(trim(title)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."advisor_profiles" (
                user_id BIGINT PRIMARY KEY,
                display_name TEXT NOT NULL,
                organization TEXT,
                specialty TEXT,
                bio TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE
            )""",
    r"""CREATE TABLE medical_app_platform."advisor_bindings" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                member_user_id BIGINT NOT NULL,
                advisor_user_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('pending', 'active', 'ended')),
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                CHECK (member_user_id != advisor_user_id)
            )""",
    r"""CREATE TABLE medical_app_platform."memberships" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('pending', 'active', 'expired', 'cancelled')),
                starts_at TEXT NOT NULL,
                ends_at TEXT,
                benefits_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                CHECK (length(trim(plan_code)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."visit_tasks" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                member_user_id BIGINT NOT NULL,
                advisor_user_id BIGINT,
                title TEXT NOT NULL,
                scheduled_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE SET NULL,
                CHECK (length(trim(title)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."visit_records" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                task_id BIGINT,
                member_user_id BIGINT NOT NULL,
                advisor_user_id BIGINT,
                visited_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES medical_app_platform."visit_tasks"(id) ON DELETE SET NULL,
                FOREIGN KEY (member_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE SET NULL,
                CHECK (length(trim(summary)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."user_files" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                relative_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                media_type TEXT,
                size_bytes BIGINT NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
                sha256 TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                CHECK (length(trim(relative_path)) > 0),
                CHECK (substr(relative_path, 1, 1) NOT IN ('/', '\')),
                CHECK (strpos(relative_path, '..') = 0)
            )""",
    r"""CREATE TABLE medical_app_platform."medical_reports" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                file_id BIGINT,
                report_type TEXT NOT NULL DEFAULT 'other',
                report_date TEXT,
                institution TEXT,
                summary TEXT,
                status TEXT NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('draft', 'confirmed', 'archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES medical_app_platform."user_files"(id) ON DELETE SET NULL
            )""",
    r"""CREATE TABLE medical_app_platform."report_items" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                report_id BIGINT NOT NULL,
                item_name TEXT NOT NULL,
                result_value TEXT,
                unit TEXT,
                reference_range TEXT,
                flag TEXT CHECK (flag IS NULL OR flag IN ('low', 'normal', 'high', 'unknown')),
                created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed'
                CHECK (status IN ('draft', 'confirmed')), updated_at TEXT NOT NULL
                DEFAULT '1970-01-01T00:00:00.000000Z',
                FOREIGN KEY (report_id) REFERENCES medical_app_platform."medical_reports"(id) ON DELETE CASCADE,
                CHECK (length(trim(item_name)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."ai_insights" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                insight_type TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence_json TEXT,
                provider TEXT,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'shown', 'accepted', 'dismissed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                CHECK (length(trim(content)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."advisor_summaries" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                member_user_id BIGINT NOT NULL,
                advisor_user_id BIGINT,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual', 'ai_draft', 'ai_confirmed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE SET NULL,
                CHECK (period_start <= period_end),
                CHECK (length(trim(content)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."app_settings" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT,
                setting_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE
            )""",
    r"""CREATE TABLE medical_app_platform."audit_logs" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                actor_user_id BIGINT,
                action TEXT NOT NULL,
                entity_type TEXT,
                entity_id BIGINT,
                details_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (actor_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE SET NULL,
                CHECK (length(trim(action)) > 0)
            )""",
    r"""CREATE TABLE medical_app_platform."products" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                brand TEXT,
                specification TEXT,
                unit TEXT,
                image_path TEXT,
                description TEXT,
                price_cents BIGINT NOT NULL CHECK (price_cents >= 0),
                source_type TEXT NOT NULL DEFAULT 'self_operated'
                    CHECK (source_type IN ('self_operated', 'third_party')),
                is_active BIGINT NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
                created_by_user_id BIGINT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (created_by_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE SET NULL,
                CHECK (length(trim(sku)) > 0),
                CHECK (length(trim(name)) > 0),
                CHECK (length(trim(category)) > 0),
                CHECK (image_path IS NULL OR (
                    length(trim(image_path)) > 0
                    AND substr(image_path, 1, 1) NOT IN ('/', '\')
                    AND strpos(image_path, '..') = 0
                ))
            )""",
    r"""CREATE TABLE medical_app_platform."product_recommendations" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                product_id BIGINT NOT NULL,
                member_user_id BIGINT NOT NULL,
                advisor_user_id BIGINT,
                reason TEXT NOT NULL,
                recommendation_source TEXT NOT NULL DEFAULT 'advisor'
                    CHECK (recommendation_source IN ('advisor', 'catalogue')),
                status TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'viewed', 'interested')),
                viewed_at TEXT,
                interested_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES medical_app_platform."products"(id) ON DELETE RESTRICT,
                FOREIGN KEY (member_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE SET NULL,
                CHECK (length(trim(reason)) > 0),
                CHECK (
                    (recommendation_source = 'advisor' AND advisor_user_id IS NOT NULL)
                    OR (recommendation_source = 'catalogue' AND advisor_user_id IS NULL)
                ),
                CHECK (status != 'viewed' OR viewed_at IS NOT NULL),
                CHECK (status != 'interested' OR interested_at IS NOT NULL)
            )""",
    r"""CREATE TABLE medical_app_platform."orders" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                order_no TEXT NOT NULL UNIQUE,
                member_user_id BIGINT NOT NULL,
                product_id BIGINT NOT NULL,
                recommendation_id BIGINT,
                reordered_from_order_id BIGINT,
                product_name_snapshot TEXT NOT NULL,
                quantity BIGINT NOT NULL CHECK (quantity > 0),
                unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0),
                total_amount_cents BIGINT NOT NULL CHECK (
                    total_amount_cents >= 0
                    AND total_amount_cents = unit_price_cents * quantity
                ),
                currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
                status TEXT NOT NULL DEFAULT 'created'
                    CHECK (status IN ('created', 'delivered')),
                delivered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES medical_app_platform."users"(id) ON DELETE RESTRICT,
                FOREIGN KEY (product_id) REFERENCES medical_app_platform."products"(id) ON DELETE RESTRICT,
                FOREIGN KEY (recommendation_id)
                    REFERENCES medical_app_platform."product_recommendations"(id) ON DELETE SET NULL,
                FOREIGN KEY (reordered_from_order_id) REFERENCES medical_app_platform."orders"(id) ON DELETE SET NULL,
                CHECK (length(trim(order_no)) > 0),
                CHECK (length(trim(product_name_snapshot)) > 0),
                CHECK (status != 'delivered' OR delivered_at IS NOT NULL)
            )""",
    r"""CREATE TABLE medical_app_platform."user_file_contents" (
                file_id BIGINT PRIMARY KEY,
                content BYTEA NOT NULL,
                mime_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES medical_app_platform."user_files"(id) ON DELETE CASCADE,
                CHECK (length(content) > 0 AND length(content) <= 15728640),
                CHECK (length(trim(mime_type)) > 0),
                CHECK (length(sha256) = 64 AND sha256 = lower(sha256))
            )""",
    r"""CREATE TABLE medical_app_platform."conversations" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES medical_app_platform."users"(id) ON DELETE CASCADE
            )""",
    r"""CREATE TABLE medical_app_platform."messages" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                conversation_id BIGINT NOT NULL,
                sequence_no BIGINT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'error')),
                error_message TEXT,
                provider TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id)
                    REFERENCES medical_app_platform."conversations"(id) ON DELETE CASCADE,
                UNIQUE (conversation_id, sequence_no),
                CHECK (status != 'pending' OR role = 'assistant')
            )""",
    r"""CREATE TABLE medical_app_platform."chat_attachments" (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                message_id BIGINT NOT NULL,
                original_name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                content BYTEA NOT NULL,
                extracted_text TEXT NOT NULL DEFAULT '',
                extraction_method TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (message_id) REFERENCES medical_app_platform."messages"(id) ON DELETE CASCADE,
                CHECK (length(content) > 0 AND length(content) <= 10485760),
                CHECK (length(original_name) BETWEEN 1 AND 255),
                CHECK (length(extracted_text) <= 50000),
                CHECK (length(sha256) = 64)
            )""",
)

INDEX_DDL = (
    r"""CREATE INDEX idx_users_role_status ON medical_app_platform."users"(role_code, account_status)""",
    r"""CREATE INDEX idx_profile_facts_user_status
            ON medical_app_platform."profile_facts"(user_id, status, fact_key)
            """,
    r"""CREATE INDEX idx_life_records_user_date
            ON medical_app_platform."life_records"(user_id, local_date DESC, occurred_at DESC)
            """,
    r"""CREATE INDEX idx_life_records_user_category
            ON medical_app_platform."life_records"(user_id, category, occurred_at DESC)
            """,
    r"""CREATE INDEX idx_reminders_user_status_time
            ON medical_app_platform."reminders"(user_id, status, scheduled_at)
            """,
    r"""CREATE UNIQUE INDEX idx_active_advisor_binding
            ON medical_app_platform."advisor_bindings"(member_user_id) WHERE status = 'active'
            """,
    r"""CREATE INDEX idx_advisor_bindings_advisor_status
            ON medical_app_platform."advisor_bindings"(advisor_user_id, status)
            """,
    r"""CREATE INDEX idx_memberships_user_status
            ON medical_app_platform."memberships"(user_id, status, ends_at)
            """,
    r"""CREATE INDEX idx_visit_tasks_member_status
            ON medical_app_platform."visit_tasks"(member_user_id, status, scheduled_at)
            """,
    r"""CREATE INDEX idx_visit_tasks_advisor_status
            ON medical_app_platform."visit_tasks"(advisor_user_id, status, scheduled_at)
            """,
    r"""CREATE INDEX idx_visit_records_member_date
            ON medical_app_platform."visit_records"(member_user_id, visited_at DESC)
            """,
    r"""CREATE INDEX idx_user_files_user_date
            ON medical_app_platform."user_files"(user_id, created_at DESC)
            """,
    r"""CREATE INDEX idx_medical_reports_user_date
            ON medical_app_platform."medical_reports"(user_id, report_date DESC)
            """,
    r"""CREATE INDEX idx_report_items_report ON medical_app_platform."report_items"(report_id)""",
    r"""CREATE INDEX idx_ai_insights_user_status
            ON medical_app_platform."ai_insights"(user_id, status, created_at DESC)
            """,
    r"""CREATE INDEX idx_advisor_summaries_member_period
            ON medical_app_platform."advisor_summaries"(member_user_id, period_end DESC)
            """,
    r"""CREATE UNIQUE INDEX idx_app_settings_scope_key
            ON medical_app_platform."app_settings"(COALESCE(user_id, 0), setting_key)
            """,
    r"""CREATE INDEX idx_audit_logs_actor_date
            ON medical_app_platform."audit_logs"(actor_user_id, created_at DESC)
            """,
    r"""CREATE INDEX idx_messages_conversation_created
            ON medical_app_platform."messages"(conversation_id, created_at)
            """,
    r"""CREATE INDEX idx_products_active_category
            ON medical_app_platform."products"(is_active, category, name)
            """,
    r"""CREATE INDEX idx_recommendations_member_status
            ON medical_app_platform."product_recommendations"(member_user_id, status, created_at DESC)
            """,
    r"""CREATE INDEX idx_recommendations_advisor_date
            ON medical_app_platform."product_recommendations"(advisor_user_id, created_at DESC)
            """,
    r"""CREATE INDEX idx_recommendations_product
            ON medical_app_platform."product_recommendations"(product_id)
            """,
    r"""CREATE UNIQUE INDEX idx_catalogue_interest_member_product
            ON medical_app_platform."product_recommendations"(member_user_id, product_id)
            WHERE recommendation_source = 'catalogue'
            """,
    r"""CREATE INDEX idx_orders_member_date
            ON medical_app_platform."orders"(member_user_id, created_at DESC)
            """,
    r"""CREATE INDEX idx_orders_status_date
            ON medical_app_platform."orders"(status, created_at)
            """,
    r"""CREATE INDEX idx_orders_product ON medical_app_platform."orders"(product_id)""",
    r"""CREATE INDEX idx_user_file_contents_sha256
            ON medical_app_platform."user_file_contents"(sha256)
            """,
    r"""CREATE INDEX idx_conversations_user_updated ON medical_app_platform."conversations"(user_id, updated_at DESC, id DESC)""",
    r"""CREATE INDEX idx_chat_attachments_message ON medical_app_platform."chat_attachments"(message_id, id)""",
)

PLATFORM_COLUMNS = {
    **BUSINESS_COLUMNS,
    "platform_sessions": ("token_digest", "user_id", "created_at", "expires_at", "revoked_at"),
    "auth_rate_buckets": ("bucket_id", "window_start", "attempts"),
    "rpc_requests": ("user_id", "request_id", "request_digest", "response_json", "created_at"),
}
PLATFORM_TABLES = tuple(PLATFORM_COLUMNS)

TABLE_DDL += (
    """CREATE TABLE medical_app_platform.platform_sessions (
        token_digest TEXT PRIMARY KEY CHECK (token_digest ~ '^[0-9a-f]{64}$'),
        user_id BIGINT NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        CHECK (created_at < expires_at)
    )""",
    """CREATE TABLE medical_app_platform.auth_rate_buckets (
        bucket_id BIGINT PRIMARY KEY CHECK (bucket_id BETWEEN 0 AND 16384),
        window_start BIGINT NOT NULL CHECK (window_start >= 0),
        attempts BIGINT NOT NULL CHECK (attempts BETWEEN 0 AND 1000000)
    )""",
    """CREATE TABLE medical_app_platform.rpc_requests (
        user_id BIGINT NOT NULL REFERENCES medical_app_platform.users(id) ON DELETE CASCADE,
        request_id UUID NOT NULL,
        request_digest TEXT NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
        response_json TEXT NOT NULL CHECK (octet_length(response_json) <= 131072),
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, request_id)
    )""",
)

INDEX_DDL += (
    "CREATE INDEX idx_platform_sessions_user_expiry ON medical_app_platform.platform_sessions(user_id, expires_at)",
    "CREATE INDEX idx_auth_rate_window ON medical_app_platform.auth_rate_buckets(window_start)",
    "CREATE INDEX idx_rpc_created ON medical_app_platform.rpc_requests(created_at)",
    # Explicit indexes for foreign-key maintenance not covered by leading columns
    # in the desktop indexes. These also help correlated RLS existence checks.
    "CREATE INDEX idx_visit_records_task ON medical_app_platform.visit_records(task_id)",
    "CREATE INDEX idx_visit_records_advisor ON medical_app_platform.visit_records(advisor_user_id)",
    "CREATE INDEX idx_medical_reports_file ON medical_app_platform.medical_reports(file_id)",
    "CREATE INDEX idx_summaries_advisor ON medical_app_platform.advisor_summaries(advisor_user_id)",
    "CREATE INDEX idx_products_creator ON medical_app_platform.products(created_by_user_id)",
    "CREATE INDEX idx_orders_recommendation ON medical_app_platform.orders(recommendation_id)",
    "CREATE INDEX idx_orders_reordered ON medical_app_platform.orders(reordered_from_order_id)",
)

# Every identity component is installed by trusted authentication code with
# set_config(..., true). Clients do not submit these values through RPC.
ACTOR_ID = "NULLIF(current_setting('medical_app.user_id', true), '')::bigint"
ACTOR_ROLE = "current_setting('medical_app.role_code', true)"
AUTH_USERNAME = "NULLIF(current_setting('medical_app.auth_username', true), '')"
TOKEN_DIGEST = "NULLIF(current_setting('medical_app.token_digest', true), '')"
REGISTERING = "current_setting('medical_app.registering', true) = 'true'"
RUNTIME = f"current_user = '{PLATFORM_RUNTIME_ROLE}'"
OPERATOR = f"({ACTOR_ID} IS NOT NULL AND {ACTOR_ROLE} = 'operator')"
SIGNED_IN = f"({ACTOR_ID} IS NOT NULL AND {ACTOR_ROLE} IN ('member', 'advisor', 'operator'))"


def _bound(member_expression: str) -> str:
    # This policy dependency is deliberately one-way: bindings never SELECT
    # users, preventing users -> bindings -> users RLS recursion.
    return (
        f"({ACTOR_ROLE} = 'advisor' AND EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.advisor_bindings b "
        f"WHERE b.member_user_id = {member_expression} AND b.advisor_user_id = {ACTOR_ID} "
        "AND b.status = 'active'))"
    )


def _owned(column: str, *, advisors: bool = False) -> str:
    terms = [f"{column} = {ACTOR_ID}", OPERATOR]
    if advisors:
        terms.append(_bound(column))
    return "(" + " OR ".join(terms) + ")"


def _registration_user(column: str) -> str:
    return (
        f"({REGISTERING} AND EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.users reg "
        f"WHERE reg.id = {column} AND reg.username_normalized = {AUTH_USERNAME} "
        "AND reg.role_code = 'member'))"
    )


def policy_rules() -> dict[str, dict[str, tuple[str, str | None]]]:
    """Explicit per-table operation predicates, never a blanket authenticated rule.

    Values are (USING, WITH CHECK); INSERT only uses WITH CHECK. No DELETE policy
    exists for identities, audit history, sessions or the immutable RPC cache.
    Runtime cannot mutate administrative roles using the ordinary self predicate.
    The API's method/column authorization remains mandatory above these row checks.
    """
    rules: dict[str, dict[str, tuple[str, str | None]]] = {}

    def access(table: str, read: str, write: str, *, delete: bool = True):
        rules[table] = {
            "SELECT": (read, None),
            "INSERT": ("", write),
            "UPDATE": (write, write),
        }
        if delete:
            rules[table]["DELETE"] = (write, None)

    users_read = (
        f"(id = {ACTOR_ID} OR {OPERATOR} OR username_normalized = {AUTH_USERNAME} "
        f"OR {_bound('users.id')} OR ({ACTOR_ROLE} = 'member' AND EXISTS "
        f"(SELECT 1 FROM {PLATFORM_SCHEMA}.advisor_bindings b WHERE b.member_user_id = {ACTOR_ID} "
        "AND b.advisor_user_id = users.id AND b.status = 'active')))"
    )
    rules["users"] = {
        "SELECT": (users_read, None),
        "INSERT": (
            "",
            f"({OPERATOR} OR ({REGISTERING} AND username_normalized = {AUTH_USERNAME} "
            "AND role_code = 'member' AND account_status = 'active'))",
        ),
        "UPDATE": (
            f"(id = {ACTOR_ID} OR {OPERATOR})",
            f"({OPERATOR} OR (id = {ACTOR_ID} AND role_code = {ACTOR_ROLE} "
            "AND account_status = 'active'))",
        ),
    }
    bindings_read = f"({OPERATOR} OR member_user_id = {ACTOR_ID} OR advisor_user_id = {ACTOR_ID})"
    access("advisor_bindings", bindings_read, OPERATOR)
    advisor_read = (
        f"({_owned('advisor_profiles.user_id')} OR EXISTS "
        f"(SELECT 1 FROM {PLATFORM_SCHEMA}.advisor_bindings b "
        f"WHERE b.member_user_id = {ACTOR_ID} AND b.advisor_user_id = advisor_profiles.user_id "
        "AND b.status = 'active'))"
    )
    access("advisor_profiles", advisor_read, OPERATOR)
    conversation_owner = (
        f"({_owned('conversations.user_id')} OR {_registration_user('conversations.user_id')})"
    )
    access("conversations", conversation_owner, conversation_owner)
    message_owner = (
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.conversations c "
        f"WHERE c.id = messages.conversation_id AND {_owned('c.user_id')})"
    )
    access("messages", message_owner, message_owner)
    attachment_owner = (
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.messages m JOIN {PLATFORM_SCHEMA}.conversations c "
        f"ON c.id = m.conversation_id WHERE m.id = chat_attachments.message_id AND {_owned('c.user_id')})"
    )
    access("chat_attachments", attachment_owner, attachment_owner)
    for table in (
        "member_profiles",
        "profile_facts",
        "life_records",
        "user_files",
        "medical_reports",
    ):
        write = _owned(f"{table}.user_id")
        if table == "member_profiles":
            write = f"({write} OR {_registration_user('member_profiles.user_id')})"
        access(table, _owned(f"{table}.user_id", advisors=True), write)
    # A bound advisor can create a member reminder as part of visit completion,
    # decrement visit benefits, and persist a validated AI advisor draft.
    for table in ("reminders", "ai_insights"):
        access(
            table,
            _owned(f"{table}.user_id", advisors=True),
            _owned(f"{table}.user_id", advisors=True),
        )
    access(
        "memberships",
        _owned("memberships.user_id", advisors=True),
        f"({OPERATOR} OR {_bound('memberships.user_id')})",
    )
    for table in ("visit_tasks", "visit_records", "advisor_summaries"):
        read = _owned(f"{table}.member_user_id", advisors=True)
        write = (
            f"({OPERATOR} OR ({table}.advisor_user_id = {ACTOR_ID} "
            f"AND {_bound(table + '.member_user_id')}))"
        )
        access(table, read, write)
    file_read = (
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.user_files f "
        f"WHERE f.id = user_file_contents.file_id AND {_owned('f.user_id', advisors=True)})"
    )
    file_write = (
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.user_files f "
        f"WHERE f.id = user_file_contents.file_id AND {_owned('f.user_id')})"
    )
    access("user_file_contents", file_read, file_write)
    report_read = (
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.medical_reports r "
        f"WHERE r.id = report_items.report_id AND {_owned('r.user_id', advisors=True)})"
    )
    report_write = (
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.medical_reports r "
        f"WHERE r.id = report_items.report_id AND {_owned('r.user_id')})"
    )
    access("report_items", report_read, report_write)
    # An unpublished product must remain visible through the member's historical
    # recommendations/orders. The catalogue service itself filters active rows.
    product_history = (
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.product_recommendations pr "
        "WHERE pr.product_id = products.id) OR "
        f"EXISTS (SELECT 1 FROM {PLATFORM_SCHEMA}.orders po WHERE po.product_id = products.id)"
    )
    access(
        "products",
        f"({OPERATOR} OR ({SIGNED_IN} AND (is_active = 1 OR {product_history})))",
        OPERATOR,
    )
    access(
        "product_recommendations",
        _owned("product_recommendations.member_user_id", advisors=True),
        _owned("product_recommendations.member_user_id", advisors=True),
    )
    access(
        "orders", _owned("orders.member_user_id", advisors=True), _owned("orders.member_user_id")
    )
    access(
        "app_settings",
        f"({_owned('app_settings.user_id')} OR (user_id IS NULL AND {SIGNED_IN}))",
        f"(user_id = {ACTOR_ID} OR {OPERATOR})",
    )
    rules["audit_logs"] = {
        "SELECT": (_owned("audit_logs.actor_user_id"), None),
        "INSERT": ("", f"(actor_user_id = {ACTOR_ID} OR {OPERATOR})"),
    }
    rules["platform_sessions"] = {
        "SELECT": (
            f"(token_digest = {TOKEN_DIGEST} OR {_owned('platform_sessions.user_id')})",
            None,
        ),
        "INSERT": ("", f"user_id = {ACTOR_ID}"),
        "UPDATE": (f"user_id = {ACTOR_ID}", f"user_id = {ACTOR_ID}"),
    }
    access("auth_rate_buckets", "true", "true")
    rules["rpc_requests"] = {
        "SELECT": (f"user_id = {ACTOR_ID}", None),
        "INSERT": ("", f"user_id = {ACTOR_ID}"),
    }
    assert set(rules) == set(PLATFORM_TABLES)
    return rules


def policy_ddl() -> tuple[str, ...]:
    statements = []
    for table, commands in policy_rules().items():
        qualified = f"{PLATFORM_SCHEMA}.{table}"
        statements.extend(
            (
                f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY",
                f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY",
            )
        )
        for command, (using, check) in commands.items():
            # TO PUBLIC does not grant any SQL privileges. The role check keeps
            # policies inert before separately-provisioned runtime credentials.
            sql = f"CREATE POLICY platform_{command.lower()} ON {qualified} FOR {command} TO PUBLIC"
            if command != "INSERT":
                sql += f" USING ({RUNTIME} AND ({using}))"
            if check is not None:
                sql += f" WITH CHECK ({RUNTIME} AND ({check}))"
            statements.append(sql)
    return tuple(statements)


def compatibility_constraints() -> tuple[str, ...]:
    """Validate legacy text encodings without changing the Python contract."""
    statements = []
    utc_pattern = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
    date_pattern = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    dates = {"birth_date", "local_date", "report_date", "period_start", "period_end"}
    for table, columns in PLATFORM_COLUMNS.items():
        for column in columns:
            check = None
            if column.endswith("_at") or column == "paused_until":
                check = f"{column} ~ '{utc_pattern}' AND {column}::timestamptz IS NOT NULL"
            elif column in dates:
                check = f"{column} ~ '{date_pattern}' AND {column}::date IS NOT NULL"
            elif column.endswith("_json"):
                check = f"{column} IS JSON"
            if check is not None:
                statements.append(
                    f"ALTER TABLE {PLATFORM_SCHEMA}.{table} ADD CONSTRAINT "
                    f"ck_{table}_{column}_encoding CHECK ({column} IS NULL OR ({check}))"
                )
    return tuple(statements)


def revoke_ddl() -> str:
    """No Supabase browser/API role may use the private schema or its objects."""
    return f"""
REVOKE ALL ON SCHEMA {PLATFORM_SCHEMA} FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA {PLATFORM_SCHEMA} FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA {PLATFORM_SCHEMA} FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA {PLATFORM_SCHEMA} REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA {PLATFORM_SCHEMA} REVOKE ALL ON SEQUENCES FROM PUBLIC;
DO $platform_revoke$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA {PLATFORM_SCHEMA} FROM %I', role_name);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA {PLATFORM_SCHEMA} FROM %I', role_name);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {PLATFORM_SCHEMA} FROM %I', role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA {PLATFORM_SCHEMA} REVOKE ALL ON TABLES FROM %I', role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA {PLATFORM_SCHEMA} REVOKE ALL ON SEQUENCES FROM %I', role_name);
        END IF;
    END LOOP;
END;
$platform_revoke$;
"""


def runtime_grant_ddl() -> tuple[str, ...]:
    """Only the provisioning workflow uses these statements; no role is adopted."""
    statements = [
        f"GRANT USAGE ON SCHEMA {PLATFORM_SCHEMA} TO {PLATFORM_RUNTIME_ROLE}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {PLATFORM_SCHEMA} TO {PLATFORM_RUNTIME_ROLE}",
    ]
    for table, commands in policy_rules().items():
        statements.append(
            f"GRANT {', '.join(commands)} ON TABLE {PLATFORM_SCHEMA}.{table} TO {PLATFORM_RUNTIME_ROLE}"
        )
    return tuple(statements)
