"""Public weather lookup only for a location explicitly supplied by the member."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

WEATHER_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "雷雨伴冰雹",
}


class WeatherUnavailable(RuntimeError):
    pass


class MemberWeatherService:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def current(self, location: Mapping[str, Any]) -> dict[str, Any]:
        city = str(location.get("city") or "").strip()
        latitude, longitude = location.get("latitude"), location.get("longitude")
        if not city and (latitude is None or longitude is None):
            raise WeatherUnavailable("请在我的平台设置天气地区")
        with httpx.Client(timeout=8, transport=self.transport) as client:
            if latitude is None or longitude is None:
                response = client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={
                        "name": city,
                        "count": 1,
                        "language": "zh",
                        "format": "json",
                    },
                )
                response.raise_for_status()
                matches = response.json().get("results", [])
                if not matches:
                    raise WeatherUnavailable("未找到该地区，请填写更完整的城市名称")
                place = matches[0]
                latitude, longitude = place["latitude"], place["longitude"]
                city = " · ".join(
                    dict.fromkeys(
                        str(place.get(k) or "")
                        for k in ("country", "admin1", "name")
                        if place.get(k)
                    )
                )
            latitude, longitude = float(latitude), float(longitude)
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise WeatherUnavailable("地区坐标无效，请重新设置")
            response = client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,weather_code",
                    "timezone": "auto",
                },
            )
            response.raise_for_status()
            payload = response.json()
            current = payload.get("current") or {}
            if current.get("temperature_2m") is None or current.get("weather_code") is None:
                raise WeatherUnavailable("天气服务未返回当前数据")
            return {
                "city": city or f"{latitude:.3f}, {longitude:.3f}",
                "temperature_c": current["temperature_2m"],
                "condition": WEATHER_CODES.get(int(current["weather_code"]), "天气状况未知"),
                "observed_at": current.get("time", ""),
                "source": "Open-Meteo",
            }
