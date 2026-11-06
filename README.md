# Weather-Aware Truck Routing — Backend API

Backend service for the Weather-Aware Truck Routing system, providing geocoding, multi-alternative commercial truck routing, geographically accurate checkpoint generation with timezone-aware ETA interpolation, Open-Meteo hourly weather forecast integration, environmental risk scoring, and safe route recommendation.

---

## 1. Tech Stack

- **Python:** 3.11+ / 3.12+
- **Framework:** Django 5.x + Django REST Framework (DRF)
- **CORS:** django-cors-headers
- **HTTP Client:** requests
- **External APIs:**
  - TomTom Geocoding API v2 (structured address search with ambiguity detection)
  - TomTom Maps Routing API v1 (`calculateRoute` with `travelMode=truck` and `extendedRouteRepresentation=distance,travelTime`)
  - Open-Meteo Forecast API (hourly `wind_speed_10m`, `rain`, `snowfall`)

---

## 2. Key Capabilities Implemented (Phases 1 – 6)

1. **Health Check Endpoint:** `GET /api/health/`
   - Returns service health status and CORS headers for frontend consumption.
2. **Truck Route Alternatives:** `POST /api/routes/`
   - Accepts origin, destination, departure datetime (ISO 8601), cargo load weight in pounds (`load_weight_lb`), and sampling interval (`interval_miles`: 10, 25, 50; default 25).
   - Validates coordinates and resolves geographical ambiguity via TomTom Geocoding API v2.
   - Calculates up to 3 genuine alternative truck routes using TomTom Maps Routing API v1 with vehicle travel mode `truck`.
   - Inspects route sections for non-truck segments (`sectionType == "TRAVEL_MODE"` and `travelMode == "other"`).
3. **Checkpoint Generation & Progress Mapping:**
   - Single-pass cumulative Haversine distance walking ($O(N + M)$) along multi-leg geometry.
   - Maps TomTom's sparse progress markers onto the flattened road polyline.
   - Generates checkpoints at Mile 0 (origin), every `interval_miles` increment, and the exact destination.
   - Handles edge-case partial segments without skipping sampling milestones or duplicating endpoints.
4. **Timezone-Aware ETA Interpolation:**
   - Interpolates travel time between surrounding progress markers based on actual road curvature and distance.
   - Preserves origin-local timezone offsets in ISO 8601 strings and generates accurate UTC Unix timestamps (`eta_unix`).
   - Ensures monotonic checkpoint arrival times matching the provider arrival instant.
5. **Progress Fallback:**
   - Deterministically falls back to linear interpolation if provider progress is omitted or unusable, flagging `"progress_source": "interpolated_fallback"` with route warnings.
6. **Open-Meteo Hourly Weather Integration (Phase 5):**
   - Batched multi-location hourly weather fetching across all route checkpoints (max 50 unique coordinates per request batch).
   - Deduplicates coordinates across overlapping routes to ~11m precision (4 decimal places).
   - Nearest-hour timestamp matching ($\le 30$ min window) with conservative upward tie-breaking.
   - Standardized checkpoint weather payloads (`available`, `partial`, `unavailable`) with wind speed (mph), rain (in), and snowfall (in).
7. **Weather Risk Scoring & Safe Route Recommendation (Phase 6):**
   - Exact 5-tier environmental hazard scale: `Low < Moderate < High < Severe < No Travel`.
   - Cargo load-dependent wind overrides (>30,000 lb and >40,000 lb) applied before overall checkpoint risk determination.
   - Route-level segment distance aggregation (`low_miles`, `moderate_miles`, `high_miles`, `severe_miles`, `no_travel_miles`, `unassessed_miles`).
   - Distance-weighted average risk calculation (`Low=1, Moderate=2, High=3, Severe=4`, strictly `null` if any `no_travel_miles` or `unassessed_miles` exist).
   - Hierarchical route recommendation: Fewest Severe miles $\to$ Fewest High miles $\to$ Lowest Average Risk $\to$ Shortest Travel Time $\to$ Provider Route Order.
   - Dynamic, factual recommendation explanation attributing the exact ranking criterion or tie-breaker that selected the winning route.
   - Accurate safety status semantics: `"recommended"` (with incomplete coverage notice if alternative routes lack data), `"no_safe_route"` (`"No safe route available under current weather conditions."`), or `"incomplete_weather"`.

---

## 3. Weather Data Attribution & Integration Details

Weather forecasts are provided by [Open-Meteo](https://open-meteo.com/):
> **Weather data by Open-Meteo.com** ([https://open-meteo.com/](https://open-meteo.com/)), used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

- **Variables & Units:** `wind_speed_10m` (mph), `rain` (inches), `snowfall` (inches).
- **Time Format:** UTC Unix timestamps (`timeformat=unixtime`).
- **Forecast Coverage:** 7-day hourly horizon (`forecast_days=7`).
- **Matching Tolerance:** Checkpoint ETA is matched against hourly forecast within a 30-minute ($\pm 1800$s) window; ties at exactly 30 minutes round upward to the later hour.
- **Licensing Notice:** Non-commercial use does not require an API key; commercial deployment requires an Open-Meteo commercial subscription or self-hosted instance.

---

## 4. Risk Engine & Recommendation Specification

### Environmental Hazard Breakpoints

| Hazard | Low | Moderate | High | Severe | No Travel |
|:---|:---|:---|:---|:---|:---|
| **Wind** | $< 25\,\text{mph}$ | $25 \le \text{wind} < 35\,\text{mph}$ | $35 \le \text{wind} < 45\,\text{mph}$ | $45 \le \text{wind} < 55\,\text{mph}$ | $\ge 55\,\text{mph}$ |
| **Rain** | $< 0.10\,\text{in/hr}$ | $0.10 \le \text{rain} \le 0.25\,\text{in/hr}$ | $0.25 < \text{rain} \le 0.50\,\text{in/hr}$ | $0.50 < \text{rain} \le 1.00\,\text{in/hr}$ | $> 1.00\,\text{in/hr}$ |
| **Snowfall** | $< 0.5\,\text{in/hr}$ | $0.5 \le \text{snow} \le 1.0\,\text{in/hr}$ | $1.0 < \text{snow} \le 2.0\,\text{in/hr}$ | $2.0 < \text{snow} \le 3.0\,\text{in/hr}$ | $> 3.0\,\text{in/hr}$ |

### Cargo Weight Overrides (Wind)
1. **Wind $\ge 55\,\text{mph}$:** `No Travel`, regardless of cargo load.
2. **Wind $45 \le \text{wind} < 55\,\text{mph}$ AND cargo weight $> 30,000\,\text{lb}$:** Overridden to `No Travel`.
3. **Wind $35 \le \text{wind} < 45\,\text{mph}$ AND cargo weight $> 40,000\,\text{lb}$:** Overridden to `Severe`.

### Distance-Weighted Average Risk
For routes that are fully assessed (`unassessed_miles == 0.0`) and have zero `no_travel_miles`:
$$\text{average\_risk} = \frac{\sum (\text{segment\_miles} \times \text{weight})}{\text{total\_route\_miles}}$$
Weights: $\text{Low} = 1$, $\text{Moderate} = 2$, $\text{High} = 3$, $\text{Severe} = 4$.  
*If any `no_travel_miles > 0.0` or `unassessed_miles > 0.0`, `average_risk` is returned strictly as `null`.*

---

## 5. API Response Contract & Example

`POST /api/routes/` returns:
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

### Recommendation Status Values:
- `"recommended"`: A fully assessed safe route was selected using the ranking hierarchy.
- `"no_safe_route"`: All alternative routes were fully assessed, but every route contains `no_travel_miles > 0.0`. Exact message: `"No safe route available under current weather conditions."`
- `"incomplete_weather"`: No safe route could be confirmed because one or more routes had incomplete weather forecast data. Exact message: `"Route safety could not be fully assessed because weather data is missing."`

---

## 6. Project Structure

```text
backend/
├── config/
│   ├── __init__.py
│   ├── asgi.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── routes/
│   ├── __init__.py
│   ├── apps.py
│   ├── risk.py
│   ├── services.py
│   ├── tests.py
│   ├── urls.py
│   ├── utils.py
│   └── views.py
├── .env.example
├── .gitignore
├── manage.py
├── README.md
└── requirements.txt
```

---

## 7. Getting Started

### Prerequisites

- Python 3.11 or higher
- A TomTom Developer API Key

### Installation

1. Create and activate a Python virtual environment:
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # Linux / macOS:
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Set up environment variables (for live application execution):
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and supply your TomTom API key:
   ```ini
   TOMTOM_API_KEY=your_actual_tomtom_api_key
   OPEN_METEO_BASE_URL=https://api.open-meteo.com/v1/forecast
   DJANGO_SECRET_KEY=your-secure-secret-key
   DJANGO_DEBUG=True
   DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
   FRONTEND_URL=http://localhost:5173
   ```
   > **Note:** A valid `TOMTOM_API_KEY` is required only for live application operation (`runserver`). The automated test suite is completely independent of local `.env` configuration and does not require a real API key or `.env` file.

---

## 8. Running the Backend (Live Application)

Start the Django development server (requires `.env` with a valid `TOMTOM_API_KEY`):
```bash
python manage.py runserver 127.0.0.1:8000
```

Verify service health:
```bash
curl http://127.0.0.1:8000/api/health/
# {"status":"ok"}
```

---

## 9. Running Automated Tests

Run the complete test suite (110 automated tests covering routing, progress mapping, checkpoint sampling, Open-Meteo weather integration, risk scoring, distance aggregation, and safe route recommendation):
```bash
python manage.py test routes
```

**Test Isolation & Offline Execution:**
- **No `.env` file required:** The test suite uses Django's `@override_settings` for endpoint configuration and executes out of the box in fresh clones.
- **Zero external network calls:** All outbound requests to TomTom Maps and Open-Meteo APIs are mocked using `unittest.mock.patch`.
- **No real credentials needed:** Tests execute completely offline without live provider keys.

Run Django system check:
```bash
python manage.py check
```

---

## 10. Current Implementation Status & Roadmap

- **Completed:**
  - Phase 1: DRF Foundation, CORS configuration, Health endpoint
  - Phase 2 & 3: TomTom Maps Routing API v1 truck calculation & Geocoding API v2
  - Phase 4 & 4.5: Checkpoint generation, ETA interpolation, and sub-tenth mile edge-case audit
  - Phase 5 & 5.5: Open-Meteo hourly weather forecasting integration & timezone audit
  - Phase 6, 6.1, 6.2 & 6.3: Weather risk assessment, cargo overrides, 2-decimal distance aggregation & raw-mileage safety eligibility
- **Pending (Future Phases):**
  - Phase 7: Trip Planning Input Form (Frontend)
  - Phase 8: Interactive Map, Routes & Checkpoint Markers (Frontend)
  - Phase 9: Corridor Heatmap & 48-Hour Forecast Slider (Frontend)
