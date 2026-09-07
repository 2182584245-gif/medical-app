from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from server.models import Conversation, Message


def new_conversation(client, headers, title="合成会话"):
    payload = {"title": title, "request_id": str(uuid4())}
    response = client.post("/conversations", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


def test_conversation_idempotency_rename_and_user_isolation(client, account):
    _first, headers = account(client)
    _second, other_headers = account(client, "another-member")
    conversation, payload = new_conversation(client, headers)
    retry = client.post("/conversations", headers=headers, json=payload)
    assert retry.status_code == 200
    assert retry.json() == conversation
    conflict = client.post("/conversations", headers=headers, json={**payload, "title": "不同正文"})
    assert conflict.status_code == 409
    assert client.get("/conversations", headers=other_headers).json() == []
    path = f"/conversations/{conversation['id']}"
    assert client.patch(path, headers=other_headers, json={"title": "越权改名"}).status_code == 404
    assert client.get(path + "/messages", headers=other_headers).status_code == 404
    changed = client.patch(path, headers=headers, json={"title": "新名称"})
    assert changed.status_code == 200
    assert changed.json()["title"] == "新名称"
    assert len(client.get("/conversations", headers=headers).json()) == 1


def test_message_idempotency_conflicts_and_pagination_preserve_conversation_ownership(
    client, app, account
):
    _user, headers = account(client)
    _other, other_headers = account(client, "another-member")
    first, _ = new_conversation(client, headers, "第一个")
    second, _ = new_conversation(client, headers, "第二个")
    path = f"/conversations/{first['id']}/messages"
    payload = {
        "request_id": str(uuid4()),
        "role": "user",
        "content": "  不应丢失空格🙂  ",
        "provider": "local",
        "model": "synthetic-model",
    }
    denied = client.post(path, headers=other_headers, json=payload)
    missing = client.post(f"/conversations/{uuid4()}/messages", headers=headers, json=payload)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    created = client.post(path, headers=headers, json=payload)
    assert created.status_code == 201
    assert created.json()["sequence_no"] == 1
    assert created.json()["content"] == payload["content"]
    retry = client.post(path, headers=headers, json=payload)
    assert retry.status_code == 200
    assert retry.json() == created.json()
    assert (
        client.post(path, headers=headers, json={**payload, "content": "冲突"}).status_code == 409
    )
    answer = client.post(
        path,
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "role": "assistant",
            "content": "合成回答",
        },
    )
    assert answer.json()["sequence_no"] == 2
    page = client.get(path + "?limit=1&after_sequence=1", headers=headers)
    assert page.json() == [answer.json()]
    assert client.get(f"/conversations/{second['id']}/messages", headers=headers).json() == []
    with app.state.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 2


def test_parallel_same_request_does_not_duplicate_rows_or_consume_sequence_numbers(
    client, app, account
):
    _user, headers = account(client)
    conversation, _ = new_conversation(client, headers)
    path = f"/conversations/{conversation['id']}/messages"
    payload = {"request_id": str(uuid4()), "role": "user", "content": "合成并发重试"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: client.post(path, headers=headers, json=payload), range(2))
        )
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    with app.state.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 1
        assert db.scalar(select(Conversation.next_sequence)) == 2


@pytest.mark.parametrize("operation", ["conversations", "messages", "rename"])
def test_unknown_ownership_and_ai_key_fields_are_rejected_without_secret_echo(
    client, operation, account
):
    _user, headers = account(client)
    conversation, _ = new_conversation(client, headers)
    secret = "synthetic-ai-key-must-not-be-stored"
    if operation == "conversations":
        method, path, payload = (
            "post",
            "/conversations",
            {
                "title": "无效",
                "request_id": str(uuid4()),
                "user_id": str(uuid4()),
            },
        )
    elif operation == "messages":
        method, path, payload = (
            "post",
            f"/conversations/{conversation['id']}/messages",
            {
                "role": "user",
                "content": "合成消息",
                "request_id": str(uuid4()),
                "api_key": secret,
            },
        )
    else:
        method, path, payload = (
            "patch",
            f"/conversations/{conversation['id']}",
            {
                "title": "无效",
                "user_id": str(uuid4()),
                "ai_key": secret,
            },
        )
    response = getattr(client, method)(path, headers=headers, json=payload)
    assert response.status_code == 422
    assert secret not in response.text


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001"])
def test_conversation_pagination_is_bounded(client, query, account):
    _user, headers = account(client)
    assert client.get("/conversations?" + query, headers=headers).status_code == 422
