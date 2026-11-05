from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
APP_ENV = os.getenv("APP_ENV", "development")

app = FastAPI(
    title="Orbital Earth",
    description="Global geospatial intelligence using attributed, source-backed public data.",
    version="1.0.0",
    docs_url="/api/docs" if APP_ENV != "production" else None,
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

HTTP_TIMEOUT = httpx.Timeout(12.0, connect=5.0)
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = asyncio.Lock()


async def fetch_json(url: str, *, params: dict[str, Any] | None = None,
                     cache_key: str, ttl: int = 300,
                     headers: dict[str, str] | None = None) -> Any:
    now = time.time()
    async with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < ttl:
            return cached[1]

    request_headers = {
        "User-Agent": "OrbitalEarth/1.0 (public geospatial information client)",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=request_headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        # Include the provider status and a short sanitized response hint so
        # upstream auth, quota, and validation failures can be diagnosed.
        hint = " ".join((exc.response.text or "").split())[:240]
        detail = f"Upstream returned HTTP {exc.response.status_code}"
        if hint:
            detail += f": {hint}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Upstream data source unavailable: {type(exc).__name__}") from exc

    async with _cache_lock:
        _cache[cache_key] = (now, payload)
    return payload


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home() -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.get("/api/client-config")
async def client_config() -> dict[str, str]:
    # Restrict this browser token to this app domain and minimum required assets.
    return {"cesium_ion_token": os.getenv("CESIUM_ION_TOKEN", "")}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "Orbital Earth",
        "version": app.version,
        "environment": APP_ENV,
        "timestamp": int(time.time()),
        "data_policy": "No synthetic observations. Upstream failures are reported as unavailable.",
    }


@app.get("/api/earthquakes")
async def earthquakes(days: int = Query(default=1, ge=1, le=30),
                      min_magnitude: float = Query(default=2.5, ge=0, le=10)) -> dict[str, Any]:
    # USGS summary feeds only publish day/week/month windows. Use the FDSN
    # event API for arbitrary 1–30 day windows so every filter maps to a valid query.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    payload = await fetch_json(
        "https://earthquake.usgs.gov/fdsnws/event/1/query",
        params={
            "format": "geojson",
            "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "minmagnitude": min_magnitude,
            "orderby": "time",
            "limit": 2000,
        },
        cache_key=f"usgs:{days}:{min_magnitude}",
        ttl=90,
    )
    features = []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        if (props.get("mag") or 0) < min_magnitude:
            continue
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 3:
            continue
        features.append({
            "id": feature.get("id"),
            "longitude": coords[0],
            "latitude": coords[1],
            "depth_km": coords[2],
            "magnitude": props.get("mag"),
            "place": props.get("place"),
            "time": props.get("time"),
            "url": props.get("url"),
        })
    return {
        "source": "USGS Earthquake Hazards Program",
        "source_url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php",
        "retrieved_at": int(time.time()),
        "count": len(features),
        "features": features,
    }


@app.get("/api/weather")
async def weather(latitude: float = Query(ge=-90, le=90),
                  longitude: float = Query(ge=-180, le=180)) -> dict[str, Any]:
    payload = await fetch_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude, "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
            "timezone": "UTC",
        },
        cache_key=f"weather:{latitude:.2f}:{longitude:.2f}",
        ttl=300,
    )
    return {
        "source": "Open-Meteo",
        "source_url": "https://open-meteo.com/",
        "retrieved_at": int(time.time()),
        "data": payload,
    }


@app.get("/api/air-quality")
async def air_quality(latitude: float = Query(ge=-90, le=90),
                      longitude: float = Query(ge=-180, le=180)) -> dict[str, Any]:
    payload = await fetch_json(
        "https://air-quality-api.open-meteo.com/v1/air-quality",
        params={
            "latitude": latitude, "longitude": longitude,
            "current": "european_aqi,us_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,ozone",
            "timezone": "UTC",
        },
        cache_key=f"air:{latitude:.2f}:{longitude:.2f}",
        ttl=600,
    )
    return {
        "source": "Open-Meteo Air Quality",
        "source_url": "https://open-meteo.com/en/docs/air-quality-api",
        "retrieved_at": int(time.time()),
        "data": payload,
    }


@app.get("/api/geocode")
async def geocode(q: str = Query(min_length=2, max_length=120)) -> dict[str, Any]:
    payload = await fetch_json(
        "https://nominatim.openstreetmap.org/search",
        params={"q": q, "format": "jsonv2", "limit": 6, "addressdetails": 1},
        cache_key=f"geocode:{q.strip().lower()}",
        ttl=86400,
    )
    return {
        "source": "OpenStreetMap Nominatim",
        "source_url": "https://nominatim.org/",
        "retrieved_at": int(time.time()),
        "results": [
            {
                "name": item.get("name") or item.get("display_name"),
                "display_name": item.get("display_name"),
                "latitude": float(item["lat"]),
                "longitude": float(item["lon"]),
                "type": item.get("type"),
            }
            for item in payload
        ],
    }


@app.get("/api/places")
async def places(latitude: float = Query(ge=-90, le=90),
                 longitude: float = Query(ge=-180, le=180),
                 radius: int = Query(default=1000, ge=100, le=5000)) -> dict[str, Any]:
    """Return mapped nearby features using resilient public Overpass mirrors."""
    query = f"""
    [out:json][timeout:8];
    (node(around:{radius},{latitude},{longitude})[amenity];
     way(around:{radius},{latitude},{longitude})[amenity];
     node(around:{radius},{latitude},{longitude})[natural=peak];
     way(around:{radius},{latitude},{longitude})[natural=peak];
     node(around:{radius},{latitude},{longitude})[tourism~"viewpoint|alpine_hut"];
     way(around:{radius},{latitude},{longitude})[tourism~"viewpoint|alpine_hut"];);
    out center tags 40;
    """
    cache_key = f"places:{latitude:.3f}:{longitude:.3f}:{radius}"
    now = time.time()
    async with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < 1800:
            return cached[1]

    # Public Overpass instances are community-operated and may rate-limit or
    # temporarily fail. Try independent mirrors; never fabricate a fallback.
    endpoints = (
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://overpass.nchc.org.tw/api/interpreter",
    )
    failures: list[str] = []
    payload = None
    source_url = ""
    for endpoint in endpoints:
        try:
            payload = await fetch_json(
                endpoint,
                params={"data": query},
                cache_key=f"{cache_key}:{endpoint}",
                ttl=1800,
                headers={"Accept": "application/json"},
            )
            source_url = endpoint
            break
        except HTTPException as exc:
            failures.append(f"{endpoint.split('/')[2]}: {exc.detail}")
        except Exception as exc:
            failures.append(f"{endpoint.split('/')[2]}: {type(exc).__name__}")

    if payload is None:
        raise HTTPException(
            status_code=503,
            detail="Nearby map data is temporarily unavailable from public OpenStreetMap query servers. " +
                   "Tried " + "; ".join(failures),
        )

    results = []
    for item in payload.get("elements", []):
        tags = item.get("tags") or {}
        lat = item.get("lat", (item.get("center") or {}).get("lat"))
        lon = item.get("lon", (item.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        results.append({
            "id": f"{item.get('type')}/{item.get('id')}",
            "name": tags.get("name") or tags.get("amenity") or tags.get("natural") or tags.get("tourism") or "Unnamed place",
            "category": tags.get("amenity") or tags.get("natural") or tags.get("tourism"),
            "latitude": lat,
            "longitude": lon,
        })
    result = {
        "source": "OpenStreetMap contributors via Overpass API",
        "source_url": "https://www.openstreetmap.org/",
        "provider_endpoint": source_url,
        "retrieved_at": int(time.time()),
        "count": len(results),
        "results": results,
        "notice": "Coverage depends on OpenStreetMap mapping in this area. Zero results means no matching mapped features were returned.",
    }
    async with _cache_lock:
        _cache[cache_key] = (time.time(), result)
    return result

@app.get("/api/cameras")
async def cameras(
    north_lat: float = Query(default=84, ge=-90, le=90),
    east_lon: float = Query(default=180, ge=-180, le=180),
    south_lat: float = Query(default=-60, ge=-90, le=90),
    west_lon: float = Query(default=-180, ge=-180, le=180),
    zoom: int = Query(default=3, ge=0, le=18),
) -> dict[str, Any]:
    """Proxy current Windy Webcams v3 map clusters without caching signed image URLs."""
    api_key = os.getenv("WINDY_WEBCAMS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Windy Webcams API key is not configured on the server.")
    if south_lat >= north_lat or west_lon == east_lon:
        raise HTTPException(status_code=422, detail="Invalid map bounds.")
    # Windy cluster zoom is constrained by the geographic span of the requested
    # bounding box. Cesium's camera zoom is not the same scale as Windy's tile zoom.
    lat_span = north_lat - south_lat
    lon_span = (east_lon - west_lon) % 360
    if lon_span <= 0:
        lon_span = 360
    max_zoom_by_lat = 4 + __import__("math").log2(22.5 / lat_span)
    max_zoom_by_lon = 4 + __import__("math").log2(45.0 / lon_span)
    provider_zoom = max(0, min(18, zoom, int(min(max_zoom_by_lat, max_zoom_by_lon))))
    payload = await fetch_json(
        "https://api.windy.com/webcams/api/v3/map/clusters",
        params={
            "northLat": north_lat, "eastLon": east_lon,
            "southLat": south_lat, "westLon": west_lon,
            "zoom": provider_zoom,
            "include": "categories,images,location,player,urls",
            "lang": "en",
        },
        cache_key=f"windy-cameras:{north_lat}:{east_lon}:{south_lat}:{west_lon}:{provider_zoom}:{int(time.time())}",
        ttl=0,
        headers={"x-windy-api-key": api_key},
    )
    return {
        "source": "Windy Webcams API v3",
        "source_url": "https://api.windy.com/webcams/docs",
        "retrieved_at": int(time.time()),
        "data": payload,
        "notice": "Provider response only. Image URLs can expire; reload to refresh them.",
    }




_health_cache: tuple[float, dict[str, Any]] | None = None
_health_lock = asyncio.Lock()


@app.get("/api/source-health")
async def source_health() -> dict[str, Any]:
    """Probe real upstream endpoints and report measured reachability, not configuration alone."""
    global _health_cache
    now = time.time()
    async with _health_lock:
        if _health_cache and now - _health_cache[0] < 120:
            return _health_cache[1]

    probes = [
        ("USGS Earthquakes", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson", None),
        ("Open-Meteo", "https://api.open-meteo.com/v1/forecast", {"latitude": 0, "longitude": 0, "current": "temperature_2m"}),
        ("OpenStreetMap Nominatim", "https://nominatim.openstreetmap.org/search", {"q": "Pretoria", "format": "jsonv2", "limit": 1}),
        ("Overpass · Kumi", "https://overpass.kumi.systems/api/status", None),
        ("Overpass · Main", "https://overpass-api.de/api/status", None),
        ("Overpass · Taiwan", "https://overpass.nchc.org.tw/api/status", None),
    ]

    async def probe(name: str, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            headers = {"User-Agent": "OrbitalEarth/1.0 (public geospatial information client)", "Accept": "application/json,text/plain"}
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
            return {"name": name, "status": "online", "http_status": response.status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000)}
        except httpx.HTTPStatusError as exc:
            return {"name": name, "status": "degraded", "http_status": exc.response.status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            return {"name": name, "status": "unavailable", "error": type(exc).__name__,
                    "latency_ms": round((time.perf_counter() - started) * 1000)}

    results = await asyncio.gather(*(probe(*item) for item in probes))
    overpass = [item for item in results if item["name"].startswith("Overpass")]
    core = [item for item in results if not item["name"].startswith("Overpass")]
    online_core = sum(item["status"] == "online" for item in core)
    result = {
        "checked_at": int(time.time()),
        "cache_seconds": 120,
        "overall": "online" if online_core == len(core) and any(item["status"] == "online" for item in overpass)
                   else "degraded" if online_core or any(item["status"] == "online" for item in overpass)
                   else "unavailable",
        "sources": results,
        "note": "Reachability probes only. A successful probe does not guarantee every data request or geographic area is covered."
    }
    async with _health_lock:
        _health_cache = (time.time(), result)
    return result


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    cameras_configured = bool(os.getenv("WINDY_WEBCAMS_API_KEY", "").strip())
    available = [
        {"id": "globe", "name": "3D Earth globe", "status": "available", "note": "CesiumJS with Esri World Imagery basemap; imagery acquisition dates vary."},
        {"id": "earthquakes", "name": "Earthquakes", "status": "live_source", "source": "USGS"},
        {"id": "weather", "name": "Weather", "status": "live_source", "source": "Open-Meteo"},
        {"id": "air", "name": "Air quality", "status": "live_source", "source": "Open-Meteo"},
        {"id": "geocode", "name": "Location search", "status": "live_source", "source": "OpenStreetMap Nominatim"},
        {"id": "places", "name": "Nearby places and natural peaks", "status": "live_source", "source": "OpenStreetMap Overpass"},
    ]
    not_connected = [
        {"id": "drones", "name": "Drone telemetry", "status": "not_connected", "note": "Requires an authorized operator or provider integration."},
        {"id": "satellite_realtime", "name": "Real-time satellite tasking", "status": "not_connected", "note": "The basemap is not represented as live satellite tasking."},
    ]
    if cameras_configured:
        available.append({"id": "cameras", "name": "Public camera directory", "status": "configured", "source": "Windy Webcams API v3", "note": "Live camera coverage is requested for the visible map bounds. Individual streams depend on provider availability."})
    else:
        not_connected.insert(0, {"id": "cameras", "name": "Public camera feeds", "status": "not_connected", "note": "Windy Webcams API key is not configured. No placeholder stream is presented as live."})
    return {
        "available": available,
        "not_connected": not_connected,
        "policy": "Only source-backed data is displayed. Unavailable integrations remain explicitly disconnected.",
    }

