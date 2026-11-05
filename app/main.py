from __future__ import annotations

import asyncio
import os
import time
from typing import Any

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
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Upstream data source unavailable: {type(exc).__name__}") from exc

    async with _cache_lock:
        _cache[cache_key] = (now, payload)
    return payload


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    return HTMLResponse(index.read_text(encoding="utf-8"))


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
    url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_{days}_day.geojson"
    payload = await fetch_json(url, cache_key=f"usgs:{days}", ttl=90)
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
    # Overpass is community infrastructure; requests are cached and kept small.
    query = f"""
    [out:json][timeout:10];
    (node(around:{radius},{latitude},{longitude})[amenity];
     way(around:{radius},{latitude},{longitude})[amenity];);
    out center tags 40;
    """
    payload = await fetch_json(
        "https://overpass-api.de/api/interpreter",
        params={"data": query},
        cache_key=f"places:{latitude:.3f}:{longitude:.3f}:{radius}",
        ttl=1800,
        headers={"Accept": "application/json"},
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
            "name": tags.get("name") or tags.get("amenity") or "Unnamed place",
            "category": tags.get("amenity"),
            "latitude": lat,
            "longitude": lon,
        })
    return {
        "source": "OpenStreetMap contributors via Overpass API",
        "source_url": "https://overpass-api.de/",
        "retrieved_at": int(time.time()),
        "count": len(results),
        "results": results,
    }


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    return {
        "available": [
            {"id": "globe", "name": "3D Earth globe", "status": "available", "note": "CesiumJS with Esri World Imagery basemap; imagery acquisition dates vary."},
            {"id": "earthquakes", "name": "Earthquakes", "status": "live_source", "source": "USGS"},
            {"id": "weather", "name": "Weather", "status": "live_source", "source": "Open-Meteo"},
            {"id": "air", "name": "Air quality", "status": "live_source", "source": "Open-Meteo"},
            {"id": "geocode", "name": "Location search", "status": "live_source", "source": "OpenStreetMap Nominatim"},
            {"id": "places", "name": "Nearby places", "status": "live_source", "source": "OpenStreetMap Overpass"},
        ],
        "not_connected": [
            {"id": "cameras", "name": "Public camera feeds", "status": "not_connected", "note": "No verified feed endpoints configured. No preview or placeholder stream is presented as live."},
            {"id": "drones", "name": "Drone telemetry", "status": "not_connected", "note": "Requires an authorized operator or provider integration."},
            {"id": "satellite_realtime", "name": "Real-time satellite tasking", "status": "not_connected", "note": "The basemap is not represented as live satellite tasking."},
        ],
        "policy": "Only source-backed data is displayed. Unavailable integrations remain explicitly disconnected.",
    }
