from datetime import datetime, timedelta, timezone

import openmeteo_requests
import requests_cache
from retry_requests import retry

# Setup the Open-Meteo API client with cache and retry on error
cache_session = requests_cache.CachedSession(".cache", expire_after=3600)
retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
openmeteo = openmeteo_requests.Client(session=retry_session)

url = "https://api.open-meteo.com/v1/forecast"
params = {
    "latitude": 6.7971255,
    "longitude": 79.9018735,
    "current": [
        "temperature_2m",
        "relative_humidity_2m",
        "apparent_temperature",
        "precipitation",
        "rain",
    ],
    "timezone": "Asia/Colombo",
}
responses = openmeteo.weather_api(url, params=params)

response = responses[0]
current = response.Current()

current_temperature_2m = current.Variables(0).Value()
current_relative_humidity = current.Variables(1).Value()
current_apparent_temp = current.Variables(2).Value()
current_precipitation = current.Variables(3).Value()
current_rain = current.Variables(4).Value()

local_time = datetime.fromtimestamp(
    current.Time(), tz=timezone(timedelta(seconds=response.UtcOffsetSeconds()))
)


def get_llm_context() -> str:
    """Returns multi-line context string for use in an LLM system prompt."""
    rain_str = f", {current_rain:.1f} mm rain" if current_rain > 0 else ""
    precip_str = (
        f", light precipitation ({current_precipitation:.1f} mm{rain_str})"
        if current_precipitation > 0
        else ""
    )
    return (
        "User is currently located at the Faculty of Information Technology, "
        "University of Moratuwa, Katubedda, Sri Lanka.\n"
        f"Current time: {local_time.strftime('%A, %B %d %Y, %I:%M %p')}\n"
        f"Weather: {current_temperature_2m:.1f}°C, feels like {current_apparent_temp:.1f}°C, "
        f"humidity {current_relative_humidity:.0f}%{precip_str}"
    )


print(get_llm_context())
