from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
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


def _load_last_known(cache_path: Path | None) -> ResolvedLocation | None:
    if cache_path is None:
        return None
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        return ResolvedLocation(
            latitude=float(data["latitude"]),
            longitude=float(data["longitude"]),
            place_name=str(data.get("place_name", "")),
            source=f"last known ({data.get('source', '?')})",
            used_fallback=True,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_last_known(cache_path: Path | None, location: ResolvedLocation) -> None:
    if cache_path is None:
        return
    try:
        Path(cache_path).write_text(json.dumps(asdict(location)), encoding="utf-8")
    except OSError:
        pass


def resolve_location(
    *,
    fallback_latitude: float,
    fallback_longitude: float,
    fallback_place_name: str,
    enable_auto_lookup: bool = True,
    timeout_seconds: float = 2.0,
    cache_path: Path | None = None,
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
                resolved = ResolvedLocation(
                    latitude=result.latitude,
                    longitude=result.longitude,
                    place_name=result.place_name or fallback_place_name,
                    source=result.source,
                    used_fallback=False,
                )
                _save_last_known(cache_path, resolved)
                return resolved

    # Offline: the laptop travels, so the last position actually seen beats the config default.
    last_known = _load_last_known(cache_path)
    if last_known is not None:
        return last_known

    return ResolvedLocation(
        latitude=fallback_latitude,
        longitude=fallback_longitude,
        place_name=fallback_place_name,
        source="config",
        used_fallback=True,
    )
