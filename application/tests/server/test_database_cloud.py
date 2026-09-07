from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import SecretStr

from server import database as module


def test_postgres_connection_margin_preserves_sql_timeout_and_pool_bounds(settings, monkeypatch):
    captured = {}
    engine = Mock()
    engine.dialect = SimpleNamespace(name="postgresql")

    def fake_engine(url, **kwargs):
        captured.update(kwargs)
        return engine

    monkeypatch.setattr(module, "create_engine", fake_engine)
    settings = settings.model_copy(
        update={"database_url": SecretStr("postgresql+psycopg://synthetic@db.invalid/postgres")}
    )
    database = module.Database(settings)
    assert database.engine is engine
    assert captured["connect_args"]["connect_timeout"] == 10
    assert captured["connect_args"]["options"] == "-c statement_timeout=5000"
    assert captured["pool_size"] + captured["max_overflow"] == 10
    assert captured["pool_timeout"] == 5
    assert captured["hide_parameters"] is True
