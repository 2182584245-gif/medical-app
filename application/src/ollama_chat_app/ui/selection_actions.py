"""Shared explicit row selection; destructive actions never infer unselected rows."""

from PySide6.QtCore import Qt


def selected_rows(table):
    result = []
    for index in sorted({item.row() for item in table.selectionModel().selectedRows()}):
        item = table.item(index, 0)
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(data, dict) and type(data.get("id")) is int:
            result.append(dict(data))
    return result


def deletion_prompt(items, label):
    preview = "\n".join(
        f"• {str(item.get('title') or item.get('content') or item['id'])[:70]}"
        for item in items[:6]
    )
    if len(items) > 6:
        preview += f"\n另有 {len(items) - 6} 条"
    return (
        f"确认删除已选的 {len(items)} 条{label}？\n\n{preview}\n\n"
        "删除不能撤销。任一条无权访问或已不存在时，本次全部取消。"
    )
