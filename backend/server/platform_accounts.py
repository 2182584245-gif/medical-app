"""Current staff terms, read inside the same transaction as protected operations."""

from ollama_chat_app.data.database import timestamp_from_db, utc_now


def advisor_term_valid(connection, user_id: int, role_code: str) -> bool:
    if role_code != "advisor":
        return True
    term = connection.execute(
        "SELECT starts_at, ends_at FROM staff_account_terms WHERE user_id = ?", (user_id,)
    ).fetchone()
    if term is None:
        return True  # Existing pre-v2 staff retain access until an operator sets terms.
    now = utc_now()
    try:
        return timestamp_from_db(term["starts_at"]) <= now < timestamp_from_db(term["ends_at"])
    except (TypeError, ValueError):
        return False
