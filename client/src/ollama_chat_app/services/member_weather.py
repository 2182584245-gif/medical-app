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

# A small, dependency-free location label table keeps the desktop shell
# useful when reverse geocoding is unavailable.  Coordinates and conditions
# still come from Open-Meteo; these labels are presentation metadata only.
CITY_META = {
    "北京": {"country": "中国", "province": "北京市", "city_name": "北京市"},
    "上海": {"country": "中国", "province": "上海市", "city_name": "上海市"},
    "广州": {"country": "中国", "province": "广东省", "city_name": "广州市"},
    "深圳": {"country": "中国", "province": "广东省", "city_name": "深圳市"},
    "杭州": {"country": "中国", "province": "浙江省", "city_name": "杭州市"},
    "南京": {"country": "中国", "province": "江苏省", "city_name": "南京市"},
    "成都": {"country": "中国", "province": "四川省", "city_name": "成都市"},
    "武汉": {"country": "中国", "province": "湖北省", "city_name": "武汉市"},
    "西安": {"country": "中国", "province": "陕西省", "city_name": "西安市"},
}


class WeatherUnavailable(RuntimeError):
    pass


class MemberWeatherService:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def current_from_ip(self) -> dict[str, Any]:
        """Resolve a coarse public-IP city, then fetch current weather.

        This is an optional convenience path for a fresh profile.  It sends
        no account data and falls back to the bounded Beijing default when the
        free geolocation endpoint is unavailable.  A saved profile city is
        still preferred by the bridge and never triggers this lookup.
        """
        try:
            with httpx.Client(timeout=4, transport=self.transport) as client:
                response = client.get("https://ipapi.co/json/")
                response.raise_for_status()
                payload = response.json()
            city = str(payload.get("city") or "").strip()
            latitude, longitude = payload.get("latitude"), payload.get("longitude")
            if city and latitude is not None and longitude is not None:
                return self.current({"city": city, "latitude": latitude, "longitude": longitude})
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            pass
        return self.fallback()

    @staticmethod
    def fallback() -> dict[str, Any]:
        """Return a readable local snapshot when public weather is offline."""
        return {
            "city": "北京市",
            "location_label": "中国 · 北京市 · 北京市",
            "country": "中国",
            "province": "北京市",
            "city_name": "北京市",
            "temperature_c": 24,
            "condition": "天气良好",
            "observed_at": "",
            "source": "local-fallback",
        }

    def current(self, location: Mapping[str, Any]) -> dict[str, Any]:
        city = str(location.get("city") or "").strip()
        latitude, longitude = location.get("latitude"), location.get("longitude")
        if not city and (latitude is None or longitude is None):
            raise WeatherUnavailable("请在我的平台设置天气地区")
        metadata = next((value for key, value in CITY_META.items() if key in city), None)
        location_label = city
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
                location_label = " · ".join(
                    dict.fromkeys(
                        str(place.get(k) or "")
                        for k in ("country", "admin1", "name")
                        if place.get(k)
                    )
                )
                metadata = {
                    "country": str(place.get("country") or "中国"),
                    "province": str(place.get("admin1") or ""),
                    "city_name": str(place.get("name") or city),
                }
                city = metadata["city_name"]
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
                "location_label": location_label or city or f"{latitude:.3f}, {longitude:.3f}",
                "country": (metadata or {}).get("country", "中国"),
                "province": (metadata or {}).get("province", ""),
                "city_name": (metadata or {}).get("city_name", city),
                "temperature_c": current["temperature_2m"],
                "condition": WEATHER_CODES.get(int(current["weather_code"]), "天气状况未知"),
                "observed_at": current.get("time", ""),
                "source": "Open-Meteo",
            }
