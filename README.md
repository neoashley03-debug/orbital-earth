# Orbital Earth

**Global geospatial intelligence, built on verified public data.**

Orbital Earth is a standalone, mobile-friendly geospatial application. It combines a CesiumJS 3D globe with a FastAPI data gateway and source-attributed observations.

## Current capabilities

| Capability | Provider | Availability |
|---|---|---|
| 3D globe | CesiumJS | Available |
| Satellite basemap | Esri World Imagery | Available; imagery dates vary |
| Earthquakes | USGS GeoJSON feeds | Live public feed |
| Weather | Open-Meteo | Live public API |
| Air quality | Open-Meteo | Live public API |
| Location search | OpenStreetMap Nominatim | Public geocoding |
| Nearby mapped amenities | OpenStreetMap Overpass | Public map data |
| Public CCTV / traffic cameras | Not configured | Not connected |
| Authorized drone telemetry | Not configured | Not connected |
| Real-time satellite tasking | Not configured | Not connected |

The application intentionally does not invent camera feeds, drone tracks, satellite observations, or sensor readings. The basemap is not represented as live satellite tasking. Source failures are surfaced as unavailable rather than replaced with fabricated values.

## Architecture

- **Frontend:** CesiumJS, responsive HTML/CSS/JavaScript, installable PWA shell.
- **Backend:** FastAPI and HTTPX, with bounded upstream timeouts and short in-memory response caching.
- **Deployment:** Render web service using the included `render.yaml`.
- **Health:** `GET /api/health`
- **Capabilities:** `GET /api/capabilities`
- **API docs:** `/api/docs` in non-production environments.

## Run locally

Python 3.12 or later is recommended.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Deploy to Render

1. Create a Render **Web Service** from this GitHub repository.
2. Select branch `main` and Python runtime.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Health-check path: `/api/health`
6. Deploy. Future commits to `main` can deploy automatically.

The included `render.yaml` describes the same service for Blueprint deployment.

## Operational notes

- Public upstream services have their own rate limits and availability policies. The API caches responses briefly but is not a substitute for a production distributed cache.
- Nominatim and Overpass are community services. Keep request rates modest and follow their usage policies before scaling.
- Add credentials only as Render environment variables. Never commit API keys, tokens, private camera URLs, or operator credentials.
- Before production scale, add persistent cache/queue infrastructure, structured logs, metrics, rate limiting, request budgets, and automated tests.
- Camera and drone integrations must be individually reviewed for provider terms, access authorization, privacy, retention, and applicable law.

## Project boundary

Orbital Earth is an independent project. It is not part of TW Trades or TW Website Studio AI.
