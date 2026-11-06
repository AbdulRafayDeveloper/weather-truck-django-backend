import math

METERS_PER_MILE = 1609.344
SECONDS_PER_MINUTE = 60
EARTH_RADIUS_METERS = 6371000.0


def meters_to_miles(meters: float) -> float:
    """Convert meters to miles, rounded to 1 decimal place."""
    return round(meters / METERS_PER_MILE, 1)


def seconds_to_minutes(seconds: int) -> int:
    """Convert seconds to whole minutes."""
    return round(seconds / SECONDS_PER_MINUTE)


def haversine_distance(coord1: dict, coord2: dict) -> float:
    """Calculate the great-circle distance in meters between two coordinates."""
    lat1 = math.radians(coord1['latitude'])
    lon1 = math.radians(coord1['longitude'])
    lat2 = math.radians(coord2['latitude'])
    lon2 = math.radians(coord2['longitude'])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return EARTH_RADIUS_METERS * c


def interpolate_coordinate(coord1: dict, coord2: dict, fraction: float) -> dict:
    """Linearly interpolate between two geographic coordinates."""
    frac = max(0.0, min(1.0, float(fraction)))
    lat = coord1['latitude'] + frac * (coord2['latitude'] - coord1['latitude'])
    lon = coord1['longitude'] + frac * (coord2['longitude'] - coord1['longitude'])
    return {
        'latitude': round(lat, 5),
        'longitude': round(lon, 5),
    }
