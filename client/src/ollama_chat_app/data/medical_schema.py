"""Schema 7: preserve existing facts while widening only the category CHECK."""

from __future__ import annotations

import re
import sqlite3

OLD_CATEGORIES = "('diet', 'water', 'activity', 'sleep', 'environment')"
NEW_CATEGORIES = "('diet', 'water', 'activity', 'sleep', 'environment', 'medical')"


def migrate_medical(connection: sqlite3.Connection) -> None:
    """Called in Database.initialize's existing transaction with FK checking after.

    SQLite cannot ALTER a CHECK. Replace this one table transactionally; never
    rebuild the database, drop data, edit sqlite_schema or reset any user IDs.
    Unknown table customizations fail safely and leave the original untouched.
    """
    schema = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='life_records'"
    ).fetchone()
    if not schema or schema[0].count(OLD_CATEGORIES) != 1:
        raise sqlite3.DatabaseError("记录表分类约束不是预期旧版；已保留原库，需人工审核")
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='life_records_v7_new' OR "
        "(type='trigger' AND tbl_name='life_records')"
    ).fetchone():
        raise sqlite3.DatabaseError("记录表有未审核对象；拒绝自动升级并保留原数据")
    indexes = [
        row[0]
        for row in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name='life_records' "
            "AND sql IS NOT NULL ORDER BY name"
        )
    ]
    statement, count = re.subn(
        r'CREATE TABLE(?: IF NOT EXISTS)?\s+["\[]?life_records["\]]?',
        "CREATE TABLE life_records_v7_new",
        schema[0],
        count=1,
        flags=re.I,
    )
    if count != 1:
        raise sqlite3.DatabaseError("记录表定义不兼容；未修改原数据")
    connection.execute(statement.replace(OLD_CATEGORIES, NEW_CATEGORIES))
    connection.execute("INSERT INTO life_records_v7_new SELECT * FROM life_records")
    for first, second in (
        ("life_records", "life_records_v7_new"),
        ("life_records_v7_new", "life_records"),
    ):
        if connection.execute(f"SELECT * FROM {first} EXCEPT SELECT * FROM {second}").fetchone():
            raise sqlite3.DatabaseError("记录升级逐行校验失败，整笔回滚")
    connection.execute("DROP TABLE life_records")
    connection.execute("ALTER TABLE life_records_v7_new RENAME TO life_records")
    for statement in indexes:
        connection.execute(statement)
