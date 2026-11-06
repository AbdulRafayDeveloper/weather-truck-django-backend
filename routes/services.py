import bisect
import time
from datetime import datetime, timezone, timedelta
import requests
from django.conf import settings
from .utils import (
    meters_to_miles,
    seconds_to_minutes,
    haversine_distance,
    interpolate_coordinate,
    METERS_PER_MILE,
)
from .risk import process_route_risks


class RouteServiceError(Exception):
    """Domain exception representing routing and geocoding failures."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def geocode_location(query: str, api_key: str) -> dict:
    """Geocode a location string to latitude/longitude using TomTom Geocoding API v2."""
    url = f"https://api.tomtom.com/search/2/geocode/{query}.json"
    params = {'key': api_key, 'limit': 3}

    try:
        response = requests.get(url, params=params, timeout=10)
    except requests.Timeout:
        raise RouteServiceError(
            'PROVIDER_TIMEOUT',
            f'Geocoding request for "{query}" timed out.',
            status_code=504,
        )
    except requests.RequestException:
        raise RouteServiceError(
            'PROVIDER_ERROR',
            'Failed to connect to geocoding service.',
            status_code=502,
        )

    if response.status_code in (401, 403):
        raise RouteServiceError(
            'PROVIDER_UNAUTHORIZED',
            'TomTom API key is invalid or unauthorized.',
            status_code=502,
        )

    if response.status_code >= 400:
        raise RouteServiceError(
            'PROVIDER_ERROR',
            'Geocoding service returned an error.',
            status_code=502,
        )

    try:
        data = response.json()
    except ValueError:
        raise RouteServiceError(
            'PROVIDER_ERROR',
            'Malformed response from geocoding service.',
            status_code=502,
        )

    results = data.get('results', [])
    if not results:
        raise RouteServiceError(
            'GEOCODE_NOT_FOUND',
            f'Could not find coordinates for location: {query}',
            status_code=404,
        )

    # Detect genuine ambiguity among multiple top results
    if len(results) > 1:
        score0 = float(results[0].get('score', results[0].get('matchConfidence', {}).get('score', 1.0)))
        score1 = float(results[1].get('score', results[1].get('matchConfidence', {}).get('score', 0.0)))
        pos0 = results[0].get('position', {})
        pos1 = results[1].get('position', {})
        if pos0 and pos1 and 'lat' in pos0 and 'lon' in pos0 and 'lat' in pos1 and 'lon' in pos1:
            dist_meters = haversine_distance(
                {'latitude': pos0['lat'], 'longitude': pos0['lon']},
                {'latitude': pos1['lat'], 'longitude': pos1['lon']},
            )
            score_diff_ratio = abs(score0 - score1) / max(score0, 0.001) if score0 > 0 else 0
            # If top two candidates have near-identical match scores (within 2%) and are far apart (>25km)
            if score_diff_ratio < 0.02 and dist_meters > 25000:
                candidates = [
                    r.get('address', {}).get('freeformAddress') or 'Unknown'
                    for r in results[:3]
                ]
                raise RouteServiceError(
                    'LOCATION_AMBIGUOUS',
                    f'Location query "{query}" is ambiguous. Did you mean: {", ".join(candidates)}?',
                    status_code=400,
                )

    best_match = results[0]
    position = best_match.get('position')
    if not position or 'lat' not in position or 'lon' not in position:
        raise RouteServiceError(
            'GEOCODE_NOT_FOUND',
            f'Coordinates unavailable for location: {query}',
            status_code=404,
        )

    return {
        'latitude': round(float(position['lat']), 5),
        'longitude': round(float(position['lon']), 5),
    }


def fetch_tomtom_routes(
    origin_coords: dict,
    dest_coords: dict,
    departure: str,
    api_key: str,
) -> dict:
    """Request truck route alternatives from TomTom Maps Routing API v1."""
    orig = f"{origin_coords['latitude']},{origin_coords['longitude']}"
    dest = f"{dest_coords['latitude']},{dest_coords['longitude']}"
    url = f"https://api.tomtom.com/routing/1/calculateRoute/{orig}:{dest}/json"

    # Send repeated query parameters as a list of tuples to avoid dictionary key collision
    params = [
        ('key', api_key),
        ('travelMode', 'truck'),
        ('maxAlternatives', 2),
        ('departAt', departure),
        ('routeRepresentation', 'polyline'),
        ('sectionType', 'travelMode'),
        ('extendedRouteRepresentation', 'distance'),
        ('extendedRouteRepresentation', 'travelTime'),
    ]

    try:
        response = requests.get(url, params=params, timeout=20)
    except requests.Timeout:
        raise RouteServiceError(
            'PROVIDER_TIMEOUT',
            'Routing provider request timed out.',
            status_code=504,
        )
    except requests.RequestException:
        raise RouteServiceError(
            'PROVIDER_ERROR',
            'Failed to connect to routing provider.',
            status_code=502,
        )

    if response.status_code in (401, 403):
        raise RouteServiceError(
            'PROVIDER_UNAUTHORIZED',
            'TomTom API key is invalid or unauthorized.',
            status_code=502,
        )

    try:
        data = response.json()
    except ValueError:
        raise RouteServiceError(
            'PROVIDER_ERROR',
            'Malformed response from routing provider.',
            status_code=502,
        )

    if response.status_code == 404:
        raise RouteServiceError(
            'ROUTE_NOT_FOUND',
            'No route was found for the selected locations.',
            status_code=404,
        )

    if response.status_code >= 400:
        error_msg = data.get('error', {}).get('description') or data.get('detailedError', {}).get('message') or data.get('message') or 'Routing provider returned an error.'
        if 'noroute' in str(error_msg).lower() or 'not found' in str(error_msg).lower():
            raise RouteServiceError(
                'ROUTE_NOT_FOUND',
                'No route was found for the selected locations.',
                status_code=404,
            )
        # Redact API key if echoed by provider error
        safe_msg = str(error_msg).replace(api_key, '[REDACTED]') if api_key else str(error_msg)
        raise RouteServiceError(
            'PROVIDER_ERROR',
            f'Routing provider error: {safe_msg}',
            status_code=502,
        )

    return data


def process_progress_markers(
    raw_progress: list,
    geom_len: int,
    total_distance_m: float,
    total_time_s: int,
) -> tuple:
    """
    Validate, filter, and sort progress markers.
    Returns (cleaned_markers, progress_source, warnings).
    """
    warnings = []
    if geom_len <= 0:
        return [], 'interpolated_fallback', warnings

    valid_candidates = []
    if isinstance(raw_progress, list):
        for p in raw_progress:
            if not isinstance(p, dict):
                continue
            idx = p.get('pointIndex')
            dist = p.get('distanceInMeters')
            time_s = p.get('travelTimeInSeconds')

            # Validate types, bounds, and non-negative values
            if (
                idx is not None
                and isinstance(idx, int)
                and 0 <= idx < geom_len
                and dist is not None
                and isinstance(dist, (int, float))
                and dist >= 0
                and time_s is not None
                and isinstance(time_s, (int, float))
                and time_s >= 0
            ):
                valid_candidates.append({
                    'point_index': idx,
                    'distance_meters': float(dist),
                    'travel_time_seconds': int(time_s),
                })

    # Sort by point_index
    valid_candidates.sort(key=lambda m: m['point_index'])

    # Handle duplicate point_index: keep first consistent marker per pointIndex
    deduped = []
    seen_indices = set()
    for m in valid_candidates:
        if m['point_index'] not in seen_indices:
            deduped.append(m)
            seen_indices.add(m['point_index'])

    # Check cumulative distance and travel time monotonicity
    monotonic = []
    last_dist = -1.0
    last_time = -1
    for m in deduped:
        if m['distance_meters'] >= last_dist and m['travel_time_seconds'] >= last_time:
            monotonic.append(m)
            last_dist = m['distance_meters']
            last_time = m['travel_time_seconds']

    had_valid_intermediate = any(
        0 < m['point_index'] < geom_len - 1 for m in monotonic
    )

    if not monotonic:
        progress_source = 'interpolated_fallback'
        markers = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {
                'point_index': geom_len - 1,
                'distance_meters': float(total_distance_m),
                'travel_time_seconds': int(total_time_s),
            },
        ]
        warnings.append(
            'Progress markers not provided by routing service; checkpoint ETAs will be estimated via linear approximation.'
        )
    else:
        # Check start boundary
        if monotonic[0]['point_index'] != 0:
            monotonic.insert(0, {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0})
        else:
            monotonic[0]['distance_meters'] = 0.0
            monotonic[0]['travel_time_seconds'] = 0

        # Check end boundary
        if monotonic[-1]['point_index'] != geom_len - 1:
            monotonic.append({
                'point_index': geom_len - 1,
                'distance_meters': float(total_distance_m),
                'travel_time_seconds': int(total_time_s),
            })
        else:
            monotonic[-1]['distance_meters'] = float(total_distance_m)
            monotonic[-1]['travel_time_seconds'] = int(total_time_s)

        # Enforce end-boundary monotonicity
        for i in range(1, len(monotonic)):
            if monotonic[i]['distance_meters'] < monotonic[i - 1]['distance_meters']:
                monotonic[i]['distance_meters'] = monotonic[i - 1]['distance_meters']
            if monotonic[i]['travel_time_seconds'] < monotonic[i - 1]['travel_time_seconds']:
                monotonic[i]['travel_time_seconds'] = monotonic[i - 1]['travel_time_seconds']

        if bool(raw_progress) and (had_valid_intermediate or len(raw_progress) >= 2):
            progress_source = 'provider'
        else:
            progress_source = 'interpolated_fallback'
            warnings.append(
                'Progress markers not provided by routing service; checkpoint ETAs will be estimated via linear approximation.'
            )
        markers = monotonic

    return markers, progress_source, warnings


def generate_checkpoints(
    geometry: list,
    progress_markers: list,
    total_distance_m: float,
    total_time_s: int,
    departure_str: str,
    interval_miles: int = 25,
    total_miles: float = None,
) -> list:
    """
    Generate geographically accurate checkpoints along route geometry
    and interpolate estimated truck arrival times (ETA).
    """
    if not geometry:
        return []

    # Parse departure time into timezone-aware datetime
    dep_dt = None
    if departure_str:
        try:
            dep_dt = datetime.fromisoformat(departure_str.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            dep_dt = None
    if dep_dt is None:
        dep_dt = datetime.now(timezone.utc)
    if dep_dt.tzinfo is None:
        dep_dt = dep_dt.replace(tzinfo=timezone.utc)

    if total_miles is None:
        raw_miles = total_distance_m / METERS_PER_MILE
        total_miles = round(raw_miles, 2) if round(raw_miles, 1) != round(raw_miles, 2) else round(raw_miles, 1)

    N = len(geometry)

    if N == 1 or total_miles <= 0.0:
        return [{
            'id': 1,
            'latitude': geometry[0]['latitude'],
            'longitude': geometry[0]['longitude'],
            'mile_marker': 0.0,
            'segment_miles': 0.0,
            'eta': dep_dt.isoformat(),
            'eta_unix': int(dep_dt.timestamp()),
        }]

    # 1. Calculate cumulative Haversine distances along geometry
    geom_dists = [0.0] * N
    for i in range(1, N):
        geom_dists[i] = geom_dists[i - 1] + haversine_distance(geometry[i - 1], geometry[i])

    # 2. Map progress markers onto every geometry point
    route_dist = [0.0] * N
    route_time = [0.0] * N

    num_markers = len(progress_markers)
    for m in range(num_markers - 1):
        m_curr = progress_markers[m]
        m_next = progress_markers[m + 1]
        start_idx = m_curr['point_index']
        end_idx = m_next['point_index']

        m_geom_span = geom_dists[end_idx] - geom_dists[start_idx]
        dist_span = m_next['distance_meters'] - m_curr['distance_meters']
        time_span = m_next['travel_time_seconds'] - m_curr['travel_time_seconds']

        for j in range(start_idx, end_idx + 1):
            if m_geom_span > 0:
                frac = (geom_dists[j] - geom_dists[start_idx]) / m_geom_span
            else:
                idx_span = end_idx - start_idx
                frac = (j - start_idx) / idx_span if idx_span > 0 else 0.0
            frac = max(0.0, min(1.0, frac))
            route_dist[j] = m_curr['distance_meters'] + frac * dist_span
            route_time[j] = m_curr['travel_time_seconds'] + frac * time_span

    # 3. Construct target mile markers: 0, interval, 2*interval, ..., total_miles
    # Use a small floating-point epsilon independent of interval_miles
    EPSILON = 1e-5
    target_miles_list = []
    step = 0
    while True:
        m = step * interval_miles
        if m > total_miles + EPSILON:
            break
        if abs(m - total_miles) <= EPSILON:
            target_miles_list.append(total_miles)
            break
        target_miles_list.append(round(m, 1))
        step += 1

    if not target_miles_list or abs(target_miles_list[-1] - total_miles) > EPSILON:
        target_miles_list.append(total_miles)

    # 4. Generate checkpoints by walking the geometry
    checkpoints = []
    geom_idx = 0
    prev_mile = 0.0
    prev_time_s = 0.0

    for cp_id, target_mile in enumerate(target_miles_list, start=1):
        target_m = target_mile * METERS_PER_MILE

        if cp_id == 1:
            coord = geometry[0]
            time_s = 0.0
            segment_miles = 0.0
        elif target_mile >= total_miles:
            coord = geometry[-1]
            time_s = float(total_time_s)
            diff = target_mile - prev_mile
            segment_miles = round(diff, 2) if round(diff, 1) != round(diff, 2) else round(diff, 1)
        else:
            while geom_idx + 1 < N and route_dist[geom_idx + 1] < target_m:
                geom_idx += 1

            d1 = route_dist[geom_idx]
            d2 = route_dist[min(geom_idx + 1, N - 1)]

            if d2 > d1:
                frac = (target_m - d1) / (d2 - d1)
                frac = max(0.0, min(1.0, frac))
            else:
                frac = 0.0

            pt1 = geometry[geom_idx]
            pt2 = geometry[min(geom_idx + 1, N - 1)]
            coord = interpolate_coordinate(pt1, pt2, frac)

            t1 = route_time[geom_idx]
            t2 = route_time[min(geom_idx + 1, N - 1)]
            time_s = t1 + frac * (t2 - t1)
            diff = target_mile - prev_mile
            segment_miles = round(diff, 2) if round(diff, 1) != round(diff, 2) else round(diff, 1)

        time_s = max(time_s, prev_time_s)
        prev_time_s = time_s
        prev_mile = target_mile

        eta_dt = dep_dt + timedelta(seconds=round(time_s))
        marker_val = round(float(target_mile), 2) if round(target_mile, 1) != round(target_mile, 2) else round(float(target_mile), 1)

        checkpoints.append({
            'id': cp_id,
            'latitude': coord['latitude'],
            'longitude': coord['longitude'],
            'mile_marker': marker_val,
            'segment_miles': segment_miles,
            'eta': eta_dt.isoformat(),
            'eta_unix': int(eta_dt.timestamp()),
        })

    return checkpoints


def normalize_routes(
    raw_data: dict,
    origin_coords: dict,
    dest_coords: dict,
    departure: str = None,
    interval_miles: int = 25,
) -> list:
    """Normalize raw TomTom routing response into application route contract."""
    raw_routes = raw_data.get('routes')
    if not raw_routes or not isinstance(raw_routes, list):
        raise RouteServiceError(
            'ROUTE_NOT_FOUND',
            'No route was found for the selected locations.',
            status_code=404,
        )

    normalized = []
    # Up to 3 real alternatives returned by TomTom
    for index, route in enumerate(raw_routes[:3], start=1):
        summary = route.get('summary', {})
        length_meters = summary.get('lengthInMeters', 0)
        travel_time_seconds = summary.get('travelTimeInSeconds', 0)
        departure_time = summary.get('departureTime') or departure
        arrival_time = summary.get('arrivalTime')

        # Read geometry from routes[].legs[].points
        # In TomTom Routing API v1, progress[].pointIndex references the flattened
        # sequence of points across all legs. Preserving the exact flattened geometry
        # ensures pointIndex maps 1:1 without index shifting.
        geometry = []
        for leg in route.get('legs', []):
            for pt in leg.get('points', []):
                geometry.append({
                    'latitude': round(float(pt['latitude']), 5),
                    'longitude': round(float(pt['longitude']), 5),
                })

        geom_len = len(geometry)

        # Process and clean progress markers
        progress_markers, progress_source, progress_warnings = process_progress_markers(
            route.get('progress', []),
            geom_len,
            length_meters,
            travel_time_seconds,
        )

        # Inspect route sections for non-truck segments (sectionType == "TRAVEL_MODE" and travelMode == "other")
        has_non_truck_sections = False
        truck_warnings = []
        for section in route.get('sections', []):
            stype = str(section.get('sectionType', '')).upper()
            tmode = str(section.get('travelMode', '')).lower()
            if stype == 'TRAVEL_MODE' and tmode == 'other':
                has_non_truck_sections = True
                start_idx = section.get('startPointIndex', 0)
                end_idx = section.get('endPointIndex', 0)
                truck_warnings.append(
                    f'Section from point {start_idx} to {end_idx} is not officially designated for trucks (travelMode: other).'
                )

        warnings = progress_warnings + truck_warnings
        distance_miles = meters_to_miles(length_meters)

        # Generate checkpoints and interpolate ETAs
        checkpoints = generate_checkpoints(
            geometry=geometry,
            progress_markers=progress_markers,
            total_distance_m=length_meters,
            total_time_s=travel_time_seconds,
            departure_str=departure_time,
            interval_miles=interval_miles,
            total_miles=distance_miles,
        )

        normalized.append({
            'id': index,
            'distance_miles': distance_miles,
            'duration_seconds': travel_time_seconds,
            'duration_minutes': seconds_to_minutes(travel_time_seconds),
            'departure': departure_time,
            'arrival': arrival_time,
            'origin': origin_coords,
            'destination': dest_coords,
            'geometry': geometry,
            'progress': progress_markers,
            'progress_source': progress_source,
            'has_non_truck_sections': has_non_truck_sections,
            'warnings': warnings,
            'interval_miles': interval_miles,
            'checkpoint_count': len(checkpoints),
            'checkpoints': checkpoints,
        })

    if not normalized:
        raise RouteServiceError(
            'ROUTE_NOT_FOUND',
            'No route was found for the selected locations.',
            status_code=404,
        )

    return normalized


def get_route_alternatives(
    origin: str,
    destination: str,
    departure: str,
    load_weight_lb: float,
    interval_miles: int = 25,
) -> list:
    """Coordinate TomTom geocoding, route calculation, and response normalization."""
    api_key = getattr(settings, 'TOMTOM_API_KEY', '')
    if not api_key:
        raise RouteServiceError(
            'CONFIGURATION_ERROR',
            'TomTom API key is not configured on the server.',
            status_code=500,
        )

    origin_coords = geocode_location(origin, api_key)
    dest_coords = geocode_location(destination, api_key)

    # Note: load_weight_lb is validated and preserved for Phase 4-6 risk scoring,
    # and is deliberately NOT passed to TomTom as vehicle gross weight.
    raw_routes = fetch_tomtom_routes(
        origin_coords=origin_coords,
        dest_coords=dest_coords,
        departure=departure,
        api_key=api_key,
    )

    routes = normalize_routes(
        raw_routes,
        origin_coords,
        dest_coords,
        departure=departure,
        interval_miles=interval_miles,
    )
    routes = attach_weather_to_routes(routes)
    return process_route_risks(routes, load_weight_lb=load_weight_lb)


def fetch_open_meteo_batch(
    coords: list,
    base_url: str,
    max_retries: int = 2,
) -> list:
    """
    Fetch hourly weather forecast for a batch of coordinates from Open-Meteo.
    Handles transient 429/5xx errors with bounded backoff.
    """
    if not coords:
        return []

    lat_str = ",".join(f"{c[0]:.4f}" for c in coords)
    lon_str = ",".join(f"{c[1]:.4f}" for c in coords)

    params = {
        'latitude': lat_str,
        'longitude': lon_str,
        'hourly': 'wind_speed_10m,rain,snowfall',
        'wind_speed_unit': 'mph',
        'precipitation_unit': 'inch',
        'timeformat': 'unixtime',
        'forecast_days': 7,
    }

    for attempt in range(max_retries + 1):
        try:
            response = requests.get(base_url, params=params, timeout=15)
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    raise RouteServiceError(
                        'PROVIDER_ERROR',
                        'Malformed JSON response from weather service.',
                        502,
                    )

                if isinstance(data, list):
                    results = data
                elif isinstance(data, dict):
                    results = [data]
                else:
                    raise RouteServiceError(
                        'PROVIDER_ERROR',
                        'Unexpected response structure from weather service.',
                        502,
                    )

                if len(results) != len(coords):
                    raise RouteServiceError(
                        'PROVIDER_ERROR',
                        f'Weather provider returned {len(results)} locations, expected {len(coords)}.',
                        502,
                    )
                return results

            if response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue

            raise RouteServiceError(
                'PROVIDER_ERROR',
                f'Weather service returned error status {response.status_code}.',
                status_code=502 if response.status_code >= 500 or response.status_code == 429 else 400,
            )

        except requests.Timeout:
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RouteServiceError(
                'PROVIDER_TIMEOUT',
                'Weather service request timed out.',
                status_code=504,
            )
        except requests.RequestException:
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RouteServiceError(
                'PROVIDER_ERROR',
                'Failed to connect to weather forecast service.',
                status_code=502,
            )


def parse_weather_hourly(location_data: dict) -> dict:
    """
    Validate and extract hourly weather arrays from an Open-Meteo location object.
    Returns dict with validated time, wind_speed, rain, and snowfall arrays.
    """
    if not isinstance(location_data, dict):
        return None

    hourly = location_data.get('hourly')
    if not isinstance(hourly, dict):
        return None

    time_arr = hourly.get('time')
    wind_arr = hourly.get('wind_speed_10m')
    rain_arr = hourly.get('rain')
    snow_arr = hourly.get('snowfall')

    if not (
        isinstance(time_arr, list)
        and isinstance(wind_arr, list)
        and isinstance(rain_arr, list)
        and isinstance(snow_arr, list)
    ):
        return None

    n = len(time_arr)
    if not (n > 0 and len(wind_arr) == n and len(rain_arr) == n and len(snow_arr) == n):
        return None

    # Validate timestamps are non-decreasing integers
    for i in range(n):
        if not isinstance(time_arr[i], int):
            return None
        if i > 0 and time_arr[i] <= time_arr[i - 1]:
            return None

    return {
        'time': time_arr,
        'wind_speed_10m': wind_arr,
        'rain': rain_arr,
        'snowfall': snow_arr,
    }


def match_checkpoint_weather(eta_unix: int, hourly_data: dict) -> dict:
    """
    Match checkpoint ETA with the nearest forecast hour (within 30 minutes / 1800s).
    Tie-breaking: on exact 30-minute boundary, chooses the later hour.
    """
    if not hourly_data or not isinstance(hourly_data.get('time'), list) or not hourly_data['time']:
        return {
            'status': 'unavailable',
            'wind_speed_mph': None,
            'rain_in': None,
            'snowfall_in': None,
            'reason': 'No valid forecast hour is available for this checkpoint ETA.',
        }

    times = hourly_data['time']
    n = len(times)

    # Use bisect to find candidate indices
    idx = bisect.bisect_left(times, eta_unix)

    candidates = []
    if idx > 0:
        candidates.append(idx - 1)
    if idx < n:
        candidates.append(idx)

    if not candidates:
        return {
            'status': 'unavailable',
            'wind_speed_mph': None,
            'rain_in': None,
            'snowfall_in': None,
            'reason': 'No valid forecast hour is available for this checkpoint ETA.',
        }

    # Sort candidates by (abs(time - eta_unix), -time)
    # This selects the nearest timestamp, and on an exact tie, selects the later timestamp.
    best_idx = min(candidates, key=lambda i: (abs(times[i] - eta_unix), -times[i]))
    chosen_time = times[best_idx]
    offset_seconds = chosen_time - eta_unix

    if abs(offset_seconds) > 1800:
        return {
            'status': 'unavailable',
            'wind_speed_mph': None,
            'rain_in': None,
            'snowfall_in': None,
            'reason': 'No valid forecast hour is available for this checkpoint ETA.',
        }

    wind = hourly_data['wind_speed_10m'][best_idx]
    rain = hourly_data['rain'][best_idx]
    snow = hourly_data['snowfall'][best_idx]

    # Validate numbers or nulls
    missing = []
    if wind is None:
        missing.append('wind_speed_mph')
    elif not isinstance(wind, (int, float)):
        wind = None
        missing.append('wind_speed_mph')

    if rain is None:
        missing.append('rain_in')
    elif not isinstance(rain, (int, float)):
        rain = None
        missing.append('rain_in')

    if snow is None:
        missing.append('snowfall_in')
    elif not isinstance(snow, (int, float)):
        snow = None
        missing.append('snowfall_in')

    if missing:
        return {
            'status': 'partial',
            'wind_speed_mph': round(float(wind), 2) if wind is not None else None,
            'rain_in': round(float(rain), 3) if rain is not None else None,
            'snowfall_in': round(float(snow), 3) if snow is not None else None,
            'forecast_time_unix': chosen_time,
            'forecast_match_offset_seconds': offset_seconds,
            'missing_fields': missing,
        }

    return {
        'status': 'available',
        'wind_speed_mph': round(float(wind), 2),
        'rain_in': round(float(rain), 3),
        'snowfall_in': round(float(snow), 3),
        'forecast_time_unix': chosen_time,
        'forecast_match_offset_seconds': offset_seconds,
    }


def attach_weather_to_routes(routes: list, base_url: str = None) -> list:
    """
    Collect all unique checkpoint coordinates across all routes,
    fetch forecast data in batches from Open-Meteo,
    and attach matched weather data to each checkpoint.
    """
    if not routes:
        return routes

    if base_url is None:
        base_url = getattr(
            settings,
            'OPEN_METEO_BASE_URL',
            'https://api.open-meteo.com/v1/forecast',
        )

    # 1. Collect unique coordinates across all routes
    # Round to 4 decimal places (~11 meters) for weather-grid deduplication
    coord_keys = []
    seen = set()

    for route in routes:
        for cp in route.get('checkpoints', []):
            lat = cp.get('latitude')
            lon = cp.get('longitude')
            if lat is not None and lon is not None:
                key = (round(float(lat), 4), round(float(lon), 4))
                if key not in seen:
                    seen.add(key)
                    coord_keys.append(key)

    if not coord_keys:
        return routes

    # 2. Divide coordinates into batches (max 50 coordinates per batch)
    BATCH_SIZE = 50
    weather_by_coord = {}

    for i in range(0, len(coord_keys), BATCH_SIZE):
        batch = coord_keys[i:i + BATCH_SIZE]
        raw_results = fetch_open_meteo_batch(batch, base_url)
        for coord, loc_data in zip(batch, raw_results):
            weather_by_coord[coord] = parse_weather_hourly(loc_data)

    # 3. Attach weather to each checkpoint
    for route in routes:
        for cp in route.get('checkpoints', []):
            lat = cp.get('latitude')
            lon = cp.get('longitude')
            key = (round(float(lat), 4), round(float(lon), 4)) if lat is not None and lon is not None else None
            hourly = weather_by_coord.get(key)
            cp['weather'] = match_checkpoint_weather(cp.get('eta_unix', 0), hourly)

    return routes
