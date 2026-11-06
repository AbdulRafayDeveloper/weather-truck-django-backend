"""
Weather risk classification and route recommendation engine.

Implements the 5-tier environmental hazard scale:
Low < Moderate < High < Severe < No Travel
Applies cargo weight overrides to wind risk, calculates checkpoint risk,
aggregates route risk distances, and determines the safest recommended route.
"""

from typing import Optional, Dict, Any, List, Tuple

# Exact 5 risk categories (ordered by severity)
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

# Distance-weighted average risk weights (No Travel is excluded)
RISK_WEIGHTS = {
    RISK_LOW: 1,
    RISK_MODERATE: 2,
    RISK_HIGH: 3,
    RISK_SEVERE: 4,
}

# Recommendation status codes
REC_STATUS_RECOMMENDED = 'recommended'
REC_STATUS_NO_SAFE_ROUTE = 'no_safe_route'
REC_STATUS_INCOMPLETE_WEATHER = 'incomplete_weather'

# Standard message strings
MSG_NO_SAFE_ROUTE = 'No safe route available under current weather conditions.'
MSG_INCOMPLETE_WEATHER = 'Route safety could not be fully assessed because weather data is missing.'


def classify_wind_risk(wind_mph: float, load_weight_lb: float = 0.0) -> str:
    """
    Classify wind hazard with continuous ranges and apply cargo-weight overrides.

    Base Thresholds:
    - Below 25 mph: Low
    - 25 to less than 35 mph: Moderate
    - 35 to less than 45 mph: High
    - 45 to less than 55 mph: Severe
    - 55 mph or above: No Travel

    Cargo Weight Overrides (applied after base classification):
    1. Wind >= 55 mph: No Travel, regardless of cargo load.
    2. Wind >= 45 and < 55 mph AND cargo load > 30,000 lb: No Travel.
    3. Wind >= 35 and < 45 mph AND cargo load > 40,000 lb: Severe.
    """
    # 1. Base classification
    if wind_mph < 25.0:
        base_level = RISK_LOW
    elif wind_mph < 35.0:
        base_level = RISK_MODERATE
    elif wind_mph < 45.0:
        base_level = RISK_HIGH
    elif wind_mph < 55.0:
        base_level = RISK_SEVERE
    else:
        base_level = RISK_NO_TRAVEL

    # 2. Cargo-weight wind overrides
    if wind_mph >= 55.0:
        return RISK_NO_TRAVEL
    if wind_mph >= 45.0 and load_weight_lb > 30000:
        return RISK_NO_TRAVEL
    if wind_mph >= 35.0 and load_weight_lb > 40000:
        return RISK_SEVERE

    return base_level


def classify_rain_risk(rain_in: float) -> str:
    """
    Classify rain hazard in inches per hour.

    Thresholds:
    - Below 0.10: Low
    - 0.10 through 0.25 inclusive: Moderate
    - Above 0.25 through 0.50 inclusive: High
    - Above 0.50 through 1.00 inclusive: Severe
    - Above 1.00: No Travel
    """
    if rain_in < 0.10:
        return RISK_LOW
    elif rain_in <= 0.25:
        return RISK_MODERATE
    elif rain_in <= 0.50:
        return RISK_HIGH
    elif rain_in <= 1.00:
        return RISK_SEVERE
    else:
        return RISK_NO_TRAVEL


def classify_snow_risk(snowfall_in: float) -> str:
    """
    Classify snowfall hazard in inches per hour.

    Thresholds:
    - Below 0.5: Low
    - 0.5 through 1.0 inclusive: Moderate
    - Above 1.0 through 2.0 inclusive: High
    - Above 2.0 through 3.0 inclusive: Severe
    - Above 3.0: No Travel
    """
    if snowfall_in < 0.5:
        return RISK_LOW
    elif snowfall_in <= 1.0:
        return RISK_MODERATE
    elif snowfall_in <= 2.0:
        return RISK_HIGH
    elif snowfall_in <= 3.0:
        return RISK_SEVERE
    else:
        return RISK_NO_TRAVEL


def assess_checkpoint_risk(weather: Optional[Dict[str, Any]], load_weight_lb: float = 0.0) -> Dict[str, Any]:
    """
    Assess checkpoint risk factors (wind with load overrides, rain, snow) and
    determine overall checkpoint risk level.

    Handles complete ('available'), incomplete ('partial'), and missing ('unavailable') weather.
    Incomplete weather never produces a falsely reassuring overall risk.
    """
    if not weather or not isinstance(weather, dict):
        return {
            'assessment_status': 'unavailable',
            'wind_level': None,
            'rain_level': None,
            'snow_level': None,
            'overall_level': None,
        }

    status = weather.get('status')
    wind = weather.get('wind_speed_mph')
    rain = weather.get('rain_in')
    snow = weather.get('snowfall_in')

    if status == 'available' and wind is not None and rain is not None and snow is not None:
        try:
            wind_val = float(wind)
            rain_val = float(rain)
            snow_val = float(snow)
        except (TypeError, ValueError):
            return {
                'assessment_status': 'partial',
                'wind_level': None,
                'rain_level': None,
                'snow_level': None,
                'overall_level': None,
            }

        wind_level = classify_wind_risk(wind_val, load_weight_lb)
        rain_level = classify_rain_risk(rain_val)
        snow_level = classify_snow_risk(snow_val)

        overall_level = max(
            [wind_level, rain_level, snow_level],
            key=lambda lvl: RISK_SEVERITY[lvl],
        )

        return {
            'assessment_status': 'assessed',
            'wind_level': wind_level,
            'rain_level': rain_level,
            'snow_level': snow_level,
            'overall_level': overall_level,
        }

    elif status == 'partial':
        wind_level = None
        if wind is not None:
            try:
                wind_level = classify_wind_risk(float(wind), load_weight_lb)
            except (TypeError, ValueError):
                pass

        rain_level = None
        if rain is not None:
            try:
                rain_level = classify_rain_risk(float(rain))
            except (TypeError, ValueError):
                pass

        snow_level = None
        if snow is not None:
            try:
                snow_level = classify_snow_risk(float(snow))
            except (TypeError, ValueError):
                pass

        # Partial assessment MUST NOT assign a reassuring overall risk
        return {
            'assessment_status': 'partial',
            'wind_level': wind_level,
            'rain_level': rain_level,
            'snow_level': snow_level,
            'overall_level': None,
        }

    else:
        return {
            'assessment_status': 'unavailable',
            'wind_level': None,
            'rain_level': None,
            'snow_level': None,
            'overall_level': None,
        }


def aggregate_route_risk(route: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aggregate actual segment distances across risk categories:
    low_miles, moderate_miles, high_miles, severe_miles, no_travel_miles, unassessed_miles.
    Computes distance-weighted average risk for fully assessed safe routes.
    """
    low_miles = 0.0
    moderate_miles = 0.0
    high_miles = 0.0
    severe_miles = 0.0
    no_travel_miles = 0.0
    unassessed_miles = 0.0

    checkpoints = route.get('checkpoints', [])
    for cp in checkpoints:
        seg_miles = float(cp.get('segment_miles', 0.0))
        risk = cp.get('risk', {})
        status = risk.get('assessment_status')
        overall = risk.get('overall_level')

        if status == 'assessed' and overall in RISK_SEVERITY:
            if overall == RISK_LOW:
                low_miles += seg_miles
            elif overall == RISK_MODERATE:
                moderate_miles += seg_miles
            elif overall == RISK_HIGH:
                high_miles += seg_miles
            elif overall == RISK_SEVERE:
                severe_miles += seg_miles
            elif overall == RISK_NO_TRAVEL:
                no_travel_miles += seg_miles
        else:
            unassessed_miles += seg_miles

    total_route_miles = float(route.get('distance_miles', 0.0))
    sum_miles = low_miles + moderate_miles + high_miles + severe_miles + no_travel_miles + unassessed_miles
    if total_route_miles <= 0.0:
        total_route_miles = sum_miles

    # Raw safety states evaluated before display rounding to prevent sub-precision leakage
    is_fully_assessed = (unassessed_miles == 0.0)
    has_no_travel = (no_travel_miles > 0.0)

    # Normalize floating-point representation to 2 decimal places for display
    # Guarantees maximum rounding divergence <= 0.03 miles across 6 categories (strictly <= 0.05 miles)
    r_low = round(low_miles, 2)
    r_mod = round(moderate_miles, 2)
    r_high = round(high_miles, 2)
    r_sev = round(severe_miles, 2)
    r_notrav = round(no_travel_miles, 2)
    r_unass = round(unassessed_miles, 2)

    target_total = round(total_route_miles, 2)

    # Average risk calculation: retain unrounded values during calculation
    # Only defined for fully assessed routes with zero No Travel miles
    if is_fully_assessed and not has_no_travel and target_total > 0.0:
        weighted_sum = (
            low_miles * RISK_WEIGHTS[RISK_LOW]
            + moderate_miles * RISK_WEIGHTS[RISK_MODERATE]
            + high_miles * RISK_WEIGHTS[RISK_HIGH]
            + severe_miles * RISK_WEIGHTS[RISK_SEVERE]
        )
        average_risk = round(weighted_sum / total_route_miles, 2)
    else:
        average_risk = None

    return {
        'low_miles': r_low,
        'moderate_miles': r_mod,
        'high_miles': r_high,
        'severe_miles': r_sev,
        'no_travel_miles': r_notrav,
        'unassessed_miles': r_unass,
        'average_risk': average_risk,
        'is_fully_assessed': is_fully_assessed,
        'has_no_travel': has_no_travel,
    }


def generate_recommendation_message(
    best_route: Dict[str, Any],
    eligible_routes: List[Tuple[int, Dict[str, Any]]],
    all_routes: List[Dict[str, Any]],
    is_weather_complete: bool,
) -> str:
    """
    Generate dynamic, factual recommendation rationale accurately identifying
    the ranking criterion or tie-breaker that selected the winning route.
    """
    best_id = best_route.get('id') if best_route.get('id') is not None else 1
    best_rs = best_route.get('risk_summary', {})

    if len(eligible_routes) == 1:
        if len(all_routes) == 1:
            base_msg = f"Route {best_id} is recommended as the safest route under current weather conditions."
        else:
            base_msg = f"Route {best_id} is recommended as the only fully assessed safe route available."
    else:
        # Compare best_route against the runner-up (the second-ranked eligible route)
        runner_up = eligible_routes[1][1]
        runner_rs = runner_up.get('risk_summary', {})

        best_severe = best_rs.get('severe_miles', 0.0)
        runner_severe = runner_rs.get('severe_miles', 0.0)
        best_high = best_rs.get('high_miles', 0.0)
        runner_high = runner_rs.get('high_miles', 0.0)
        best_avg = best_rs.get('average_risk')
        runner_avg = runner_rs.get('average_risk')
        best_dur = best_route.get('duration_seconds', 0)
        runner_dur = runner_up.get('duration_seconds', 0)

        # 1. Fewest Severe miles
        if best_severe < runner_severe:
            base_msg = f"Route {best_id} is recommended because it has the fewest severe-risk miles."
        # 2. Fewest High miles
        elif best_high < runner_high:
            base_msg = f"Route {best_id} is recommended because it has the fewest high-risk miles."
        # 3. Lowest average risk
        elif best_avg is not None and runner_avg is not None and best_avg < runner_avg:
            base_msg = f"Route {best_id} is recommended because it has the lowest average weather risk."
        # 4. Shortest travel time (weather risk metrics are equal between best and runner up)
        elif best_dur < runner_dur:
            all_equal_weather = all(
                r.get('risk_summary', {}).get('severe_miles', 0.0) == best_severe
                and r.get('risk_summary', {}).get('high_miles', 0.0) == best_high
                and r.get('risk_summary', {}).get('average_risk') == best_avg
                for _, r in eligible_routes
            )
            if all_equal_weather:
                if len(eligible_routes) == len(all_routes):
                    if len(all_routes) == 2:
                        prefix = "Both routes"
                    elif len(all_routes) == 3:
                        prefix = "All three routes"
                    else:
                        prefix = f"All {len(all_routes)} routes"
                else:
                    if len(eligible_routes) == 2:
                        prefix = "Both eligible routes"
                    elif len(eligible_routes) == 3:
                        prefix = "All three eligible routes"
                    else:
                        prefix = f"All {len(eligible_routes)} eligible routes"
                base_msg = f"{prefix} have equal weather-risk assessments. Route {best_id} is recommended because it has the shortest estimated travel time."
            else:
                base_msg = f"Route {best_id} is recommended because it has the shortest estimated travel time among routes with lowest weather risk."
        # 5. Deterministic provider order tie-break
        else:
            if len(eligible_routes) == len(all_routes):
                prefix = "Both routes" if len(all_routes) == 2 else f"All {len(all_routes)} routes"
            else:
                prefix = f"All {len(eligible_routes)} eligible routes"
            base_msg = f"{prefix} have equal weather-risk assessments and travel times. Route {best_id} is recommended based on provider route ordering."

    # Communicate incomplete weather coverage for alternative routes when present
    if not is_weather_complete:
        unassessed_ids = [
            str(r.get('id') if r.get('id') is not None else (idx + 1))
            for idx, r in enumerate(all_routes)
            if not r.get('risk_summary', {}).get('is_fully_assessed', False)
        ]
        if len(unassessed_ids) == 1:
            route_ref = f"Route {unassessed_ids[0]}"
        elif len(unassessed_ids) == 2:
            route_ref = f"Route {unassessed_ids[0]} and Route {unassessed_ids[1]}"
        else:
            route_ref = "Some alternative routes"
        base_msg += f" Note: {route_ref} could not be fully assessed due to missing weather data."

    return base_msg


def evaluate_route_recommendations(routes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Evaluate and rank route alternatives to determine safest recommended route.

    Eligibility:
    1. Fully assessed (unassessed_miles == 0)
    2. Free of No Travel miles (no_travel_miles == 0)

    Ranking Hierarchy among eligible routes:
    1. Fewest Severe miles
    2. Fewest High miles
    3. Lowest distance-weighted average risk
    4. Shortest travel time (duration_seconds)
    5. Provider route ordering (deterministic final tie-breaker)
    """
    if not routes:
        return {
            'recommended_route_id': None,
            'status': REC_STATUS_NO_SAFE_ROUTE,
            'message': MSG_NO_SAFE_ROUTE,
            'all_routes_unsafe': True,
            'is_weather_complete': True,
        }

    all_weather_complete = all(
        r.get('risk_summary', {}).get('is_fully_assessed', False) for r in routes
    )

    eligible_routes = []
    for idx, r in enumerate(routes):
        rs = r.get('risk_summary', {})
        # Route is eligible only if fully assessed and free of any No Travel mileage (raw precision)
        is_assessed = rs.get('is_fully_assessed', False)
        has_nt = rs.get('has_no_travel', rs.get('no_travel_miles', 0.0) > 0.0)
        if is_assessed and not has_nt and rs.get('no_travel_miles', 0.0) == 0.0:
            eligible_routes.append((idx, r))

    if eligible_routes:
        # Sort by specified hierarchy
        eligible_routes.sort(
            key=lambda item: (
                item[1]['risk_summary']['severe_miles'],
                item[1]['risk_summary']['high_miles'],
                item[1]['risk_summary']['average_risk'] if item[1]['risk_summary']['average_risk'] is not None else 999.0,
                item[1].get('duration_seconds', 0),
                item[0],  # provider route index
            )
        )

        best_idx, best_route = eligible_routes[0]
        best_id = best_route.get('id') if best_route.get('id') is not None else (best_idx + 1)

        # Mark routes with recommendation flag
        for idx, r in enumerate(routes):
            r_id = r.get('id') if r.get('id') is not None else (idx + 1)
            r['is_recommended'] = (r_id == best_id)

        msg = generate_recommendation_message(
            best_route=best_route,
            eligible_routes=eligible_routes,
            all_routes=routes,
            is_weather_complete=all_weather_complete,
        )

        return {
            'recommended_route_id': best_id,
            'status': REC_STATUS_RECOMMENDED,
            'message': msg,
            'all_routes_unsafe': False,
            'is_weather_complete': all_weather_complete,
        }

    # No eligible routes available
    for r in routes:
        r['is_recommended'] = False

    if all_weather_complete:
        # Every route is fully assessed and contains No Travel miles (Case C)
        return {
            'recommended_route_id': None,
            'status': REC_STATUS_NO_SAFE_ROUTE,
            'message': MSG_NO_SAFE_ROUTE,
            'all_routes_unsafe': True,
            'is_weather_complete': True,
        }
    else:
        # Incomplete weather data prevents full assessment (Case B & Case D)
        return {
            'recommended_route_id': None,
            'status': REC_STATUS_INCOMPLETE_WEATHER,
            'message': MSG_INCOMPLETE_WEATHER,
            'all_routes_unsafe': False,
            'is_weather_complete': False,
        }


def process_route_risks(routes: List[Dict[str, Any]], load_weight_lb: float = 0.0) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Process checkpoint risk scoring, distance aggregation, and route recommendations
    across a set of route alternatives.
    """
    for route in routes:
        # 1. Assess each checkpoint's risk
        for cp in route.get('checkpoints', []):
            cp['risk'] = assess_checkpoint_risk(cp.get('weather'), load_weight_lb)

        # 2. Aggregate route-level risk miles
        route['risk_summary'] = aggregate_route_risk(route)

    # 3. Evaluate route recommendations
    recommendation = evaluate_route_recommendations(routes)

    return routes, recommendation
