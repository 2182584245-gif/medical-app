"""Additive reading interactions. Run only through an explicit admin migration."""
SCHEMA='medical_app_platform'
ROLE='medical_app_platform_runtime'
TABLES=('reading_engagement','reading_follows','reading_shares','reading_comments','reading_comment_likes')
SQLITE_DDL=(
 "CREATE TABLE reading_engagement(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,article_id TEXT NOT NULL,liked INTEGER NOT NULL DEFAULT 0 CHECK(liked IN(0,1)),saved INTEGER NOT NULL DEFAULT 0 CHECK(saved IN(0,1)),is_read INTEGER NOT NULL DEFAULT 0 CHECK(is_read IN(0,1)),saved_at TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,article_id))",
 "CREATE TABLE reading_follows(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,publisher_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(user_id,publisher_id))",
 "CREATE TABLE reading_shares(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,article_id TEXT NOT NULL,created_at TEXT NOT NULL)",
 "CREATE TABLE reading_comments(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,article_id TEXT NOT NULL,parent_id TEXT REFERENCES reading_comments(id) ON DELETE CASCADE,content TEXT NOT NULL,author TEXT NOT NULL,avatar_data TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)",
 "CREATE TABLE reading_comment_likes(comment_id TEXT NOT NULL REFERENCES reading_comments(id) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,created_at TEXT NOT NULL,PRIMARY KEY(comment_id,user_id))",
 'CREATE INDEX idx_reading_engagement_article ON reading_engagement(article_id)',
 'CREATE INDEX idx_reading_share_article ON reading_shares(article_id)',
 'CREATE INDEX idx_reading_comment_article ON reading_comments(article_id,parent_id,created_at,id)',
)

def initialize_sqlite(database):
    if getattr(database,'dialect','sqlite')=='postgresql':raise RuntimeError('Administrator migration required')
    with database.transaction() as conn:
        for sql in SQLITE_DDL:conn.execute(sql)

def postgres_statements():
    actor="NULLIF(current_setting('medical_app.user_id',true),'')::bigint"
    runtime=f"current_user='{ROLE}' AND current_setting('medical_app.role_code',true)='member' AND {actor}>0"
    result=[]
    for sql in SQLITE_DDL:
        for name in (*TABLES,'users'):
            for a,b in [('TABLE '+name+'(','TABLE '+SCHEMA+'.'+name+'('),('REFERENCES '+name+'(','REFERENCES '+SCHEMA+'.'+name+'('),(' ON '+name+'(',' ON '+SCHEMA+'.'+name+'(')]:sql=sql.replace(a,b)
        result.append(sql)
    for name in TABLES:
        table=SCHEMA+'.'+name
        read='TRUE' if name in {'reading_comments','reading_comment_likes'} else f'user_id={actor}'
        result.extend((f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY',f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY',
            f'REVOKE ALL ON TABLE {table} FROM PUBLIC',f'GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE {table} TO {ROLE}',
            f'CREATE POLICY reading_read ON {table} FOR SELECT TO {ROLE} USING ({runtime} AND ({read}))',
            f'CREATE POLICY reading_insert ON {table} FOR INSERT TO {ROLE} WITH CHECK ({runtime} AND user_id={actor})',
            f'CREATE POLICY reading_update ON {table} FOR UPDATE TO {ROLE} USING ({runtime} AND user_id={actor}) WITH CHECK ({runtime} AND user_id={actor})',
            f'CREATE POLICY reading_delete ON {table} FOR DELETE TO {ROLE} USING ({runtime} AND user_id={actor})'))
    # Only aggregate counts leave the owner-private engagement/share tables.
    guard=f"session_user='{ROLE}' AND current_setting('medical_app.role_code',true)='member' AND EXISTS(SELECT 1 FROM {SCHEMA}.users u WHERE u.id={actor} AND u.role_code='member' AND u.account_status='active')"
    result.extend((f"""CREATE FUNCTION {SCHEMA}.reading_public_counts(target_id TEXT) RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,{SCHEMA}
AS $reading$ SELECT CASE WHEN {guard} THEN json_build_object(
 'likes',(SELECT COUNT(*) FROM {SCHEMA}.reading_engagement WHERE article_id=target_id AND liked=1),
 'saves',(SELECT COUNT(*) FROM {SCHEMA}.expansion_content_progress WHERE content_id=target_id AND saved=1),
 'shares',(SELECT COUNT(*) FROM {SCHEMA}.reading_shares WHERE article_id=target_id),
 'comments',(SELECT COUNT(*) FROM {SCHEMA}.reading_comments WHERE article_id=target_id))::text ELSE NULL END $reading$""",
        f'REVOKE ALL ON FUNCTION {SCHEMA}.reading_public_counts(TEXT) FROM PUBLIC',
        f'GRANT EXECUTE ON FUNCTION {SCHEMA}.reading_public_counts(TEXT) TO {ROLE}'))
    return tuple(result)
