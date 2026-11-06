"""Weather risk classification and route recommendation."""

from typing import Any, Callable, Dict, List, Optional, Tuple

RISK_LOW = 'Low'
RISK_MODERATE = 'Moderate'
RISK_HIGH = 'High'
RISK_SEVERE = 'Severe'
RISK_NO_TRAVEL = 'No Travel'

RISK_LEVELS = [RISK_LOW, RISK_MODERATE, RISK_HIGH, RISK_SEVERE, RISK_NO_TRAVEL]

RISK_SEVERITY = {
    RISK_LOW: 1,
    RISK_MODERATE: 2,
    RISK_HIGH: 3,
    RISK_SEVERE: 4,
    RISK_NO_TRAVEL: 5,
}

# Weights for the distance-weighted average. No Travel has no weight because
# a route with any No Travel miles never gets an average.
RISK_WEIGHTS = {
    RISK_LOW: 1,
    RISK_MODERATE: 2,
    RISK_HIGH: 3,
    RISK_SEVERE: 4,
}

REC_STATUS_RECOMMENDED = 'recommended'
REC_STATUS_NO_SAFE_ROUTE = 'no_safe_route'
REC_STATUS_INCOMPLETE_WEATHER = 'incomplete_weather'

MSG_NO_SAFE_ROUTE = 'No safe route available under current weather conditions.'
MSG_INCOMPLETE_WEATHER = 'Route safety could not be fully assessed because weather data is missing.'

HEAVY_LOAD_LB = 30000
VERY_HEAVY_LOAD_LB = 40000


def classify_wind_risk(wind_mph: float, load_weight_lb: float = 0.0) -> str:
    """Classify wind speed, applying the load rules for heavy trucks."""
    if wind_mph < 25.0:
        return RISK_LOW
    if wind_mph < 35.0:
        return RISK_MODERATE
    if wind_mph < 45.0:
        return RISK_SEVERE if load_weight_lb > VERY_HEAVY_LOAD_LB else RISK_HIGH
    if wind_mph < 55.0:
        return RISK_NO_TRAVEL if load_weight_lb > HEAVY_LOAD_LB else RISK_SEVERE
    return RISK_NO_TRAVEL


def classify_rain_risk(rain_in: float) -> str:
    """Classify rainfall in inches per hour."""
    if rain_in < 0.10:
        return RISK_LOW
    if rain_in <= 0.25:
        return RISK_MODERATE
    if rain_in <= 0.50:
        return RISK_HIGH
    if rain_in <= 1.00:
        return RISK_SEVERE
    return RISK_NO_TRAVEL


def classify_snow_risk(snowfall_in: float) -> str:
    """Classify snowfall in inches per hour."""
    if snowfall_in < 0.5:
        return RISK_LOW
    if snowfall_in <= 1.0:
        return RISK_MODERATE
    if snowfall_in <= 2.0:
        return RISK_HIGH
    if snowfall_in <= 3.0:
        return RISK_SEVERE
    return RISK_NO_TRAVEL


def _empty_assessment(status: str) -> Dict[str, Any]:
    return {
        'assessment_status': status,
        'wind_level': None,
        'rain_level': None,
        'snow_level': None,
        'overall_level': None,
    }


def _classify_or_none(value: Any, classify: Callable[..., str], *args: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return classify(float(value), *args)
    except (TypeError, ValueError):
        return None


def assess_checkpoint_risk(weather: Optional[Dict[str, Any]], load_weight_lb: float = 0.0) -> Dict[str, Any]:
    """Classify each hazard at a checkpoint and take the worst as the overall level."""
    if not weather or not isinstance(weather, dict):
        return _empty_assessment('unavailable')

    status = weather.get('status')
    wind = weather.get('wind_speed_mph')
    rain = weather.get('rain_in')
    snow = weather.get('snowfall_in')

    if status == 'available' and None not in (wind, rain, snow):
        try:
            wind_val, rain_val, snow_val = float(wind), float(rain), float(snow)
        except (TypeError, ValueError):
            return _empty_assessment('partial')

        levels = {
            'wind_level': classify_wind_risk(wind_val, load_weight_lb),
            'rain_level': classify_rain_risk(rain_val),
            'snow_level': classify_snow_risk(snow_val),
        }
        return {
            'assessment_status': 'assessed',
            **levels,
            'overall_level': max(levels.values(), key=RISK_SEVERITY.get),
        }

    if status == 'partial':
        # Report what we can, but never give an overall level from incomplete
        # data: a missing value could be the dangerous one.
        return {
            'assessment_status': 'partial',
            'wind_level': _classify_or_none(wind, classify_wind_risk, load_weight_lb),
            'rain_level': _classify_or_none(rain, classify_rain_risk),
            'snow_level': _classify_or_none(snow, classify_snow_risk),
            'overall_level': None,
        }

    return _empty_assessment('unavailable')


def aggregate_route_risk(route: Dict[str, Any]) -> Dict[str, Any]:
    """Sum route miles per risk level and compute the distance-weighted average."""
    miles = {level: 0.0 for level in RISK_LEVELS}
    unassessed_miles = 0.0

    for cp in route.get('checkpoints', []):
        segment_miles = float(cp.get('segment_miles', 0.0))
        risk = cp.get('risk', {})
        overall = risk.get('overall_level')

        if risk.get('assessment_status') == 'assessed' and overall in RISK_SEVERITY:
            miles[overall] += segment_miles
        else:
            unassessed_miles += segment_miles

    total_miles = float(route.get('distance_miles', 0.0))
    if total_miles <= 0.0:
        total_miles = sum(miles.values()) + unassessed_miles

    # Decide safety from the raw values, before rounding for display,
    # so a tiny No Travel stretch can't round away to 0.00.
    is_fully_assessed = unassessed_miles == 0.0
    has_no_travel = miles[RISK_NO_TRAVEL] > 0.0

    average_risk = None
    if is_fully_assessed and not has_no_travel and round(total_miles, 2) > 0.0:
        weighted_sum = sum(miles[level] * weight for level, weight in RISK_WEIGHTS.items())
        average_risk = round(weighted_sum / total_miles, 2)

    return {
        'low_miles': round(miles[RISK_LOW], 2),
        'moderate_miles': round(miles[RISK_MODERATE], 2),
        'high_miles': round(miles[RISK_HIGH], 2),
        'severe_miles': round(miles[RISK_SEVERE], 2),
        'no_travel_miles': round(miles[RISK_NO_TRAVEL], 2),
        'unassessed_miles': round(unassessed_miles, 2),
        'average_risk': average_risk,
        'is_fully_assessed': is_fully_assessed,
        'has_no_travel': has_no_travel,
    }


def _route_id(route: Dict[str, Any], index: int) -> int:
    return route['id'] if route.get('id') is not None else index + 1


def _group_label(count: int, eligible_only: bool) -> str:
    """Wording like "Both routes" or "All three eligible routes"."""
    noun = 'eligible routes' if eligible_only else 'routes'
    if count == 2:
        return f'Both {noun}'
    if count == 3:
        return f'All three {noun}'
    return f'All {count} {noun}'


def generate_recommendation_message(
    best_route: Dict[str, Any],
    eligible_routes: List[Tuple[int, Dict[str, Any]]],
    all_routes: List[Dict[str, Any]],
    is_weather_complete: bool,
) -> str:
    """Explain which ranking rule picked the recommended route."""
    best_id = best_route.get('id') if best_route.get('id') is not None else 1
    best_rs = best_route.get('risk_summary', {})
    eligible_only = len(eligible_routes) != len(all_routes)

    if len(eligible_routes) == 1:
        if len(all_routes) == 1:
            message = f"Route {best_id} is recommended as the safest route under current weather conditions."
        else:
            message = f"Route {best_id} is recommended as the only fully assessed safe route available."
    else:
        # The winner only needs to beat the runner-up, so explain the first
        # rule where the two differ.
        runner_up = eligible_routes[1][1]
        runner_rs = runner_up.get('risk_summary', {})

        best_severe = best_rs.get('severe_miles', 0.0)
        best_high = best_rs.get('high_miles', 0.0)
        best_avg = best_rs.get('average_risk')
        runner_avg = runner_rs.get('average_risk')

        if best_severe < runner_rs.get('severe_miles', 0.0):
            message = f"Route {best_id} is recommended because it has the fewest severe-risk miles."
        elif best_high < runner_rs.get('high_miles', 0.0):
            message = f"Route {best_id} is recommended because it has the fewest high-risk miles."
        elif best_avg is not None and runner_avg is not None and best_avg < runner_avg:
            message = f"Route {best_id} is recommended because it has the lowest average weather risk."
        elif best_route.get('duration_seconds', 0) < runner_up.get('duration_seconds', 0):
            all_equal_weather = all(
                r.get('risk_summary', {}).get('severe_miles', 0.0) == best_severe
                and r.get('risk_summary', {}).get('high_miles', 0.0) == best_high
                and r.get('risk_summary', {}).get('average_risk') == best_avg
                for _, r in eligible_routes
            )
            if all_equal_weather:
                count = len(eligible_routes) if eligible_only else len(all_routes)
                label = _group_label(count, eligible_only)
                message = (
                    f"{label} have equal weather-risk assessments. "
                    f"Route {best_id} is recommended because it has the shortest estimated travel time."
                )
            else:
                message = (
                    f"Route {best_id} is recommended because it has the shortest estimated "
                    "travel time among routes with lowest weather risk."
                )
        else:
            if not eligible_only:
                label = 'Both routes' if len(all_routes) == 2 else f'All {len(all_routes)} routes'
            else:
                label = f'All {len(eligible_routes)} eligible routes'
            message = (
                f"{label} have equal weather-risk assessments and travel times. "
                f"Route {best_id} is recommended based on provider route ordering."
            )

    if not is_weather_complete:
        unassessed_ids = [
            str(_route_id(r, idx))
            for idx, r in enumerate(all_routes)
            if not r.get('risk_summary', {}).get('is_fully_assessed', False)
        ]
        if len(unassessed_ids) == 1:
            route_ref = f"Route {unassessed_ids[0]}"
        elif len(unassessed_ids) == 2:
            route_ref = f"Route {unassessed_ids[0]} and Route {unassessed_ids[1]}"
        else:
            route_ref = "Some alternative routes"
        message += f" Note: {route_ref} could not be fully assessed due to missing weather data."

    return message


def _is_eligible(route: Dict[str, Any]) -> bool:
    """A route can be recommended only if it is fully assessed and has no No Travel miles."""
    rs = route.get('risk_summary', {})
    no_travel_miles = rs.get('no_travel_miles', 0.0)
    has_no_travel = rs.get('has_no_travel', no_travel_miles > 0.0)
    return rs.get('is_fully_assessed', False) and not has_no_travel and no_travel_miles == 0.0


def _ranking_key(item: Tuple[int, Dict[str, Any]]) -> tuple:
    index, route = item
    rs = route['risk_summary']
    average_risk = rs['average_risk'] if rs['average_risk'] is not None else 999.0
    return (
        rs['severe_miles'],
        rs['high_miles'],
        average_risk,
        route.get('duration_seconds', 0),
        index,  # keep the provider's order as the final tie-breaker
    )


def evaluate_route_recommendations(routes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Pick the safest route.

    Ranking: fewest Severe miles, fewest High miles, lowest average risk,
    shortest travel time, then the provider's order.
    """
    if not routes:
        return {
            'recommended_route_id': None,
            'status': REC_STATUS_NO_SAFE_ROUTE,
            'message': MSG_NO_SAFE_ROUTE,
            'all_routes_unsafe': True,
            'is_weather_complete': True,
        }

    weather_complete = all(
        r.get('risk_summary', {}).get('is_fully_assessed', False) for r in routes
    )
    eligible_routes = sorted(
        ((idx, r) for idx, r in enumerate(routes) if _is_eligible(r)),
        key=_ranking_key,
    )

    if not eligible_routes:
        for r in routes:
            r['is_recommended'] = False

        if weather_complete:
            return {
                'recommended_route_id': None,
                'status': REC_STATUS_NO_SAFE_ROUTE,
                'message': MSG_NO_SAFE_ROUTE,
                'all_routes_unsafe': True,
                'is_weather_complete': True,
            }
        return {
            'recommended_route_id': None,
            'status': REC_STATUS_INCOMPLETE_WEATHER,
            'message': MSG_INCOMPLETE_WEATHER,
            'all_routes_unsafe': False,
            'is_weather_complete': False,
        }

    best_idx, best_route = eligible_routes[0]
    best_id = _route_id(best_route, best_idx)
    for idx, r in enumerate(routes):
        r['is_recommended'] = _route_id(r, idx) == best_id

    return {
        'recommended_route_id': best_id,
        'status': REC_STATUS_RECOMMENDED,
        'message': generate_recommendation_message(
            best_route=best_route,
            eligible_routes=eligible_routes,
            all_routes=routes,
            is_weather_complete=weather_complete,
        ),
        'all_routes_unsafe': False,
        'is_weather_complete': weather_complete,
    }


def process_route_risks(
    routes: List[Dict[str, Any]], load_weight_lb: float = 0.0
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Score every checkpoint, summarize each route and pick a recommendation."""
    for route in routes:
        for cp in route.get('checkpoints', []):
            cp['risk'] = assess_checkpoint_risk(cp.get('weather'), load_weight_lb)
        route['risk_summary'] = aggregate_route_risk(route)

    return routes, evaluate_route_recommendations(routes)
