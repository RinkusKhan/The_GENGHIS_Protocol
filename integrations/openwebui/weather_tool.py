"""
title: Weather (Open-Meteo)
author: The GENGHIS Protocol
description: Current conditions and a 7-day forecast for any place, from Open-Meteo — no API key, no account.
version: 0.1.0
requirements: requests
"""
# Open WebUI Tool: Workspace -> Tools -> + -> paste -> Save -> enable on the model or per chat.
# Geocodes the place name with Open-Meteo's free geocoder, then pulls the forecast. Units default to US.

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        units: str = Field(default="us", description="'us' (F, mph, in) or 'metric' (C, km/h, mm)")

    def __init__(self):
        self.valves = self.Valves()

    def weather_forecast(self, place: str, days: int = 7) -> str:
        """Get the current weather and a daily forecast for a place (a city, 'city, state', or 'city, country').
        Use whenever the user asks about weather, temperature, rain, snow, wind or a forecast for a location.
        `days` is 1-7 (default 7)."""
        try:
            g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": place, "count": 1, "language": "en", "format": "json"}, timeout=10).json()
            hits = g.get("results") or []
            if not hits:
                return f"I couldn't find a place called '{place}'."
            loc = hits[0]
            name = ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x)
            us = self.valves.units.lower() == "us"
            params = {"latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto",
                      "forecast_days": max(1, min(int(days or 7), 7)),
                      "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
                      "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,sunrise,sunset"}
            if us:
                params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
            w = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10).json()
        except Exception as e:
            return f"Weather lookup failed: {e}"
        t, ws, pr = ("°F", "mph", "in") if us else ("°C", "km/h", "mm")
        c = w.get("current") or {}
        out = [f"Weather for {name} (local time {c.get('time', '')}):",
               f"- Now: {c.get('temperature_2m')}{t} (feels {c.get('apparent_temperature')}{t}), {self._desc(c.get('weather_code'))}, "
               f"humidity {c.get('relative_humidity_2m')}%, wind {c.get('wind_speed_10m')} {ws}, precip {c.get('precipitation')} {pr}"]
        d = w.get("daily") or {}
        for i, day in enumerate(d.get("time") or []):
            out.append(f"- {day}: {self._desc((d.get('weather_code') or [None])[i])}, high {d['temperature_2m_max'][i]}{t} / low {d['temperature_2m_min'][i]}{t}, "
                       f"rain chance {d['precipitation_probability_max'][i]}%, precip {d['precipitation_sum'][i]} {pr}, wind up to {d['wind_speed_10m_max'][i]} {ws}, "
                       f"sun {str(d['sunrise'][i])[-5:]}–{str(d['sunset'][i])[-5:]}")
        return "\n".join(out)

    @staticmethod
    def _desc(code):
        table = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "icy fog",
                 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
                 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
                 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
                 82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
                 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}
        return table.get(code, f"code {code}")
