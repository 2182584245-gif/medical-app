from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import func, select

from server.models import Conversation, Message


def test_same_request_identifier_cannot_alias_another_users_conversation(client, account):
    _, first_headers = account(client, "review-first-user")
    _, second_headers = account(client, "review-second-user")
    payload = {"request_id": str(uuid4()), "title": "Private synthetic conversation"}
    first = client.post("/conversations", headers=first_headers, json=payload)
    second = client.post("/conversations", headers=second_headers, json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    first_id = first.json()["id"]
    message = {"request_id": str(uuid4()), "role": "user", "content": "First user's content"}
    assert (
        client.post(
            f"/conversations/{first_id}/messages", headers=first_headers, json=message
        ).status_code
        == 201
    )
    assert (
        client.get(f"/conversations/{first_id}/messages", headers=second_headers).status_code == 404
    )
    assert (
        client.post(
            f"/conversations/{first_id}/messages", headers=second_headers, json=message
        ).status_code
        == 404
    )
    assert client.get("/conversations", headers=second_headers).json() == [second.json()]


def test_parallel_distinct_messages_allocate_one_contiguous_sequence_each(client, app, account):
    _, headers = account(client, "review-parallel-user")
    conversation = client.post(
        "/conversations",
        headers=headers,
        json={"request_id": str(uuid4()), "title": "Concurrent synthetic conversation"},
    ).json()
    path = f"/conversations/{conversation['id']}/messages"
    payloads = [
        {"request_id": str(uuid4()), "role": "user", "content": f"Synthetic message {index}"}
        for index in range(6)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(
            pool.map(lambda payload: client.post(path, headers=headers, json=payload), payloads)
        )
    assert [response.status_code for response in responses] == [201] * 6
    assert sorted(response.json()["sequence_no"] for response in responses) == list(range(1, 7))
    assert len({response.json()["id"] for response in responses}) == 6
    assert {response.json()["content"] for response in responses} == {
        payload["content"] for payload in payloads
    }
    with app.state.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 6
        assert db.scalar(select(Conversation.next_sequence)) == 7


def test_concurrent_conflicting_retries_never_replace_committed_message(client, app, account):
    _, headers = account(client, "review-conflict-user")
    conversation = client.post(
        "/conversations",
        headers=headers,
        json={"request_id": str(uuid4()), "title": "Conflicting synthetic retries"},
    ).json()
    path = f"/conversations/{conversation['id']}/messages"
    request_id = str(uuid4())
    payloads = [
        {"request_id": request_id, "role": "user", "content": f"Candidate {index}"}
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda payload: client.post(path, headers=headers, json=payload), payloads)
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response.json() for response in responses if response.status_code == 201)
    listed = client.get(path, headers=headers).json()
    assert listed == [winner]
    with app.state.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 1
        assert db.scalar(select(Conversation.next_sequence)) == 2


def test_revoked_token_cannot_write_and_cannot_fall_back_to_claimed_identity(client, account):
    user, headers = account(client, "review-revocation-user")
    conversation = client.post(
        "/conversations",
        headers=headers,
        json={"request_id": str(uuid4()), "title": "Synthetic revocation test"},
    ).json()
    assert client.post("/auth/logout", headers=headers).status_code == 204
    payload = {"request_id": str(uuid4()), "role": "user", "content": "Must not be stored"}
    response = client.post(
        f"/conversations/{conversation['id']}/messages?user_id={user['id']}",
        headers={**headers, "X-User-ID": user["id"]},
        json=payload,
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}
