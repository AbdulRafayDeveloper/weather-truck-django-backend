from datetime import datetime, timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .services import get_route_alternatives, RouteServiceError


@api_view(['GET'])
def api_root(request):
    """Service status page shown at the backend root URL."""
    return Response(
        {
            'service': 'Weather-Aware Truck Routing API',
            'status': 'running',
            'message': 'Backend is running successfully.',
            'server_time_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'endpoints': {
                'health': {'method': 'GET', 'path': '/api/health/'},
                'routes': {'method': 'POST', 'path': '/api/routes/'},
            },
        },
        status=status.HTTP_200_OK,
    )


@api_view(['GET'])
def health_check(request):
    return Response({'status': 'ok'}, status=status.HTTP_200_OK)


def validate_route_request(data: dict) -> dict:
    """Validate and normalize incoming routing request payload."""
    if not isinstance(data, dict):
        raise RouteServiceError('VALIDATION_ERROR', 'Request body must be a JSON object.', 400)

    origin = data.get('origin')
    if not origin or not isinstance(origin, str) or not origin.strip():
        raise RouteServiceError('VALIDATION_ERROR', 'Origin location is required.', 400)

    destination = data.get('destination')
    if not destination or not isinstance(destination, str) or not destination.strip():
        raise RouteServiceError('VALIDATION_ERROR', 'Destination location is required.', 400)

    if origin.strip().lower() == destination.strip().lower():
        raise RouteServiceError('VALIDATION_ERROR', 'Origin and destination must not be identical.', 400)

    departure = data.get('departure')
    if not departure or not isinstance(departure, str) or not departure.strip():
        raise RouteServiceError('VALIDATION_ERROR', 'Departure datetime is required.', 400)

    try:
        datetime.fromisoformat(departure.strip().replace('Z', '+00:00'))
    except ValueError:
        raise RouteServiceError('VALIDATION_ERROR', 'Departure must be a valid ISO 8601 datetime string.', 400)

    # Support load_weight_lb as canonical and load_weight as alias
    raw_weight = data.get('load_weight_lb')
    if raw_weight is None:
        raw_weight = data.get('load_weight')

    if raw_weight is None:
        raise RouteServiceError('VALIDATION_ERROR', 'Load weight is required.', 400)

    try:
        load_weight_lb = float(raw_weight)
    except (TypeError, ValueError):
        raise RouteServiceError('VALIDATION_ERROR', 'Load weight must be a numeric value.', 400)

    if load_weight_lb <= 0:
        raise RouteServiceError('VALIDATION_ERROR', 'Load weight must be greater than 0.', 400)

    # interval_miles validation: optional, defaults to 25. Allowed values: 10, 25, 50.
    raw_interval = data.get('interval_miles')
    if raw_interval is None:
        interval_miles = 25
    else:
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
            raise RouteServiceError(
                'VALIDATION_ERROR',
                'interval_miles must be an integer (10, 25, or 50).',
                400,
            )
        if raw_interval not in (10, 25, 50):
            raise RouteServiceError(
                'VALIDATION_ERROR',
                'interval_miles must be one of 10, 25, or 50.',
                400,
            )
        interval_miles = int(raw_interval)

    return {
        'origin': origin.strip(),
        'destination': destination.strip(),
        'departure': departure.strip(),
        'load_weight_lb': load_weight_lb,
        'interval_miles': interval_miles,
    }


@api_view(['POST'])
def calculate_routes(request):
    """Calculate truck route alternatives using TomTom Maps Routing API."""
    try:
        params = validate_route_request(request.data)
        routes, recommendation = get_route_alternatives(
            origin=params['origin'],
            destination=params['destination'],
            departure=params['departure'],
            load_weight_lb=params['load_weight_lb'],
            interval_miles=params['interval_miles'],
        )
        return Response(
            {
                'routes': routes,
                'recommendation': recommendation,
            },
            status=status.HTTP_200_OK,
        )
    except RouteServiceError as err:
        return Response(
            {'error': {'code': err.code, 'message': err.message}},
            status=err.status_code,
        )
