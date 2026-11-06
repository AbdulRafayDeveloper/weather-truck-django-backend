# Weather-Aware Truck Routing: Backend API

Django REST API that helps a truck driver pick the safest route. It geocodes the trip, asks TomTom for three truck routes, checks the hourly forecast at each checkpoint for the time the truck will actually be there, applies the risk and load rules, and recommends a route.

- **Live API:** https://weather-truck-backend.vercel.app
- **Live app:** https://weathertruck.vercel.app ([frontend repo](https://github.com/AbdulRafayDeveloper/weather-truck))

## Requirements coverage

| Requirement | Where it lives |
|---|---|
| Origin, destination, departure time, load weight | `POST /api/routes/`, validated in `routes/views.py` (plus an optional `interval_miles`) |
| Three route options from a routing API | TomTom Routing API with `travelMode=truck` and two alternatives |
| Weather every 10 / 25 / 50 miles at the truck's ETA | Checkpoints and ETAs in `routes/services.py`, matched to the Open-Meteo hourly forecast |
| Risk thresholds and load rules | `routes/risk.py` (tables below) |
| Recommendation: Severe miles, then High miles, then average risk, then travel time | `evaluate_route_recommendations` in `routes/risk.py` |
| Map, heatmap and 0–48 h slider | In the [frontend](https://github.com/AbdulRafayDeveloper/weather-truck). The slider calls this API again with a later departure time. |

## Tech stack

- Python 3.11+, Django 5, Django REST Framework, django-cors-headers, requests
- TomTom Geocoding API and Routing API
- Open-Meteo hourly forecast (`wind_speed_10m`, `rain`, `snowfall`)

## How it works

1. **Geocoding.** Both locations go through TomTom. If the top two matches score the same but are more than 25 km apart (think "Springfield"), the API asks the user to be more specific instead of guessing.
2. **Routing.** TomTom returns up to three truck routes. Sections TomTom marks as not designated for trucks are flagged with a warning.
3. **Checkpoints.** A checkpoint is placed at mile 0, every `interval_miles`, and at the destination. The positions come from walking the actual road polyline once.
4. **ETAs.** TomTom's progress markers give distance and travel time at some points along the route. These are spread over the whole polyline, so each checkpoint gets an arrival time that reflects the real road. Times keep the origin's UTC offset, and `eta_unix` holds the UTC timestamp. If TomTom sends no usable progress data, the ETAs fall back to a straight-line estimate and the route carries `"progress_source": "interpolated_fallback"` and a warning.
5. **Weather.** Checkpoints from all routes are deduplicated (to about 11 m) and fetched from Open-Meteo in batches of up to 50 locations. Each checkpoint uses the forecast hour closest to its ETA, within 30 minutes. On an exact tie the later hour wins. Missing values are reported as `partial` or `unavailable`, never treated as safe.
6. **Risk and recommendation.** Each checkpoint gets a level for wind, rain and snow, and the worst of the three becomes its overall level. The miles of the segment leading to that checkpoint are added to that level's total, and the route with the best ranking is recommended.

## Risk rules

| Condition | Low | Moderate | High | Severe | No Travel |
|---|---|---|---|---|---|
| Wind (mph) | under 25 | 25 to under 35 | 35 to under 45 | 45 to under 55 | 55 or more |
| Rain (in/hr) | under 0.10 | 0.10 to 0.25 | over 0.25 to 0.50 | over 0.50 to 1.00 | over 1.00 |
| Snow (in/hr) | under 0.5 | 0.5 to 1.0 | over 1.0 to 2.0 | over 2.0 to 3.0 | over 3.0 |

Where the brief's ranges touch (for example rain at exactly 0.25), the value goes to the lower level.

**Load rules for wind:**
- 55 mph or more is No Travel for any load.
- 45 to 54 mph with a load over 30,000 lb is No Travel.
- 35 to 44 mph with a load over 40,000 lb is Severe.

**Average risk** is distance-weighted: Low = 1, Moderate = 2, High = 3, Severe = 4, divided by the route length. It is `null` when a route has any No Travel miles or any miles without weather data, since a number there would be misleading.

**Recommendation.** Only routes that are fully assessed and have no No Travel miles can be recommended. They are ranked by:
1. Fewest Severe miles
2. Fewest High miles
3. Lowest average risk
4. Shortest travel time
5. TomTom's own order, if everything else is equal

The `message` says which of these rules decided the result.

| `status` | Meaning |
|---|---|
| `recommended` | A route was picked. If some other routes lack weather data, the message says so. |
| `no_safe_route` | Every route has No Travel miles. Message: "No safe route available under current weather conditions." |
| `incomplete_weather` | No route could be confirmed safe because weather data is missing. Message: "Route safety could not be fully assessed because weather data is missing." |

## API

| Method | Path | Description |
|---|---|---|
| GET | `/` | Service status and list of endpoints |
| GET | `/api/health/` | Health check: `{"status": "ok"}` |
| POST | `/api/routes/` | Routes, weather, risk and recommendation |

Request:

```json
{
  "origin": "Chicago, IL",
  "destination": "Denver, CO",
  "departure": "2026-11-03T08:00:00",
  "load_weight_lb": 38000,
  "interval_miles": 25
}
```

Response (shortened):

```json
{
  "recommendation": {
    "recommended_route_id": 1,
    "status": "recommended",
    "message": "Route 1 is recommended as the safest route under current weather conditions.",
    "all_routes_unsafe": false,
    "is_weather_complete": true
  },
  "routes": [
    {
      "id": 1,
      "distance_miles": 1004.4,
      "duration_seconds": 50100,
      "duration_minutes": 835,
      "departure": "2026-11-03T08:00:00-06:00",
      "arrival": "2026-11-03T21:12:06-07:00",
      "is_recommended": true,
      "risk_summary": {
        "low_miles": 954.4,
        "moderate_miles": 50.0,
        "high_miles": 0.0,
        "severe_miles": 0.0,
        "no_travel_miles": 0.0,
        "unassessed_miles": 0.0,
        "average_risk": 1.05,
        "is_fully_assessed": true,
        "has_no_travel": false
      },
      "checkpoint_count": 42,
      "checkpoints": [
        {
          "id": 1,
          "latitude": 41.88323,
          "longitude": -87.6324,
          "mile_marker": 0.0,
          "segment_miles": 0.0,
          "eta": "2026-11-03T08:00:00-06:00",
          "eta_unix": 1793714400,
          "weather": {
            "status": "available",
            "wind_speed_mph": 13.0,
            "rain_in": 0.0,
            "snowfall_in": 0.0,
            "forecast_time_unix": 1793714400,
            "forecast_match_offset_seconds": 0
          },
          "risk": {
            "assessment_status": "assessed",
            "wind_level": "Low",
            "rain_level": "Low",
            "snow_level": "Low",
            "overall_level": "Low"
          }
        }
      ]
    }
  ]
}
```

Errors come back as `{"error": {"code": "...", "message": "..."}}` with a matching HTTP status, for example `VALIDATION_ERROR` (400), `GEOCODE_NOT_FOUND` (404), `ROUTE_NOT_FOUND` (404) or `PROVIDER_ERROR` (502).

## Project structure

```text
weather-truck-django-backend/
├── config/
│   ├── settings.py      # Settings from environment variables, CORS, hosts
│   ├── urls.py          # Status page and /api/ routes
│   └── wsgi.py          # WSGI entry point, exposed as `app` for Vercel
├── routes/
│   ├── services.py      # TomTom, checkpoints, ETAs, Open-Meteo
│   ├── risk.py          # Risk rules, route totals, recommendation
│   ├── utils.py         # Distance and interpolation helpers
│   ├── views.py         # Endpoints and request validation
│   ├── urls.py
│   └── tests.py
├── .env.example
├── manage.py
├── requirements.txt
├── vercel.json
├── render.yaml
└── Procfile
```

## Running locally

You need Python 3.11+ and a TomTom API key.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env             # then add your TOMTOM_API_KEY
python manage.py runserver 127.0.0.1:8000
```

Check it's up:

```bash
curl http://127.0.0.1:8000/api/health/
# {"status":"ok"}
```

## Tests

```bash
python manage.py test routes
```

There are 114 tests covering geocoding, routing, checkpoint placement, ETAs, weather matching, every risk threshold, the load rules and the recommendation order. TomTom and Open-Meteo are mocked, so the tests run offline and don't need a `.env` file or an API key.

## Deployment

The API runs on Vercel and redeploys on every push to `main`. `vercel.json` sends all requests to `config/wsgi.py`.

| Variable | Value |
|---|---|
| `TOMTOM_API_KEY` | TomTom API key |
| `OPEN_METEO_BASE_URL` | `https://api.open-meteo.com/v1/forecast` |
| `DJANGO_SECRET_KEY` | A long random string |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | e.g. `.vercel.app` (added automatically on Vercel) |
| `FRONTEND_URL` | Frontend origin(s) for CORS, comma-separated |

`render.yaml` and `Procfile` are kept for running on Render with Gunicorn.

## Attribution

Weather data by [Open-Meteo.com](https://open-meteo.com/), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Open-Meteo is free for non-commercial use; commercial use needs their paid plan or a self-hosted instance.
