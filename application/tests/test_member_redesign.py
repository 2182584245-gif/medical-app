from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QWidget

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.commerce import CommerceService, CommerceValidationError
from ollama_chat_app.services.health import HealthService, HealthValidationError
from ollama_chat_app.services.member_statistics import daily_series
from ollama_chat_app.services.member_weather import MemberWeatherService, WeatherUnavailable
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.time_utils import beijing_now
from ollama_chat_app.ui.health_records_panel import LifeRecordDialog
from ollama_chat_app.ui.health_workspace import HealthWorkspace
from ollama_chat_app.ui.member_commerce import product_illustration
from ollama_chat_app.ui.member_today import TodayPage


@pytest.fixture
def context(tmp_path):
    db = Database(tmp_path / "synthetic-members.db")
    db.initialize()
    auth = AuthService(db)
    operator = auth.bootstrap_operator("synthetic-ops", "Synthetic-pass-123")
    member = auth.register("synthetic-member", "Synthetic-pass-123")
    other = auth.register("synthetic-other", "Synthetic-pass-123")
    commerce = CommerceService(db)
    cup = commerce.create_product(
        operator.id,
        sku="SYN-CUP",
        name="合成测试水杯",
        category="饮水用品",
        price_cents=1299,
        is_active=True,
    )
    food = commerce.create_product(
        operator.id,
        sku="SYN-FOOD",
        name="合成测试燕麦",
        category="食品",
        price_cents=2050,
        is_active=True,
    )
    return SimpleNamespace(
        db=db,
        operator=operator,
        member=member,
        other=other,
        commerce=commerce,
        cup=cup,
        food=food,
        health=HealthService(db),
    )


def test_cart_favorites_are_user_scoped_and_checkout_keeps_order_snapshots(context):
    c = context.commerce
    member, other = context.member.id, context.other.id
    c.add_to_cart(member, context.cup["id"], 2)
    c.add_to_cart(member, context.food["id"], 3)
    c.set_favorite(member, context.cup["id"], True)
    assert c.list_cart(other) == []
    assert c.list_favorites(other) == []
    assert len(c.list_favorites(member)) == 1
    assert sum(row["total_amount_cents"] for row in c.list_cart(member)) == 8748
    orders = c.checkout_cart(member)
    assert len(orders) == 2
    assert sum(order["total_amount_cents"] for order in orders) == 8748
    assert c.list_cart(member) == []
    assert len(c.list_orders(member)) == 2
    assert len(c.list_favorites(member)) == 1
    c.update_product(context.operator.id, context.cup["id"], price_cents=5000)
    assert (
        next(order for order in c.list_orders(member) if order["product_id"] == context.cup["id"])[
            "unit_price_cents"
        ]
        == 1299
    )


def test_unavailable_cart_product_cannot_partially_checkout(context):
    c, member = context.commerce, context.member.id
    c.add_to_cart(member, context.cup["id"])
    c.add_to_cart(member, context.food["id"])
    c.set_product_active(context.operator.id, context.food["id"], False)
    with pytest.raises(CommerceValidationError):
        c.checkout_cart(member)
    assert c.list_orders(member) == []
    assert len(c.list_cart(member)) == 2
    c.set_cart_quantity(member, context.food["id"], 0)
    assert len(c.checkout_cart(member)) == 1


def test_weather_requires_explicit_location_and_uses_mocked_public_endpoint():
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "current": {"temperature_2m": 23.5, "weather_code": 2, "time": "2026-09-08T12:00"}
            },
        )

    weather = MemberWeatherService(httpx.MockTransport(reply))
    with pytest.raises(WeatherUnavailable):
        weather.current({})
    assert not requests
    result = weather.current({"city": "用户选定的测试地区", "latitude": 30, "longitude": 120})
    assert result["temperature_c"] == 23.5
    assert result["condition"] == "多云"
    assert len(requests) == 1
    assert requests[0].url.host == "api.open-meteo.com"
    assert requests[0].url.params["latitude"] == "30.0"


def test_series_preserves_missing_and_zero_values():
    records = [
        {
            "category": "environment",
            "occurred_at": "2026-09-07T03:00:00Z",
            "details": {"temperature_c": 0},
        },
        {
            "category": "environment",
            "occurred_at": "2026-09-07T05:00:00Z",
            "details": {"temperature_c": 10},
        },
        {"category": "environment", "occurred_at": "2026-09-08T05:00:00Z", "details": {}},
    ]
    series = daily_series(records, "environment", "temperature_c", date(2026, 9, 7), 2)
    assert series[0]["value"] == 5
    assert series[1]["value"] is None
    assert series[1]["samples"] == 0


def test_record_parameters_validate_and_proposal_only_prefills(qtbot, context):
    dialog = LifeRecordDialog("environment")
    qtbot.addWidget(dialog)
    dialog.set_proposal(
        {
            "category": "environment",
            "content": "合成温度记录",
            "details": {"temperature_c": 0, "humidity_percent": 0},
        }
    )
    assert dialog.values["details"] == {"temperature_c": 0.0, "humidity_percent": 0.0}
    assert context.health.list_life_records(context.member.id) == []
    with pytest.raises(HealthValidationError):
        context.health.add_life_record(
            context.member.id,
            category="activity",
            content="合成记录",
            occurred_at=beijing_now(),
            details={"energy_kcal": -1},
        )


def test_member_navigation_today_reminders_and_local_illustrations(qtbot, context):
    prefs = PreferencesService(context.db)
    now = beijing_now()
    context.health.add_life_record(
        context.member.id,
        category="water",
        content="合成饮水记录",
        occurred_at=now,
        details={"amount_ml": 250},
    )
    context.health.add_reminder(context.member.id, "今日合成提醒", now + timedelta(minutes=1))
    context.health.add_reminder(context.member.id, "明日合成提醒", now + timedelta(days=1))
    workspace = HealthWorkspace(
        context.health, QWidget(), commerce_service=context.commerce, preferences_service=prefs
    )
    qtbot.addWidget(workspace)
    workspace.start_session(context.member)
    assert [button.text() for button in workspace.navigation_buttons] == [
        "今日记录",
        "档案与统计",
        "服务",
        "AI 助手",
        "我的平台",
    ]
    assert workspace.today_page.records_table.rowCount() == 1
    assert "250" in workspace.today_page.records_table.item(0, 1).text()
    assert workspace.today_page.reminders_table.rowCount() == 1
    assert workspace.today_page.reminders_table.item(0, 1).text() == "今日合成提醒"
    assert "天气未开启" in workspace.today_page.weather_label.text()
    assert workspace.service_page.section_tabs.count() == 2
    assert not hasattr(workspace, "records_page")
    assert not hasattr(workspace, "export_button")
    assert not product_illustration(context.cup).isNull()
    workspace.end_session()
    assert workspace.today_page.records_table.rowCount() == 0


def test_declining_smart_record_confirmation_never_writes(qtbot, context, monkeypatch):
    page = TodayPage(context.health)
    qtbot.addWidget(page)
    page.set_user(context.member.id)
    monkeypatch.setattr(LifeRecordDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    page._confirm_proposals(
        context.member.id,
        [{"category": "water", "content": "仅提议", "details": {"amount_ml": 300}}],
    )
    assert context.health.list_life_records(context.member.id) == []


def test_weather_city_without_explicit_consent_never_starts_a_request(qtbot, context):
    class Preferences:
        def get(self, user_id):
            return {"city": "合成测试城市", "weather_consent": False}

    class NoNetworkWeather:
        def current(self, location):
            raise AssertionError("没有天气同意，不应调用天气服务")

    page = TodayPage(
        context.health, preferences_service=Preferences(), weather_service=NoNetworkWeather()
    )
    qtbot.addWidget(page)
    page.set_user(context.member.id)
    page.refresh_weather(force=True)
    assert page._tasks == []
    assert "天气未开启" in page.weather_label.text()
