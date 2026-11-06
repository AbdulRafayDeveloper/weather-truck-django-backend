"""Routing, checkpoint and weather services backed by TomTom and Open-Meteo."""

import bisect
import time
from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings

from .risk import process_route_risks
from .utils import (
    METERS_PER_MILE,
    haversine_distance,
    interpolate_coordinate,
    meters_to_miles,
    seconds_to_minutes,
)

TOMTOM_GEOCODE_URL = 'https://api.tomtom.com/search/2/geocode/{query}.json'
TOMTOM_ROUTE_URL = 'https://api.tomtom.com/routing/1/calculateRoute/{origin}:{destination}/json'
DEFAULT_OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'

MAX_ROUTES = 3
WEATHER_BATCH_SIZE = 50
FORECAST_MATCH_WINDOW_S = 1800
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)

NO_PROGRESS_WARNING = (
    'Progress markers not provided by routing service; '
    'checkpoint ETAs will be estimated via linear approximation.'
)
NO_FORECAST_REASON = 'No valid forecast hour is available for this checkpoint ETA.'


class RouteServiceError(Exception):
    """An expected failure that maps to an API error response."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _route_not_found() -> RouteServiceError:
    return RouteServiceError(
        'ROUTE_NOT_FOUND',
        'No route was found for the selected locations.',
        status_code=404,
    )


def _round_miles(value: float) -> float:
    """Round to one decimal, keeping a second one only when it carries information."""
    return round(value, 2) if round(value, 1) != round(value, 2) else round(value, 1)


def _raise_if_ambiguous(query: str, results: list) -> None:
    """Reject queries whose top two matches score the same but are far apart."""
    if len(results) < 2:
        return

    first, second = results[0], results[1]
    score0 = float(first.get('score', first.get('matchConfidence', {}).get('score', 1.0)))
    score1 = float(second.get('score', second.get('matchConfidence', {}).get('score', 0.0)))
    pos0 = first.get('position', {})
    pos1 = second.get('position', {})

    if not (pos0 and pos1 and 'lat' in pos0 and 'lon' in pos0 and 'lat' in pos1 and 'lon' in pos1):
        return

    distance_m = haversine_distance(
        {'latitude': pos0['lat'], 'longitude': pos0['lon']},
        {'latitude': pos1['lat'], 'longitude': pos1['lon']},
    )
    score_gap = abs(score0 - score1) / max(score0, 0.001) if score0 > 0 else 0

    # e.g. "Springfield": several equally good matches in different states.
    if score_gap < 0.02 and distance_m > 25000:
        candidates = [r.get('address', {}).get('freeformAddress') or 'Unknown' for r in results[:3]]
        raise RouteServiceError(
            'LOCATION_AMBIGUOUS',
            f'Location query "{query}" is ambiguous. Did you mean: {", ".join(candidates)}?',
            status_code=400,
        )


def geocode_location(query: str, api_key: str) -> dict:
    """Resolve a place name to coordinates."""
    url = TOMTOM_GEOCODE_URL.format(query=query)
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

    _raise_if_ambiguous(query, results)

    position = results[0].get('position')
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


def fetch_tomtom_routes(origin_coords: dict, dest_coords: dict, departure: str, api_key: str) -> dict:
    """Request up to three truck routes from TomTom."""
    url = TOMTOM_ROUTE_URL.format(
        origin=f"{origin_coords['latitude']},{origin_coords['longitude']}",
        destination=f"{dest_coords['latitude']},{dest_coords['longitude']}",
    )
    # A list of tuples because extendedRouteRepresentation is sent twice.
    params = [
        ('key', api_key),
        ('travelMode', 'truck'),
        ('maxAlternatives', MAX_ROUTES - 1),
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
        raise _route_not_found()

    if response.status_code >= 400:
        error_msg = str(
            data.get('error', {}).get('description')
            or data.get('detailedError', {}).get('message')
            or data.get('message')
            or 'Routing provider returned an error.'
        )
        if 'noroute' in error_msg.lower() or 'not found' in error_msg.lower():
            raise _route_not_found()

        # Some provider errors echo the request URL, key included.
        if api_key:
            error_msg = error_msg.replace(api_key, '[REDACTED]')
        raise RouteServiceError(
            'PROVIDER_ERROR',
            f'Routing provider error: {error_msg}',
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
    Clean TomTom's progress markers into a sorted, monotonic list.

    Returns (markers, progress_source, warnings). Falls back to a straight
    start-to-end line when the provider data is missing or unusable.
    """
    warnings = []
    if geom_len <= 0:
        return [], 'interpolated_fallback', warnings

    candidates = []
    if isinstance(raw_progress, list):
        for p in raw_progress:
            if not isinstance(p, dict):
                continue
            idx = p.get('pointIndex')
            dist = p.get('distanceInMeters')
            time_s = p.get('travelTimeInSeconds')
            if (
                isinstance(idx, int) and 0 <= idx < geom_len
                and isinstance(dist, (int, float)) and dist >= 0
                and isinstance(time_s, (int, float)) and time_s >= 0
            ):
                candidates.append({
                    'point_index': idx,
                    'distance_meters': float(dist),
                    'travel_time_seconds': int(time_s),
                })

    candidates.sort(key=lambda m: m['point_index'])

    # Keep the first marker per point, then drop any that go backwards.
    markers = []
    seen = set()
    last_dist, last_time = -1.0, -1
    for m in candidates:
        if m['point_index'] in seen:
            continue
        seen.add(m['point_index'])
        if m['distance_meters'] >= last_dist and m['travel_time_seconds'] >= last_time:
            markers.append(m)
            last_dist, last_time = m['distance_meters'], m['travel_time_seconds']

    start = {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0}
    end = {
        'point_index': geom_len - 1,
        'distance_meters': float(total_distance_m),
        'travel_time_seconds': int(total_time_s),
    }

    if not markers:
        warnings.append(NO_PROGRESS_WARNING)
        return [start, end], 'interpolated_fallback', warnings

    has_intermediate = any(0 < m['point_index'] < geom_len - 1 for m in markers)

    # Pin both ends to the route's own totals.
    if markers[0]['point_index'] != 0:
        markers.insert(0, start)
    else:
        markers[0].update(distance_meters=0.0, travel_time_seconds=0)

    if markers[-1]['point_index'] != geom_len - 1:
        markers.append(end)
    else:
        markers[-1].update(distance_meters=end['distance_meters'], travel_time_seconds=end['travel_time_seconds'])

    # Pinning the end can break monotonicity, so clamp once more.
    for prev, curr in zip(markers, markers[1:]):
        curr['distance_meters'] = max(curr['distance_meters'], prev['distance_meters'])
        curr['travel_time_seconds'] = max(curr['travel_time_seconds'], prev['travel_time_seconds'])

    if raw_progress and (has_intermediate or len(raw_progress) >= 2):
        source = 'provider'
    else:
        source = 'interpolated_fallback'
        warnings.append(NO_PROGRESS_WARNING)

    return markers, source, warnings


def _parse_departure(departure_str: str) -> datetime:
    departure = None
    if departure_str:
        try:
            departure = datetime.fromisoformat(departure_str.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            departure = None
    if departure is None:
        departure = datetime.now(timezone.utc)
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=timezone.utc)
    return departure


def _map_progress_to_geometry(geometry: list, progress_markers: list) -> tuple:
    """
    Spread TomTom's sparse progress markers over every geometry point.

    Between two markers, distance and time are interpolated by how far along
    the polyline each point is, so curvy stretches get a fair share of time.
    """
    n = len(geometry)
    geom_dists = [0.0] * n
    for i in range(1, n):
        geom_dists[i] = geom_dists[i - 1] + haversine_distance(geometry[i - 1], geometry[i])

    route_dist = [0.0] * n
    route_time = [0.0] * n

    for curr, nxt in zip(progress_markers, progress_markers[1:]):
        start_idx, end_idx = curr['point_index'], nxt['point_index']
        geom_span = geom_dists[end_idx] - geom_dists[start_idx]
        dist_span = nxt['distance_meters'] - curr['distance_meters']
        time_span = nxt['travel_time_seconds'] - curr['travel_time_seconds']

        for j in range(start_idx, end_idx + 1):
            if geom_span > 0:
                frac = (geom_dists[j] - geom_dists[start_idx]) / geom_span
            else:
                idx_span = end_idx - start_idx
                frac = (j - start_idx) / idx_span if idx_span > 0 else 0.0
            frac = max(0.0, min(1.0, frac))
            route_dist[j] = curr['distance_meters'] + frac * dist_span
            route_time[j] = curr['travel_time_seconds'] + frac * time_span

    return route_dist, route_time


def _target_miles(total_miles: float, interval_miles: int) -> list:
    """Mile markers 0, interval, 2 * interval, ... ending exactly at the destination."""
    epsilon = 1e-5
    targets = []
    step = 0
    while True:
        mile = step * interval_miles
        if mile > total_miles + epsilon:
            break
        if abs(mile - total_miles) <= epsilon:
            targets.append(total_miles)
            break
        targets.append(round(mile, 1))
        step += 1

    if not targets or abs(targets[-1] - total_miles) > epsilon:
        targets.append(total_miles)
    return targets


def generate_checkpoints(
    geometry: list,
    progress_markers: list,
    total_distance_m: float,
    total_time_s: int,
    departure_str: str,
    interval_miles: int = 25,
    total_miles: float = None,
) -> list:
    """Place checkpoints along the route and estimate the truck's arrival time at each."""
    if not geometry:
        return []

    departure = _parse_departure(departure_str)

    if total_miles is None:
        total_miles = _round_miles(total_distance_m / METERS_PER_MILE)

    n = len(geometry)
    if n == 1 or total_miles <= 0.0:
        return [{
            'id': 1,
            'latitude': geometry[0]['latitude'],
            'longitude': geometry[0]['longitude'],
            'mile_marker': 0.0,
            'segment_miles': 0.0,
            'eta': departure.isoformat(),
            'eta_unix': int(departure.timestamp()),
        }]

    route_dist, route_time = _map_progress_to_geometry(geometry, progress_markers)

    checkpoints = []
    geom_idx = 0
    prev_mile = 0.0
    prev_time_s = 0.0

    for cp_id, target_mile in enumerate(_target_miles(total_miles, interval_miles), start=1):
        if cp_id == 1:
            coord = geometry[0]
            time_s = 0.0
            segment_miles = 0.0
        elif target_mile >= total_miles:
            coord = geometry[-1]
            time_s = float(total_time_s)
            segment_miles = _round_miles(target_mile - prev_mile)
        else:
            # Targets only move forward, so the geometry walk never restarts.
            target_m = target_mile * METERS_PER_MILE
            while geom_idx + 1 < n and route_dist[geom_idx + 1] < target_m:
                geom_idx += 1
            next_idx = min(geom_idx + 1, n - 1)

            d1, d2 = route_dist[geom_idx], route_dist[next_idx]
            frac = max(0.0, min(1.0, (target_m - d1) / (d2 - d1))) if d2 > d1 else 0.0

            coord = interpolate_coordinate(geometry[geom_idx], geometry[next_idx], frac)
            t1, t2 = route_time[geom_idx], route_time[next_idx]
            time_s = t1 + frac * (t2 - t1)
            segment_miles = _round_miles(target_mile - prev_mile)

        # ETAs must never go backwards.
        time_s = max(time_s, prev_time_s)
        prev_time_s = time_s
        prev_mile = target_mile

        eta = departure + timedelta(seconds=round(time_s))
        checkpoints.append({
            'id': cp_id,
            'latitude': coord['latitude'],
            'longitude': coord['longitude'],
            'mile_marker': _round_miles(float(target_mile)),
            'segment_miles': segment_miles,
            'eta': eta.isoformat(),
            'eta_unix': int(eta.timestamp()),
        })

    return checkpoints


def _flatten_geometry(route: dict) -> list:
    # TomTom's progress pointIndex counts across all legs, so the points must
    # be flattened in order without dropping any.
    return [
        {
            'latitude': round(float(pt['latitude']), 5),
            'longitude': round(float(pt['longitude']), 5),
        }
        for leg in route.get('legs', [])
        for pt in leg.get('points', [])
    ]


def _non_truck_warnings(route: dict) -> list:
    warnings = []
    for section in route.get('sections', []):
        section_type = str(section.get('sectionType', '')).upper()
        travel_mode = str(section.get('travelMode', '')).lower()
        if section_type == 'TRAVEL_MODE' and travel_mode == 'other':
            start_idx = section.get('startPointIndex', 0)
            end_idx = section.get('endPointIndex', 0)
            warnings.append(
                f'Section from point {start_idx} to {end_idx} is not officially '
                'designated for trucks (travelMode: other).'
            )
    return warnings


def normalize_routes(
    raw_data: dict,
    origin_coords: dict,
    dest_coords: dict,
    departure: str = None,
    interval_miles: int = 25,
) -> list:
    """Convert TomTom's response into the routes returned by the API."""
    raw_routes = raw_data.get('routes')
    if not raw_routes or not isinstance(raw_routes, list):
        raise _route_not_found()

    normalized = []
    for route_id, route in enumerate(raw_routes[:MAX_ROUTES], start=1):
        summary = route.get('summary', {})
        length_meters = summary.get('lengthInMeters', 0)
        travel_time_seconds = summary.get('travelTimeInSeconds', 0)
        departure_time = summary.get('departureTime') or departure

        geometry = _flatten_geometry(route)
        progress_markers, progress_source, progress_warnings = process_progress_markers(
            route.get('progress', []),
            len(geometry),
            length_meters,
            travel_time_seconds,
        )
        truck_warnings = _non_truck_warnings(route)
        distance_miles = meters_to_miles(length_meters)

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
            'id': route_id,
            'distance_miles': distance_miles,
            'duration_seconds': travel_time_seconds,
            'duration_minutes': seconds_to_minutes(travel_time_seconds),
            'departure': departure_time,
            'arrival': summary.get('arrivalTime'),
            'origin': origin_coords,
            'destination': dest_coords,
            'geometry': geometry,
            'progress': progress_markers,
            'progress_source': progress_source,
            'has_non_truck_sections': bool(truck_warnings),
            'warnings': progress_warnings + truck_warnings,
            'interval_miles': interval_miles,
            'checkpoint_count': len(checkpoints),
            'checkpoints': checkpoints,
        })

    if not normalized:
        raise _route_not_found()

    return normalized


def get_route_alternatives(
    origin: str,
    destination: str,
    departure: str,
    load_weight_lb: float,
    interval_miles: int = 25,
) -> tuple:
    """Run the full pipeline: geocode, route, checkpoints, weather, risk."""
    api_key = getattr(settings, 'TOMTOM_API_KEY', '')
    if not api_key:
        raise RouteServiceError(
            'CONFIGURATION_ERROR',
            'TomTom API key is not configured on the server.',
            status_code=500,
        )

    origin_coords = geocode_location(origin, api_key)
    dest_coords = geocode_location(destination, api_key)

    # Load weight is used only by our risk rules. It is not sent to TomTom
    # as vehicle weight, so all routes stay comparable.
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


def _backoff(attempt: int) -> None:
    time.sleep(0.5 * (attempt + 1))


def _parse_weather_batch(response, expected: int) -> list:
    try:
        data = response.json()
    except ValueError:
        raise RouteServiceError(
            'PROVIDER_ERROR',
            'Malformed JSON response from weather service.',
            502,
        )

    # Open-Meteo returns a single object for one location and a list for many.
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

    if len(results) != expected:
        raise RouteServiceError(
            'PROVIDER_ERROR',
            f'Weather provider returned {len(results)} locations, expected {expected}.',
            502,
        )
    return results


def fetch_open_meteo_batch(coords: list, base_url: str, max_retries: int = 2) -> list:
    """Fetch hourly forecasts for a batch of coordinates, retrying transient errors."""
    if not coords:
        return []

    params = {
        'latitude': ','.join(f'{c[0]:.4f}' for c in coords),
        'longitude': ','.join(f'{c[1]:.4f}' for c in coords),
        'hourly': 'wind_speed_10m,rain,snowfall',
        'wind_speed_unit': 'mph',
        'precipitation_unit': 'inch',
        'timeformat': 'unixtime',
        'forecast_days': 7,
    }

    for attempt in range(max_retries + 1):
        can_retry = attempt < max_retries
        try:
            response = requests.get(base_url, params=params, timeout=15)
        except requests.Timeout:
            if can_retry:
                _backoff(attempt)
                continue
            raise RouteServiceError(
                'PROVIDER_TIMEOUT',
                'Weather service request timed out.',
                status_code=504,
            )
        except requests.RequestException:
            if can_retry:
                _backoff(attempt)
                continue
            raise RouteServiceError(
                'PROVIDER_ERROR',
                'Failed to connect to weather forecast service.',
                status_code=502,
            )

        if response.status_code == 200:
            return _parse_weather_batch(response, len(coords))

        if response.status_code in RETRYABLE_STATUS_CODES and can_retry:
            _backoff(attempt)
            continue

        is_server_side = response.status_code >= 500 or response.status_code == 429
        raise RouteServiceError(
            'PROVIDER_ERROR',
            f'Weather service returned error status {response.status_code}.',
            status_code=502 if is_server_side else 400,
        )


def parse_weather_hourly(location_data: dict) -> dict:
    """Return the hourly arrays for one location, or None if they are unusable."""
    if not isinstance(location_data, dict):
        return None

    hourly = location_data.get('hourly')
    if not isinstance(hourly, dict):
        return None

    times = hourly.get('time')
    wind = hourly.get('wind_speed_10m')
    rain = hourly.get('rain')
    snow = hourly.get('snowfall')

    if not all(isinstance(arr, list) for arr in (times, wind, rain, snow)):
        return None

    n = len(times)
    if n == 0 or not (len(wind) == len(rain) == len(snow) == n):
        return None

    # Timestamps must be integers in strictly increasing order for bisect.
    for i, t in enumerate(times):
        if not isinstance(t, int) or (i > 0 and t <= times[i - 1]):
            return None

    return {
        'time': times,
        'wind_speed_10m': wind,
        'rain': rain,
        'snowfall': snow,
    }


def _no_forecast() -> dict:
    return {
        'status': 'unavailable',
        'wind_speed_mph': None,
        'rain_in': None,
        'snowfall_in': None,
        'reason': NO_FORECAST_REASON,
    }


def match_checkpoint_weather(eta_unix: int, hourly_data: dict) -> dict:
    """
    Pick the forecast hour closest to the checkpoint ETA.

    Only hours within 30 minutes count. On an exact tie the later hour wins.
    """
    if not hourly_data or not isinstance(hourly_data.get('time'), list) or not hourly_data['time']:
        return _no_forecast()

    times = hourly_data['time']
    idx = bisect.bisect_left(times, eta_unix)
    candidates = [i for i in (idx - 1, idx) if 0 <= i < len(times)]

    best_idx = min(candidates, key=lambda i: (abs(times[i] - eta_unix), -times[i]))
    chosen_time = times[best_idx]
    offset_seconds = chosen_time - eta_unix

    if abs(offset_seconds) > FORECAST_MATCH_WINDOW_S:
        return _no_forecast()

    values = {}
    missing = []
    for field, key in (('wind_speed_mph', 'wind_speed_10m'), ('rain_in', 'rain'), ('snowfall_in', 'snowfall')):
        value = hourly_data[key][best_idx]
        if not isinstance(value, (int, float)):
            value = None
            missing.append(field)
        values[field] = value

    wind, rain, snow = values['wind_speed_mph'], values['rain_in'], values['snowfall_in']
    weather = {
        'status': 'partial' if missing else 'available',
        'wind_speed_mph': round(float(wind), 2) if wind is not None else None,
        'rain_in': round(float(rain), 3) if rain is not None else None,
        'snowfall_in': round(float(snow), 3) if snow is not None else None,
        'forecast_time_unix': chosen_time,
        'forecast_match_offset_seconds': offset_seconds,
    }
    if missing:
        weather['missing_fields'] = missing
    return weather


def _coord_key(checkpoint: dict):
    # 4 decimals is about 11 m, so overlapping routes share forecast lookups.
    lat = checkpoint.get('latitude')
    lon = checkpoint.get('longitude')
    if lat is None or lon is None:
        return None
    return (round(float(lat), 4), round(float(lon), 4))


def attach_weather_to_routes(routes: list, base_url: str = None) -> list:
    """Fetch forecasts for every unique checkpoint and attach the matching hour."""
    if not routes:
        return routes

    if base_url is None:
        base_url = getattr(settings, 'OPEN_METEO_BASE_URL', DEFAULT_OPEN_METEO_URL)

    coord_keys = []
    seen = set()
    for route in routes:
        for cp in route.get('checkpoints', []):
            key = _coord_key(cp)
            if key is not None and key not in seen:
                seen.add(key)
                coord_keys.append(key)

    if not coord_keys:
        return routes

    weather_by_coord = {}
    for i in range(0, len(coord_keys), WEATHER_BATCH_SIZE):
        batch = coord_keys[i:i + WEATHER_BATCH_SIZE]
        for coord, location_data in zip(batch, fetch_open_meteo_batch(batch, base_url)):
            weather_by_coord[coord] = parse_weather_hourly(location_data)

    for route in routes:
        for cp in route.get('checkpoints', []):
            hourly = weather_by_coord.get(_coord_key(cp))
            cp['weather'] = match_checkpoint_weather(cp.get('eta_unix', 0), hourly)

    return routes
