"""Independent trusted-tool acceptance; every account belongs to a fresh test DB."""

from __future__ import annotations

import json

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.security.passwords import verify_password
from ollama_chat_app.services.auth import AuthenticationError, AuthService, RegistrationError
from ollama_chat_app.services.developer import GRANT_KEY
from tools.provision_local_developer import provision

OWNER_PASSWORD = "SyntheticOwnerOnly@1726"
MEMBER_PASSWORD = "SyntheticMemberOnly@1726"
NEW_PASSWORD = "SyntheticNewOperator@1726"


@pytest.fixture
def local_accounts(tmp_path):
    database = Database(tmp_path / "local-provision.db")
    auth = AuthService(database)
    owner = auth.bootstrap_operator("owner_fixture", OWNER_PASSWORD)
    member = auth.register("member_fixture", MEMBER_PASSWORD)
    return database, auth, owner, member


def _grants(database):
    with database.connect() as connection:
        return {
            row["user_id"]: json.loads(row["value_json"])
            for row in connection.execute(
                "SELECT user_id,value_json FROM app_settings WHERE setting_key=?", (GRANT_KEY,)
            )
        }


def _users(database):
    with database.connect() as connection:
        return {
            row["username"]: dict(row)
            for row in connection.execute("SELECT id,username,role_code,password_hash FROM users")
        }


def test_verified_existing_operator_can_receive_only_local_grant(local_accounts, capsys):
    database, _auth, owner, _member = local_accounts
    old = _users(database)
    result = provision(database, "owner_fixture", OWNER_PASSWORD)
    assert result == {
        "local_developer_granted": True,
        "username": "owner_fixture",
        "cloud_changed": False,
        "password_printed": False,
    }
    assert _grants(database) == {owner.id: {"enabled": True}}
    assert _users(database) == old
    assert OWNER_PASSWORD not in json.dumps(result)
    assert capsys.readouterr().out == ""


def test_wrong_password_cannot_grant_or_reset_existing_operator(local_accounts):
    database, _auth, _owner, _member = local_accounts
    original = _users(database)
    with pytest.raises(AuthenticationError):
        provision(database, "owner_fixture", "IncorrectPassword@1726")
    assert _grants(database) == {}
    assert _users(database) == original
    assert verify_password(_users(database)["owner_fixture"]["password_hash"], OWNER_PASSWORD)


def test_authenticated_member_cannot_gain_developer_or_operator_role(local_accounts):
    database, _auth, _owner, member = local_accounts
    before = _users(database)
    with pytest.raises(ValueError, match="普通会员"):
        provision(database, "member_fixture", MEMBER_PASSWORD)
    assert _grants(database) == {}
    assert _users(database) == before
    assert _users(database)["member_fixture"]["id"] == member.id
    assert _users(database)["member_fixture"]["role_code"] == "member"


def test_authorized_operator_can_create_new_operator_and_grant_only_target(local_accounts):
    database, auth, owner, _member = local_accounts
    original = _users(database)
    result = provision(
        database, "new_operator_fixture", NEW_PASSWORD, actor=("owner_fixture", OWNER_PASSWORD)
    )
    created = auth.authenticate("new_operator_fixture", NEW_PASSWORD)
    assert created.role_code == "operator"
    assert _grants(database) == {created.id: {"enabled": True}}
    assert owner.id not in _grants(database)
    assert set(_users(database)) == {*original, "new_operator_fixture"}
    assert all(_users(database)[name] == row for name, row in original.items())
    assert result["cloud_changed"] is False and result["password_printed"] is False


@pytest.mark.parametrize(
    "actor",
    [
        ("owner_fixture", "IncorrectPassword@1726"),
        ("member_fixture", MEMBER_PASSWORD),
    ],
)
def test_wrong_or_member_actor_cannot_create_privileged_account(local_accounts, actor):
    database, _auth, _owner, _member = local_accounts
    original = _users(database)
    with pytest.raises((AuthenticationError, ValueError)):
        provision(database, "must_not_be_created", NEW_PASSWORD, actor=actor)
    assert _users(database) == original
    assert _grants(database) == {}


def test_existing_username_never_silently_resets_password(local_accounts):
    database, auth, _owner, _member = local_accounts
    original = _users(database)
    with pytest.raises(RegistrationError):
        provision(
            database,
            "owner_fixture",
            NEW_PASSWORD,
            actor=("owner_fixture", OWNER_PASSWORD),
        )
    assert _users(database) == original
    assert _grants(database) == {}
    assert auth.authenticate("owner_fixture", OWNER_PASSWORD).role_code == "operator"
    with pytest.raises(AuthenticationError):
        auth.authenticate("owner_fixture", NEW_PASSWORD)


def test_unknown_username_is_not_an_implicit_bootstrap(local_accounts):
    database, _auth, _owner, _member = local_accounts
    original = _users(database)
    with pytest.raises(AuthenticationError):
        provision(database, "missing_fixture", NEW_PASSWORD)
    assert _users(database) == original
    assert _grants(database) == {}
