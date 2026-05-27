from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen


USER_AGENT = "bird-audio-suite"


@dataclass(frozen=True)
class ResolvedLocation:
    latitude: float
    longitude: float
    place_name: str
    source: str
    used_fallback: bool


def _build_place_name(*parts: str) -> str:
    return ", ".join(part.strip() for part in parts if part and part.strip())


def _fetch_json(url: str, timeout_seconds: float) -> dict | None:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.load(response)
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


def _lookup_environment() -> ResolvedLocation | None:
    latitude = os.getenv("BIRD_AUDIO_LATITUDE")
    longitude = os.getenv("BIRD_AUDIO_LONGITUDE")
    if not latitude or not longitude:
        return None

    try:
        parsed_latitude = float(latitude)
        parsed_longitude = float(longitude)
    except ValueError:
        return None

    return ResolvedLocation(
        latitude=parsed_latitude,
        longitude=parsed_longitude,
        place_name=os.getenv("BIRD_AUDIO_PLACE_NAME", "").strip(),
        source="environment",
        used_fallback=False,
    )


def _lookup_ipapi(timeout_seconds: float) -> ResolvedLocation | None:
    data = _fetch_json("https://ipapi.co/json/", timeout_seconds)
    if not data:
        return None

    latitude = data.get("latitude")
    longitude = data.get("longitude")
    if latitude is None or longitude is None:
        return None

    place_name = _build_place_name(
        data.get("city", ""),
        data.get("region", ""),
        data.get("country_name", ""),
    )
    if not place_name:
        return None

    return ResolvedLocation(
        latitude=float(latitude),
        longitude=float(longitude),
        place_name=place_name,
        source="ipapi",
        used_fallback=False,
    )


def _lookup_ipwhois(timeout_seconds: float) -> ResolvedLocation | None:
    data = _fetch_json("https://ipwho.is/", timeout_seconds)
    if not data or not data.get("success"):
        return None
    if data.get("connection", {}).get("proxy"):
        return None

    latitude = data.get("latitude")
    longitude = data.get("longitude")
    if latitude is None or longitude is None:
        return None

    place_name = _build_place_name(
        data.get("city", ""),
        data.get("region", ""),
        data.get("country", ""),
    )
    if not place_name:
        return None

    return ResolvedLocation(
        latitude=float(latitude),
        longitude=float(longitude),
        place_name=place_name,
        source="ipwhois",
        used_fallback=False,
    )


def resolve_location(
    *,
    fallback_latitude: float,
    fallback_longitude: float,
    fallback_place_name: str,
    enable_auto_lookup: bool = True,
    timeout_seconds: float = 2.0,
) -> ResolvedLocation:
    env_location = _lookup_environment()
    if env_location:
        return ResolvedLocation(
            latitude=env_location.latitude,
            longitude=env_location.longitude,
            place_name=env_location.place_name or fallback_place_name,
            source=env_location.source,
            used_fallback=False,
        )

    if enable_auto_lookup:
        for lookup in (_lookup_ipapi, _lookup_ipwhois):
            result = lookup(timeout_seconds)
            if result:
                return ResolvedLocation(
                    latitude=result.latitude,
                    longitude=result.longitude,
                    place_name=result.place_name or fallback_place_name,
                    source=result.source,
                    used_fallback=False,
                )

    return ResolvedLocation(
        latitude=fallback_latitude,
        longitude=fallback_longitude,
        place_name=fallback_place_name,
        source="config",
        used_fallback=True,
    )
