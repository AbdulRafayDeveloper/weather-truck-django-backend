"""
Assignment Verification Suite (Phase 8.1)
Deterministic automated tests validating assignment compliance using synthetic weather/risk data.
Runs completely offline without external API dependencies.
"""

import os
import sys

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from routes.risk import (
    classify_wind_risk,
    classify_rain_risk,
    classify_snow_risk,
    assess_checkpoint_risk,
    aggregate_route_risk,
    evaluate_route_recommendations,
    RISK_LOW,
    RISK_MODERATE,
    RISK_HIGH,
    RISK_SEVERE,
    RISK_NO_TRAVEL,
    MSG_NO_SAFE_ROUTE,
    MSG_INCOMPLETE_WEATHER,
    REC_STATUS_RECOMMENDED,
    REC_STATUS_NO_SAFE_ROUTE,
    REC_STATUS_INCOMPLETE_WEATHER,
)
from routes.services import generate_checkpoints


def test_wind_boundaries():
    assert classify_wind_risk(24.9, 0) == RISK_LOW, "24.9 mph must be Low"
    assert classify_wind_risk(25.0, 0) == RISK_MODERATE, "25.0 mph must be Moderate"
    assert classify_wind_risk(34.9, 0) == RISK_MODERATE, "34.9 mph must be Moderate"
    assert classify_wind_risk(35.0, 0) == RISK_HIGH, "35.0 mph must be High"
    assert classify_wind_risk(44.9, 0) == RISK_HIGH, "44.9 mph must be High"
    assert classify_wind_risk(45.0, 0) == RISK_SEVERE, "45.0 mph must be Severe"
    assert classify_wind_risk(54.9, 0) == RISK_SEVERE, "54.9 mph must be Severe"
    assert classify_wind_risk(55.0, 0) == RISK_NO_TRAVEL, "55.0 mph must be No Travel"
    print("PASS - wind threshold boundaries")


def test_cargo_overrides():
    # 45 mph + 30,000 lb -> Severe; 45 mph + 30,001 lb -> No Travel
    assert classify_wind_risk(45.0, 30000) == RISK_SEVERE, "45 mph at 30,000 lb must be Severe"
    assert classify_wind_risk(45.0, 30001) == RISK_NO_TRAVEL, "45 mph at 30,001 lb must be No Travel"
    
    # 35 mph + 40,000 lb -> High; 35 mph + 40,001 lb -> Severe
    assert classify_wind_risk(35.0, 40000) == RISK_HIGH, "35 mph at 40,000 lb must be High"
    assert classify_wind_risk(35.0, 40001) == RISK_SEVERE, "35 mph at 40,001 lb must be Severe"
    
    # 55 mph -> No Travel regardless of cargo
    assert classify_wind_risk(55.0, 0) == RISK_NO_TRAVEL, "55 mph at 0 lb must be No Travel"
    assert classify_wind_risk(55.0, 10000) == RISK_NO_TRAVEL, "55 mph at 10,000 lb must be No Travel"
    assert classify_wind_risk(55.0, 50000) == RISK_NO_TRAVEL, "55 mph at 50,000 lb must be No Travel"
    print("PASS - cargo override boundaries")


def test_rain_boundaries():
    assert classify_rain_risk(0.09) == RISK_LOW, "0.09 in/hr must be Low"
    assert classify_rain_risk(0.10) == RISK_MODERATE, "0.10 in/hr must be Moderate"
    assert classify_rain_risk(0.25) == RISK_MODERATE, "0.25 in/hr must be Moderate"
    assert classify_rain_risk(0.26) == RISK_HIGH, "0.26 in/hr must be High"
    assert classify_rain_risk(0.50) == RISK_HIGH, "0.50 in/hr must be High"
    assert classify_rain_risk(0.51) == RISK_SEVERE, "0.51 in/hr must be Severe"
    assert classify_rain_risk(1.00) == RISK_SEVERE, "1.00 in/hr must be Severe"
    assert classify_rain_risk(1.01) == RISK_NO_TRAVEL, "1.01 in/hr must be No Travel"
    print("PASS - rain boundaries")


def test_snow_boundaries():
    """
    Snowfall (inches/hour) thresholds per assignment specification:
    - below 0.5 in/hr -> Low
    - 0.5 to 1.0 in/hr inclusive -> Moderate
    - above 1.0 to 2.0 in/hr inclusive -> High
    - above 2.0 to 3.0 in/hr inclusive -> Severe
    - above 3.0 in/hr -> No Travel
    """
    assert classify_snow_risk(0.0) == RISK_LOW, "0.0 in/hr (<0.5) must be Low"
    assert classify_snow_risk(0.49) == RISK_LOW, "0.49 in/hr (<0.5) must be Low"
    assert classify_snow_risk(0.50) == RISK_MODERATE, "0.50 in/hr (0.5 to 1.0 inclusive) must be Moderate"
    assert classify_snow_risk(1.00) == RISK_MODERATE, "1.00 in/hr (0.5 to 1.0 inclusive) must be Moderate"
    assert classify_snow_risk(1.01) == RISK_HIGH, "1.01 in/hr (>1.0 to 2.0 inclusive) must be High"
    assert classify_snow_risk(2.00) == RISK_HIGH, "2.00 in/hr (>1.0 to 2.0 inclusive) must be High"
    assert classify_snow_risk(2.01) == RISK_SEVERE, "2.01 in/hr (>2.0 to 3.0 inclusive) must be Severe"
    assert classify_snow_risk(3.00) == RISK_SEVERE, "3.00 in/hr (>2.0 to 3.0 inclusive) must be Severe"
    assert classify_snow_risk(3.01) == RISK_NO_TRAVEL, "3.01 in/hr (>3.0) must be No Travel"
    print("PASS - snowfall threshold boundaries (<0.5, 0.5-1.0, >1.0-2.0, >2.0-3.0, >3.0 in/hr)")


def test_overall_risk_selection():
    # 1. Overall is worst of wind, rain, snow
    w1 = {'status': 'available', 'wind_speed_mph': 15.0, 'rain_in': 0.15, 'snowfall_in': 0.0}
    cp1 = assess_checkpoint_risk(w1, load_weight_lb=20000)
    assert cp1['wind_level'] == RISK_LOW
    assert cp1['rain_level'] == RISK_MODERATE
    assert cp1['snow_level'] == RISK_LOW
    assert cp1['overall_level'] == RISK_MODERATE, "Worst hazard (Moderate rain) must become overall risk"

    w2 = {'status': 'available', 'wind_speed_mph': 10.0, 'rain_in': 0.60, 'snowfall_in': 1.5}
    cp2 = assess_checkpoint_risk(w2, load_weight_lb=20000)
    assert cp2['wind_level'] == RISK_LOW
    assert cp2['rain_level'] == RISK_SEVERE
    assert cp2['snow_level'] == RISK_HIGH
    assert cp2['overall_level'] == RISK_SEVERE, "Worst hazard (Severe rain) must dominate High snow"

    # 2. Cargo override applied before selecting checkpoint overall risk
    # 35 mph wind with 45,000 lb cargo overrides wind to Severe, dominating Moderate rain
    w3 = {'status': 'available', 'wind_speed_mph': 35.0, 'rain_in': 0.15, 'snowfall_in': 0.0}
    cp3 = assess_checkpoint_risk(w3, load_weight_lb=45000)
    assert cp3['wind_level'] == RISK_SEVERE, "Wind must be overridden to Severe for cargo > 40k lb"
    assert cp3['overall_level'] == RISK_SEVERE, "Overall level must reflect overridden Severe wind"

    # 45 mph wind with 35,000 lb cargo overrides wind to No Travel
    w4 = {'status': 'available', 'wind_speed_mph': 45.0, 'rain_in': 0.0, 'snowfall_in': 0.0}
    cp4 = assess_checkpoint_risk(w4, load_weight_lb=35000)
    assert cp4['wind_level'] == RISK_NO_TRAVEL
    assert cp4['overall_level'] == RISK_NO_TRAVEL
    print("PASS - overall risk selection")


def test_severe_mile_ranking():
    r1 = {
        'id': 1,
        'duration_seconds': 3000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 5.0, 'high_miles': 0.0, 'average_risk': 1.5},
    }
    r2 = {
        'id': 2,
        'duration_seconds': 4000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 20.0, 'average_risk': 2.2},
    }
    rec = evaluate_route_recommendations([r1, r2])
    assert rec['recommended_route_id'] == 2, "Priority 1 (Fewest Severe miles) must select Route 2"
    print("PASS - Severe-mile ranking")


def test_high_mile_ranking():
    r1 = {
        'id': 1,
        'duration_seconds': 3000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 15.0, 'average_risk': 1.2},
    }
    r2 = {
        'id': 2,
        'duration_seconds': 4000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 5.0, 'average_risk': 1.8},
    }
    rec = evaluate_route_recommendations([r1, r2])
    assert rec['recommended_route_id'] == 2, "Priority 2 (Fewest High miles on severe tie) must select Route 2"
    print("PASS - High-mile ranking")


def test_average_risk_ranking():
    r1 = {
        'id': 1,
        'duration_seconds': 3000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.8},
    }
    r2 = {
        'id': 2,
        'duration_seconds': 4000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.1},
    }
    rec = evaluate_route_recommendations([r1, r2])
    assert rec['recommended_route_id'] == 2, "Priority 3 (Lowest Average Risk on severe/high tie) must select Route 2"
    print("PASS - average-risk ranking")


def test_duration_ranking():
    r1 = {
        'id': 1,
        'duration_seconds': 5000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.5},
    }
    r2 = {
        'id': 2,
        'duration_seconds': 3000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.5},
    }
    rec = evaluate_route_recommendations([r1, r2])
    assert rec['recommended_route_id'] == 2, "Priority 4 (Shortest Duration on risk tie) must select Route 2"
    print("PASS - duration ranking")


def test_notravel_exclusion():
    r1 = {
        'id': 1,
        'duration_seconds': 2000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 1.5, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None, 'has_no_travel': True},
    }
    r2 = {
        'id': 2,
        'duration_seconds': 4000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 10.0, 'average_risk': 1.6, 'has_no_travel': False},
    }
    routes = [r1, r2]
    rec = evaluate_route_recommendations(routes)
    assert rec['recommended_route_id'] == 2, "Route with No Travel must be excluded from recommendation"
    assert r1['is_recommended'] is False, "No Travel route must not be marked recommended"
    assert r2['is_recommended'] is True, "Safe eligible route must be marked recommended"
    assert len(routes) == 2, "No Travel route must remain visible in routes list"
    print("PASS - No Travel exclusion")


def test_all_unsafe_behavior():
    r1 = {
        'id': 1,
        'duration_seconds': 3000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 5.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None, 'has_no_travel': True},
    }
    r2 = {
        'id': 2,
        'duration_seconds': 3500,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 2.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None, 'has_no_travel': True},
    }
    rec = evaluate_route_recommendations([r1, r2])
    assert rec['recommended_route_id'] is None, "No route should be recommended when all are unsafe"
    assert rec['status'] == REC_STATUS_NO_SAFE_ROUTE, f"Status must be {REC_STATUS_NO_SAFE_ROUTE}"
    assert rec['all_routes_unsafe'] is True, "all_routes_unsafe must be True"
    assert rec['is_weather_complete'] is True, "is_weather_complete must be True when all are assessed"
    assert rec['message'] == "No safe route available under current weather conditions.", "Must match exact all-unsafe message"
    print("PASS - all-unsafe behavior")


def test_incomplete_weather_behavior():
    # 1. Incomplete route must NOT be classified as safely Low
    incomplete_route = {
        'id': 1,
        'distance_miles': 100.0,
        'duration_seconds': 3600,
        'checkpoints': [
            {'id': 1, 'segment_miles': 50.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
            {'id': 2, 'segment_miles': 50.0, 'risk': {'assessment_status': 'unavailable'}},
        ]
    }
    rs = aggregate_route_risk(incomplete_route)
    assert rs['is_fully_assessed'] is False, "Route with unavailable checkpoint cannot be fully assessed"
    assert rs['unassessed_miles'] == 50.0, "Unassessed miles must equal 50.0"
    assert rs['low_miles'] == 50.0, "Only assessed segment can be Low miles"
    assert rs['average_risk'] is None, "Average risk must be None when incomplete"

    # 2. Fully assessed safe route alongside incomplete alternative -> safe route is recommended
    safe_r = {
        'id': 1,
        'duration_seconds': 4000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.0, 'has_no_travel': False},
    }
    inc_r = {
        'id': 2,
        'duration_seconds': 3000,
        'risk_summary': {'is_fully_assessed': False, 'unassessed_miles': 25.0, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None, 'has_no_travel': False},
    }
    rec_mixed = evaluate_route_recommendations([safe_r, inc_r])
    assert rec_mixed['recommended_route_id'] == 1, "Safe route must be recommended despite incomplete alternative"
    assert rec_mixed['status'] == REC_STATUS_RECOMMENDED
    assert rec_mixed['is_weather_complete'] is False, "is_weather_complete must be False"
    assert rec_mixed['all_routes_unsafe'] is False, "all_routes_unsafe must be False"

    # 3. No fully assessed safe route + incomplete alternative -> return incomplete_weather status (NOT all_routes_unsafe)
    unsafe_r = {
        'id': 1,
        'duration_seconds': 4000,
        'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 10.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None, 'has_no_travel': True},
    }
    rec_inc_unsafe = evaluate_route_recommendations([unsafe_r, inc_r])
    assert rec_inc_unsafe['recommended_route_id'] is None
    assert rec_inc_unsafe['status'] == REC_STATUS_INCOMPLETE_WEATHER
    assert rec_inc_unsafe['all_routes_unsafe'] is False, "Must NOT claim all routes unsafe when some are incomplete"
    assert rec_inc_unsafe['is_weather_complete'] is False
    print("PASS - incomplete-weather behavior")


def test_distance_conservation():
    # Synthetic geometry of ~100 km (~62.137 miles)
    coords = [
        {'latitude': 41.8781 + i * 0.01, 'longitude': -87.6298 + i * 0.01}
        for i in range(100)
    ]
    # Synthetic progress markers and total distance
    total_meters = 100000.0  # 100 km = ~62.137 miles
    total_seconds = 3600
    total_miles = total_meters / 1609.344

    cps = generate_checkpoints(
        geometry=coords,
        progress_markers=[],
        total_distance_m=total_meters,
        total_time_s=total_seconds,
        departure_str='2026-11-04T08:00:00',
        interval_miles=25,
        total_miles=round(total_miles, 1),
    )
    seg_sum = sum(cp['segment_miles'] for cp in cps)
    route_dist = round(total_miles, 1)
    
    # Verify sum of segment distances ~= total route distance
    assert abs(seg_sum - route_dist) < 0.05, f"Segment sum {seg_sum} diverges from route distance {route_dist}"
    # Verify it does NOT equal checkpoint_count * interval_miles
    naive_product = len(cps) * 25.0
    assert seg_sum != naive_product, "Segment distance must not be naive checkpoint_count * interval"
    print("PASS - distance conservation")


def main():
    print("=" * 60)
    print("ASSIGNMENT COMPLIANCE VERIFICATION SUITE")
    print("=" * 60)
    test_wind_boundaries()
    test_cargo_overrides()
    test_rain_boundaries()
    test_snow_boundaries()
    test_overall_risk_selection()
    test_severe_mile_ranking()
    test_high_mile_ranking()
    test_average_risk_ranking()
    test_duration_ranking()
    test_notravel_exclusion()
    test_all_unsafe_behavior()
    test_incomplete_weather_behavior()
    test_distance_conservation()
    print("=" * 60)
    print("ALL 13 ASSIGNMENT COMPLIANCE CHECKS PASSED")
    print("=" * 60)


if __name__ == '__main__':
    main()

