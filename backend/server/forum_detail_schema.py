"""Additive community engagement schema; explicit administrator migration only."""
from __future__ import annotations

SCHEMA = 'medical_app_platform'
ROLE = 'medical_app_platform_runtime'
TABLES = ('forum_bookmarks', 'forum_follows', 'forum_comment_requests')
SQLITE_DDL = (
    'CREATE TABLE forum_bookmarks(post_id BIGINT NOT NULL REFERENCES forum_posts(id) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,created_at TEXT NOT NULL,PRIMARY KEY(post_id,user_id))',
    'CREATE TABLE forum_follows(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,author_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,created_at TEXT NOT NULL,CHECK(user_id<>author_user_id),PRIMARY KEY(user_id,author_user_id))',
    'CREATE TABLE forum_comment_requests(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,request_id TEXT NOT NULL CHECK(length(request_id)=36),post_id BIGINT NOT NULL REFERENCES forum_posts(id) ON DELETE CASCADE,comment_id BIGINT NOT NULL REFERENCES forum_comments(id) ON DELETE CASCADE,content_digest TEXT NOT NULL CHECK(length(content_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(user_id,request_id))',
    'CREATE INDEX idx_forum_bookmark_user ON forum_bookmarks(user_id)',
    'CREATE INDEX idx_forum_follow_author ON forum_follows(author_user_id)',
)


def initialize_sqlite(database):
    if getattr(database, 'dialect', 'sqlite') == 'postgresql':
        raise RuntimeError('Community schema requires administrator migration')
    with database.transaction() as connection:
        for statement in SQLITE_DDL:
            connection.execute(statement)


def postgres_statements():
    actor = "NULLIF(current_setting('medical_app.user_id',true),'')::bigint"
    runtime = f"current_user='{ROLE}' AND current_setting('medical_app.role_code',true)='member' AND {actor}>0"
    result = []
    for sql in SQLITE_DDL:
        for name in (*TABLES, 'forum_posts', 'forum_comments', 'users'):
            sql = sql.replace('TABLE '+name+'(', 'TABLE '+SCHEMA+'.'+name+'(')
            sql = sql.replace('REFERENCES '+name+'(', 'REFERENCES '+SCHEMA+'.'+name+'(')
            sql = sql.replace(' ON '+name+'(', ' ON '+SCHEMA+'.'+name+'(')
        result.append(sql)
    for name in TABLES:
        table = SCHEMA+'.'+name
        # Public aggregate bookmark counts are intentional; follows and request
        # proofs are private to their owner. No client can pick another actor.
        read = 'TRUE' if name == 'forum_bookmarks' else f'user_id={actor}'
        result.extend((f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY',
                       f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY',
                       f'REVOKE ALL ON TABLE {table} FROM PUBLIC',
                       f'GRANT SELECT,INSERT,DELETE ON TABLE {table} TO {ROLE}',
                       f'CREATE POLICY detail_read ON {table} FOR SELECT TO {ROLE} USING ({runtime} AND ({read}))',
                       f'CREATE POLICY detail_insert ON {table} FOR INSERT TO {ROLE} WITH CHECK ({runtime} AND user_id={actor})',
                       f'CREATE POLICY detail_delete ON {table} FOR DELETE TO {ROLE} USING ({runtime} AND user_id={actor})'))
    # Deliberately returns only public name/avatar, without expanding private
    # user/preferences policies. No actor parameter or dynamic SQL.
    # Caller must be runtime + an active member, target must have visible work.
    result.extend((f"""CREATE FUNCTION {SCHEMA}.forum_public_identity(target_id BIGINT) RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,{SCHEMA}
AS $avatar$
 SELECT (SELECT json_build_object('username',target.username,'avatar_data',CASE WHEN
 length(pref.preferences_json::jsonb->>'avatar_data') BETWEEN 46 AND 350000
 AND (pref.preferences_json::jsonb->>'avatar_data') ~ '^data:image/png;base64,iVBORw0KGgo[A-Za-z0-9+/]*={{0,2}}$'
 THEN pref.preferences_json::jsonb->>'avatar_data' ELSE '' END)::text
 FROM {SCHEMA}.users target LEFT JOIN {SCHEMA}.user_preferences pref ON target.id=pref.user_id
 WHERE target.id=target_id AND target.account_status='active'
 AND session_user='{ROLE}' AND current_setting('medical_app.role_code',true)='member'
 AND EXISTS(SELECT 1 FROM {SCHEMA}.users viewer WHERE viewer.id={actor} AND viewer.role_code='member' AND viewer.account_status='active')
 AND (EXISTS(SELECT 1 FROM {SCHEMA}.forum_posts p WHERE p.author_user_id=target_id AND p.status='active')
 OR EXISTS(SELECT 1 FROM {SCHEMA}.forum_comments c JOIN {SCHEMA}.forum_posts p ON p.id=c.post_id WHERE c.author_user_id=target_id AND p.status='active'))
 )
$avatar$""",
        f'REVOKE ALL ON FUNCTION {SCHEMA}.forum_public_identity(BIGINT) FROM PUBLIC',
        f'GRANT EXECUTE ON FUNCTION {SCHEMA}.forum_public_identity(BIGINT) TO {ROLE}'))
    return tuple(result)
