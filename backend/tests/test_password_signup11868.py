from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from expansion.schema import initialize_sqlite as init_expansion
from expansion.sqlite_testing import AtomicSqliteDatabase
from ollama_chat_app.data.database import Database, timestamp_to_db, utc_now
from ollama_chat_app.services.auth import AuthService
from server.phone_auth import PhoneAuth, create_phone_auth_router
from server.phone_auth_schema import initialize_sqlite
from server.platform_auth import PlatformAuth, create_auth_router
from server.platform_security import PlatformAuthSettings

PASSWORD = 'IsolatedFixture8!Test'


class Sender:
    code = None

    def send(self, phone, code, **kwargs):
        self.code = code
        return {'accepted': True}


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setenv('FAWNSEE_AUTH_PHONE_REQUIRED', 'true')
    raw = Database(tmp_path / 'test.sqlite')
    raw.initialize()
    db = AtomicSqliteDatabase(raw)
    with db.transaction() as c:
        c.execute('CREATE TABLE platform_sessions(token_digest TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,created_at TEXT,expires_at TEXT,revoked_at TEXT)')
        c.execute('CREATE TABLE auth_rate_buckets(bucket_id INTEGER PRIMARY KEY,window_start INTEGER,attempts INTEGER)')
    initialize_sqlite(db)
    init_expansion(db)
    settings = PlatformAuthSettings(token_pepper=SecretStr(bytes(range(32)).hex()), production=False,
                                    auth_ip_limit=100, auth_username_limit=100, auth_global_limit=1000)
    auth = PlatformAuth(db, settings)
    sender = Sender()
    phone = PhoneAuth(auth, sender=sender)
    app = FastAPI()
    app.include_router(create_auth_router(auth))
    app.include_router(create_phone_auth_router(phone))
    with TestClient(app) as http:
        yield SimpleNamespace(http=http, db=db, phone=phone, sender=sender)


def post(e, path, **body):
    return e.http.post('/v1/auth/' + path, json=body)


def test_signup_login_refresh_and_isolation(sample):
    e = sample
    users = []
    for name in ('User-A', 'User-B'):
        created = post(e, 'register', username=name, password=PASSWORD)
        assert created.status_code == 201, created.text
        user = created.json()
        assert user['role_code'] == 'member' and user['account_status'] == 'active'
        users.append(user['id'])
        login = post(e, 'login', username=' '+name.lower()+' ', password=PASSWORD)
        assert login.status_code == 200, login.text
        tokens = login.json()
        headers = {'Authorization': 'Bearer '+tokens['access_token']}
        assert e.http.get('/v1/auth/me',headers=headers).json()['id'] == user['id']
        assert e.http.get('/v1/auth/phone',headers=headers).json()['phone'] is None
        refreshed = post(e,'refresh',refresh_token=tokens['refresh_token'],request_id=str(uuid4()))
        assert refreshed.status_code == 200 and refreshed.json()['user']['id'] == user['id']
    assert users[0] != users[1]
    with e.db.connect() as c:
        assert c.execute('SELECT count(*) FROM phone_auth_bindings').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(DISTINCT user_id) FROM conversations').fetchone()[0] == 2
    assert e.sender.code is None


def test_normalized_duplicates_concurrency_and_policy(sample):
    e = sample
    def register(name):
        return post(e,'register',username=name,password=PASSWORD).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(register,['Ａｌｐｈａ', ' alpha '])) == [201,409]
    assert post(e,'register',username='x',password='short').status_code == 422
    assert post(e,'register',username=' ',password=PASSWORD).status_code == 422
    assert post(e,'register',username='__FAWNSEE_PHONE_test',password=PASSWORD).status_code == 409
    assert post(e,'register',username='adminattempt',password=PASSWORD,role_code='operator').status_code == 422
    assert post(e,'login',username='alpha',password='unmatchedPass').status_code == 401


def test_prior_bound_member_and_legacy_unbound(sample):
    e=sample
    challenge=e.phone.send('13800138000','register',client_ip='isolated')
    bound=e.phone.register_complete('oldphoneuser',PASSWORD,'13800138000',challenge['challenge_id'],e.sender.code,client_ip='isolated')
    assert post(e,'login',username='oldphoneuser',password=PASSWORD).json()['user']['id']==bound['user']['id']
    for role in ('member','operator'):
        username='oldunbound'+role
        display, normalized, digest=AuthService._prepare_new_account(username,PASSWORD)
        with e.db.transaction() as c:
            AuthService._insert_account(c,display_username=display,normalized_username=normalized,password_hash=digest,role_code=role,now=timestamp_to_db(utc_now()))
        assert post(e,'login',username=username,password=PASSWORD).status_code==401
    assert post(e,'register',username='oldphoneuser',password=PASSWORD).status_code==409
