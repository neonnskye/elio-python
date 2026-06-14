from datetime import datetime, timedelta, timezone

import openmeteo_requests
import requests_cache
from retry_requests import retry

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

current_time = datetime.now(timezone(timedelta(hours=5, minutes=30)))


def get_llm_context() -> str:
    """Returns multi-line context string for use in an LLM system prompt."""
    precip_str = (
        f", precipitation {current_precipitation:.1f} mm"
        if current_precipitation > 0
        else ""
    )
    return (
        "User is currently located at the Faculty of Information Technology, "
        "University of Moratuwa, Katubedda, Sri Lanka.\n"
        f"Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Weather: {current_temperature_2m:.1f}°C, feels like {current_apparent_temp:.1f}°C, "
        f"humidity {current_relative_humidity:.0f}%{precip_str}"
    )


print(get_llm_context())
