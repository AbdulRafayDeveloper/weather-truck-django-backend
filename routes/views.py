from datetime import datetime, timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import RouteServiceError, get_route_alternatives

ALLOWED_INTERVALS = (10, 25, 50)
DEFAULT_INTERVAL = 25


@api_view(['GET'])
def api_root(request):
    """Simple status page so the deployment can be checked in a browser."""
    return Response({
        'service': 'Weather-Aware Truck Routing API',
        'status': 'running',
        'message': 'Backend is running successfully.',
        'server_time_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'endpoints': {
            'health': {'method': 'GET', 'path': '/api/health/'},
            'routes': {'method': 'POST', 'path': '/api/routes/'},
        },
    })


@api_view(['GET'])
def health_check(request):
    return Response({'status': 'ok'})


def _invalid(message: str) -> RouteServiceError:
    return RouteServiceError('VALIDATION_ERROR', message, 400)


def _required_text(data: dict, field: str, message: str) -> str:
    value = data.get(field)
    if not value or not isinstance(value, str) or not value.strip():
        raise _invalid(message)
    return value.strip()


def _parse_load_weight(data: dict) -> float:
    # "load_weight" is accepted as a shorter alias.
    raw_weight = data.get('load_weight_lb')
    if raw_weight is None:
        raw_weight = data.get('load_weight')
    if raw_weight is None:
        raise _invalid('Load weight is required.')

    try:
        weight = float(raw_weight)
    except (TypeError, ValueError):
        raise _invalid('Load weight must be a numeric value.')

    if weight <= 0:
        raise _invalid('Load weight must be greater than 0.')
    return weight


def _parse_interval(data: dict) -> int:
    raw_interval = data.get('interval_miles')
    if raw_interval is None:
        return DEFAULT_INTERVAL

    # bool is a subclass of int, so rule it out explicitly.
    if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
        raise _invalid('interval_miles must be an integer (10, 25, or 50).')
    if raw_interval not in ALLOWED_INTERVALS:
        raise _invalid('interval_miles must be one of 10, 25, or 50.')
    return int(raw_interval)


def validate_route_request(data: dict) -> dict:
    """Validate the request body and return clean parameters."""
    if not isinstance(data, dict):
        raise _invalid('Request body must be a JSON object.')

    origin = _required_text(data, 'origin', 'Origin location is required.')
    destination = _required_text(data, 'destination', 'Destination location is required.')
    if origin.lower() == destination.lower():
        raise _invalid('Origin and destination must not be identical.')

    departure = _required_text(data, 'departure', 'Departure datetime is required.')
    try:
        datetime.fromisoformat(departure.replace('Z', '+00:00'))
    except ValueError:
        raise _invalid('Departure must be a valid ISO 8601 datetime string.')

    return {
        'origin': origin,
        'destination': destination,
        'departure': departure,
        'load_weight_lb': _parse_load_weight(data),
        'interval_miles': _parse_interval(data),
    }


@api_view(['POST'])
def calculate_routes(request):
    """Return route alternatives with weather, risk and a recommendation."""
    try:
        params = validate_route_request(request.data)
        routes, recommendation = get_route_alternatives(**params)
    except RouteServiceError as err:
        return Response(
            {'error': {'code': err.code, 'message': err.message}},
            status=err.status_code,
        )

    return Response({'routes': routes, 'recommendation': recommendation})
