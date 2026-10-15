"""
iplocation.py - approximate location from an IP address.

Used as a backup when the browser's own geolocation times out (error code 3).
IP lookups are only city-level accurate and can be wrong behind VPNs or mobile
networks, so the UI labels the result "approximate".

Providers are tried in order (both are free, HTTPS and need no API key at the
time of writing; check their current limits before you launch):
    1. ipwho.is   - about 1,000 requests/day per calling IP
    2. ipapi.co   - about 1,000 requests/day per calling IP
Results are cached per IP for an hour so those limits last longer.
"""
import json
import logging
import time
import urllib.request

logger = logging.getLogger("api")

TIMEOUT_S = 4
CACHE_TTL_S = 3600
CACHE_MAX = 2000
_cache: dict[str, tuple[float, dict]] = {}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "nearby-chat/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


def _ipwho(ip: str):
    # An empty ip means "the address that is calling", which is only right for local testing.
    data = _get_json(f"https://ipwho.is/{ip}?fields=success,message,latitude,longitude,city,country")
    if not data.get("success"):
        raise ValueError(data.get("message") or "lookup failed")
    return data["latitude"], data["longitude"], data.get("city"), data.get("country")


def _ipapi(ip: str):
    data = _get_json(f"https://ipapi.co/{ip + '/' if ip else ''}json/")
    if data.get("error"):
        raise ValueError(data.get("reason") or "lookup failed")
    return data["latitude"], data["longitude"], data.get("city"), data.get("country_name")


PROVIDERS = [("ipwho.is", _ipwho), ("ipapi.co", _ipapi)]


def lookup(ip: str):
    """Returns {'lat', 'lon', 'place'} (about 1 km precision) or None if every provider failed.

    `ip` must already be validated by the caller. Pass "" to look up the server's own
    public address (only sensible when the server and the browser are the same machine).
    """
    key = ip or "self"
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    for name, fetch in PROVIDERS:
        try:
            lat, lon, city, country = fetch(ip)
            lat, lon = float(lat), float(lon)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("coordinates out of range")
            result = {
                "lat": round(lat, 2),
                "lon": round(lon, 2),
                "place": ", ".join(p for p in (city, country) if p),
            }
            if len(_cache) >= CACHE_MAX:
                _cache.clear()
            _cache[key] = (time.time() + CACHE_TTL_S, result)
            return result
        except Exception as err:
            logger.warning(f"ip lookup via {name} failed: {type(err).__name__}: {err}")
    return None
