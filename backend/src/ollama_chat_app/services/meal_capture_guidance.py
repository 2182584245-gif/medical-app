"""Fixed, persisted navigation guidance for the meal-recording shortcut.

This answer explains an existing UI operation. It needs no model request,
personal-memory read, web lookup, or write to a life record.
"""

MEAL_RECORD_SHORTCUT = "记录日常餐食"
MEAL_CAPTURE_GUIDE = (
    "记录日常餐食，最好试着拍照或用语音记录自己实际吃的一餐，让我帮你分析食物搭配和大致营养。\n\n"
    "拍照时把整盘菜拍进去；语音可以直接说这一餐吃了什么。进入记录饮食后，选择拍照或语音记录，"
    "就能看到分析，再由你决定是否保存。\n\n"
    "[去记录饮食](#records/diet)"
)


def meal_capture_guide(content, attachments=()):
    """Match only the deliberate shortcut, never replace an actual meal input."""
    return MEAL_CAPTURE_GUIDE if content == MEAL_RECORD_SHORTCUT and not attachments else None
