from datetime import datetime, timezone, timedelta
import requests
from unittest.mock import patch, MagicMock
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from .utils import (
    meters_to_miles,
    seconds_to_minutes,
    haversine_distance,
    interpolate_coordinate,
)
from .services import (
    geocode_location,
    fetch_tomtom_routes,
    normalize_routes,
    generate_checkpoints,
    process_progress_markers,
    fetch_open_meteo_batch,
    parse_weather_hourly,
    match_checkpoint_weather,
    attach_weather_to_routes,
    RouteServiceError,
)
from .risk import (
    RISK_LOW,
    RISK_MODERATE,
    RISK_HIGH,
    RISK_SEVERE,
    RISK_NO_TRAVEL,
    classify_wind_risk,
    classify_rain_risk,
    classify_snow_risk,
    assess_checkpoint_risk,
    aggregate_route_risk,
    evaluate_route_recommendations,
    process_route_risks,
    MSG_NO_SAFE_ROUTE,
    MSG_INCOMPLETE_WEATHER,
)


def make_mock_tomtom_route(
    length_m=1619000,
    travel_time_s=51000,
    dep='2026-11-03T08:00:00-06:00',
    arr='2026-11-03T21:10:00-07:00',
    has_non_truck=True,
    include_progress=True,
):
    points = [
        {'latitude': 41.88323, 'longitude': -87.63240},
        {'latitude': 41.50000, 'longitude': -90.00000},
        {'latitude': 39.73920, 'longitude': -104.99030},
    ]

    progress = []
    if include_progress:
        progress = [
            {'pointIndex': 0, 'distanceInMeters': 0, 'travelTimeInSeconds': 0},
            {'pointIndex': 1, 'distanceInMeters': int(length_m / 2), 'travelTimeInSeconds': int(travel_time_s / 2)},
            {'pointIndex': 2, 'distanceInMeters': length_m, 'travelTimeInSeconds': travel_time_s},
        ]

    sections = [
        {'startPointIndex': 0, 'endPointIndex': 1, 'sectionType': 'TRAVEL_MODE', 'travelMode': 'truck'},
    ]
    if has_non_truck:
        sections.append(
            {'startPointIndex': 1, 'endPointIndex': 2, 'sectionType': 'TRAVEL_MODE', 'travelMode': 'other'}
        )
    else:
        sections.append(
            {'startPointIndex': 1, 'endPointIndex': 2, 'sectionType': 'TRAVEL_MODE', 'travelMode': 'truck'}
        )

    return {
        'summary': {
            'lengthInMeters': length_m,
            'travelTimeInSeconds': travel_time_s,
            'departureTime': dep,
            'arrivalTime': arr,
        },
        'legs': [{'points': points}],
        'progress': progress,
        'sections': sections,
    }


def make_mock_weather_response(num_locations=1, base_time=1793714400):
    locs = [
        {
            'latitude': 41.88,
            'longitude': -87.63,
            'hourly': {
                'time': [base_time + i * 3600 for i in range(168)],
                'wind_speed_10m': [15.0 + i * 0.1 for i in range(168)],
                'rain': [0.05 for _ in range(168)],
                'snowfall': [0.0 for _ in range(168)],
            },
        }
        for _ in range(num_locations)
    ]
    return locs if num_locations > 1 else locs[0]


class HealthCheckTests(SimpleTestCase):
    def test_health_check_returns_ok(self):
        response = self.client.get(reverse('health_check'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_health_check_method_not_allowed(self):
        response = self.client.post(reverse('health_check'))
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class UnitConversionTests(SimpleTestCase):
    def test_meters_to_miles(self):
        self.assertEqual(meters_to_miles(1609.344), 1.0)
        self.assertEqual(meters_to_miles(1619000), 1006.0)

    def test_seconds_to_minutes(self):
        self.assertEqual(seconds_to_minutes(3600), 60)
        self.assertEqual(seconds_to_minutes(51000), 850)

    def test_haversine_distance(self):
        # Chicago to Denver is roughly 1470 km
        chicago = {'latitude': 41.8781, 'longitude': -87.6298}
        denver = {'latitude': 39.7392, 'longitude': -104.9903}
        dist = haversine_distance(chicago, denver)
        self.assertTrue(1400000 < dist < 1600000)

    def test_interpolate_coordinate(self):
        c1 = {'latitude': 10.0, 'longitude': 20.0}
        c2 = {'latitude': 20.0, 'longitude': 40.0}
        mid = interpolate_coordinate(c1, c2, 0.5)
        self.assertEqual(mid, {'latitude': 15.0, 'longitude': 30.0})

        start = interpolate_coordinate(c1, c2, 0.0)
        self.assertEqual(start, {'latitude': 10.0, 'longitude': 20.0})

        end = interpolate_coordinate(c1, c2, 1.0)
        self.assertEqual(end, {'latitude': 20.0, 'longitude': 40.0})


class RouteValidationTests(SimpleTestCase):
    url = reverse('calculate_routes')

    def test_missing_origin(self):
        payload = {
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_missing_destination(self):
        payload = {
            'origin': 'Chicago, IL',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_same_origin_and_destination(self):
        payload = {
            'origin': 'Chicago, IL',
            'destination': 'chicago, il',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_missing_departure(self):
        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'load_weight_lb': 38000,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_invalid_departure_format(self):
        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': 'invalid-date-format',
            'load_weight_lb': 38000,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_missing_load_weight(self):
        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_non_numeric_load_weight(self):
        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 'heavy',
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_zero_or_negative_load_weight(self):
        for invalid_weight in (0, -500):
            payload = {
                'origin': 'Chicago, IL',
                'destination': 'Denver, CO',
                'departure': '2026-11-03T08:00:00',
                'load_weight_lb': invalid_weight,
            }
            res = self.client.post(self.url, payload, content_type='application/json')
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')

    def test_unsupported_method_get(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_invalid_interval_miles_rejected(self):
        for invalid_interval in (15, -25, 0, '25', True, False, 100, 25.5):
            payload = {
                'origin': 'Chicago, IL',
                'destination': 'Denver, CO',
                'departure': '2026-11-03T08:00:00',
                'load_weight_lb': 38000,
                'interval_miles': invalid_interval,
            }
            res = self.client.post(self.url, payload, content_type='application/json')
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(res.json()['error']['code'], 'VALIDATION_ERROR')


class TomTomGeocodingTests(SimpleTestCase):
    @patch('requests.get')
    def test_geocoding_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'results': [
                {
                    'position': {'lat': 41.88323, 'lon': -87.63240},
                    'matchConfidence': {'score': 1.0},
                    'address': {'freeformAddress': 'Chicago, IL'},
                }
            ]
        }
        mock_get.return_value = mock_resp

        coords = geocode_location('Chicago, IL', 'test-tomtom-key')
        self.assertEqual(coords, {'latitude': 41.88323, 'longitude': -87.6324})

    @patch('requests.get')
    def test_geocoding_is_limited_to_us(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'results': [{'position': {'lat': 41.14, 'lon': -104.82}, 'score': 4.45}]
        }
        mock_get.return_value = mock_resp

        geocode_location('Cheyenne, WY', 'test-tomtom-key')
        self.assertEqual(mock_get.call_args.kwargs['params']['countrySet'], 'US')

    @patch('requests.get')
    def test_geocoding_ambiguous_results(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Two distant candidates with identical matchConfidence
        mock_resp.json.return_value = {
            'results': [
                {
                    'position': {'lat': 39.7817, 'lon': -89.6501},
                    'matchConfidence': {'score': 1.0},
                    'address': {'freeformAddress': 'Springfield, IL, USA'},
                },
                {
                    'position': {'lat': 37.2089, 'lon': -93.2923},
                    'matchConfidence': {'score': 1.0},
                    'address': {'freeformAddress': 'Springfield, MO, USA'},
                },
            ]
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(RouteServiceError) as ctx:
            geocode_location('Springfield', 'test-tomtom-key')
        self.assertEqual(ctx.exception.code, 'LOCATION_AMBIGUOUS')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('Springfield, IL', ctx.exception.message)
        self.assertIn('Springfield, MO', ctx.exception.message)

    @patch('requests.get')
    def test_geocoding_no_results(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'results': []}
        mock_get.return_value = mock_resp

        with self.assertRaises(RouteServiceError) as ctx:
            geocode_location('NowhereLand12345', 'test-tomtom-key')
        self.assertEqual(ctx.exception.code, 'GEOCODE_NOT_FOUND')
        self.assertEqual(ctx.exception.status_code, 404)

    @patch('requests.get')
    def test_geocoding_timeout(self, mock_get):
        mock_get.side_effect = requests.Timeout('Timed out')

        with self.assertRaises(RouteServiceError) as ctx:
            geocode_location('Chicago, IL', 'test-tomtom-key')
        self.assertEqual(ctx.exception.code, 'PROVIDER_TIMEOUT')
        self.assertEqual(ctx.exception.status_code, 504)

    @patch('requests.get')
    def test_geocoding_unauthorized(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp

        with self.assertRaises(RouteServiceError) as ctx:
            geocode_location('Chicago, IL', 'bad-key')
        self.assertEqual(ctx.exception.code, 'PROVIDER_UNAUTHORIZED')
        self.assertEqual(ctx.exception.status_code, 502)


class TomTomRoutingTests(SimpleTestCase):
    origin = {'latitude': 41.88323, 'longitude': -87.63240}
    dest = {'latitude': 39.73920, 'longitude': -104.99030}

    @patch('requests.get')
    def test_fetch_tomtom_routes_parameters(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'routes': [make_mock_tomtom_route()]}
        mock_get.return_value = mock_resp

        fetch_tomtom_routes(self.origin, self.dest, '2026-11-03T08:00:00', 'test-key')

        self.assertTrue(mock_get.called)
        called_url = mock_get.call_args[0][0]
        self.assertIn('https://api.tomtom.com/routing/1/calculateRoute/', called_url)
        self.assertIn('41.88323,-87.6324:39.7392,-104.9903', called_url)

        called_params = mock_get.call_args[1]['params']
        # extendedRouteRepresentation is sent twice, so params must be a list of tuples
        self.assertIn(('extendedRouteRepresentation', 'distance'), called_params)
        self.assertIn(('extendedRouteRepresentation', 'travelTime'), called_params)
        self.assertIn(('travelMode', 'truck'), called_params)
        self.assertIn(('maxAlternatives', 2), called_params)
        self.assertIn(('departAt', '2026-11-03T08:00:00'), called_params)

        # Load weight stays on our side; TomTom never sees it
        param_keys = [p[0] for p in called_params]
        self.assertNotIn('vehicleWeight', param_keys)
        self.assertNotIn('vehicleGrossWeight', param_keys)

    def test_single_route_normalization(self):
        raw_data = {
            'routes': [
                make_mock_tomtom_route(
                    length_m=1619000,
                    travel_time_s=51000,
                    dep='2026-11-03T08:00:00-06:00',
                    arr='2026-11-03T21:10:00-07:00',
                    has_non_truck=True,
                )
            ]
        }
        routes = normalize_routes(raw_data, self.origin, self.dest)
        self.assertEqual(len(routes), 1)

        r = routes[0]
        self.assertEqual(r['id'], 1)
        self.assertEqual(r['distance_miles'], 1006.0)
        self.assertEqual(r['duration_seconds'], 51000)
        self.assertEqual(r['duration_minutes'], 850)
        self.assertEqual(r['departure'], '2026-11-03T08:00:00-06:00')
        self.assertEqual(r['arrival'], '2026-11-03T21:10:00-07:00')
        self.assertEqual(len(r['geometry']), 3)
        self.assertEqual(len(r['progress']), 3)
        self.assertEqual(r['progress_source'], 'provider')
        self.assertTrue(r['has_non_truck_sections'])
        self.assertTrue(any('travelMode: other' in w for w in r['warnings']))

    def test_progress_markers_sorted_and_bounded(self):
        # Progress markers out-of-order and containing out-of-bounds index
        route_data = make_mock_tomtom_route(include_progress=False)
        route_data['progress'] = [
            {'pointIndex': 2, 'distanceInMeters': 1619000, 'travelTimeInSeconds': 51000},
            {'pointIndex': 999, 'distanceInMeters': 9999999, 'travelTimeInSeconds': 999999},  # Out of bounds
            {'pointIndex': 0, 'distanceInMeters': 0, 'travelTimeInSeconds': 0},
            {'pointIndex': 1, 'distanceInMeters': 800000, 'travelTimeInSeconds': 25000},
        ]
        routes = normalize_routes({'routes': [route_data]}, self.origin, self.dest)
        progress = routes[0]['progress']
        self.assertEqual(len(progress), 3)  # Index 999 filtered out
        self.assertEqual([p['point_index'] for p in progress], [0, 1, 2])
        self.assertEqual(routes[0]['progress_source'], 'provider')

    def test_three_alternatives_preserved(self):
        raw_data = {
            'routes': [
                make_mock_tomtom_route(length_m=1600000, travel_time_s=50000),
                make_mock_tomtom_route(length_m=1700000, travel_time_s=53000),
                make_mock_tomtom_route(length_m=1800000, travel_time_s=56000),
                make_mock_tomtom_route(length_m=1900000, travel_time_s=59000),
            ]
        }
        routes = normalize_routes(raw_data, self.origin, self.dest)
        self.assertEqual(len(routes), 3)
        self.assertEqual([r['id'] for r in routes], [1, 2, 3])

    def test_fewer_than_three_alternatives_preserved_without_fabrication(self):
        raw_data = {
            'routes': [
                make_mock_tomtom_route(length_m=1600000, travel_time_s=50000),
                make_mock_tomtom_route(length_m=1700000, travel_time_s=53000),
            ]
        }
        routes = normalize_routes(raw_data, self.origin, self.dest)
        self.assertEqual(len(routes), 2)
        self.assertEqual([r['id'] for r in routes], [1, 2])

    def test_truck_compatible_route_no_warning(self):
        raw_data = {'routes': [make_mock_tomtom_route(has_non_truck=False)]}
        routes = normalize_routes(raw_data, self.origin, self.dest)
        self.assertFalse(routes[0]['has_non_truck_sections'])
        self.assertEqual(len(routes[0]['warnings']), 0)

    def test_missing_progress_markers_synthesized_with_approximation_warning(self):
        raw_data = {'routes': [make_mock_tomtom_route(include_progress=False)]}
        routes = normalize_routes(raw_data, self.origin, self.dest)
        self.assertEqual(routes[0]['progress_source'], 'interpolated_fallback')
        progress = routes[0]['progress']
        self.assertEqual(len(progress), 2)
        self.assertEqual(progress[0]['point_index'], 0)
        self.assertEqual(progress[1]['point_index'], 2)
        self.assertTrue(any('linear approximation' in w for w in routes[0]['warnings']))

    @patch('requests.get')
    def test_error_sanitization_redacts_api_key(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {
            'error': {'description': 'Internal error for query with key=secret-key-12345'}
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(RouteServiceError) as ctx:
            fetch_tomtom_routes(self.origin, self.dest, '2026-11-03T08:00:00', 'secret-key-12345')

        self.assertNotIn('secret-key-12345', ctx.exception.message)
        self.assertIn('[REDACTED]', ctx.exception.message)


class CheckpointGenerationTests(SimpleTestCase):
    def setUp(self):
        # 100-mile route (160934.4 m) taking 2 hours
        self.points = [
            {'latitude': 40.0, 'longitude': -90.0},
            {'latitude': 40.0, 'longitude': -89.5},
            {'latitude': 40.0, 'longitude': -89.0},
            {'latitude': 40.0, 'longitude': -88.5},
            {'latitude': 40.0, 'longitude': -88.0},
        ]
        self.total_m = 160934.4  # 100.0 miles
        self.total_s = 7200      # 2 hours
        self.dep_time = '2026-11-03T08:00:00-06:00'
        # Progress markers at points 0, 2, 4
        self.progress = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {'point_index': 2, 'distance_meters': 80467.2, 'travel_time_seconds': 3600},
            {'point_index': 4, 'distance_meters': 160934.4, 'travel_time_seconds': 7200},
        ]

    def test_25_mile_intervals_sampling(self):
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, self.dep_time, 25)
        # 0, 25, 50, 75, 100
        self.assertEqual(len(cps), 5)
        markers = [cp['mile_marker'] for cp in cps]
        self.assertEqual(markers, [0.0, 25.0, 50.0, 75.0, 100.0])

    def test_10_mile_intervals_sampling(self):
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, self.dep_time, 10)
        # 0, 10, 20, ..., 100
        self.assertEqual(len(cps), 11)
        self.assertEqual(cps[0]['mile_marker'], 0.0)
        self.assertEqual(cps[-1]['mile_marker'], 100.0)

    def test_50_mile_intervals_sampling(self):
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, self.dep_time, 50)
        # 0, 50, 100
        self.assertEqual(len(cps), 3)
        markers = [cp['mile_marker'] for cp in cps]
        self.assertEqual(markers, [0.0, 50.0, 100.0])

    def test_origin_and_destination_inclusion(self):
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, self.dep_time, 25)
        self.assertEqual(cps[0]['mile_marker'], 0.0)
        self.assertEqual(cps[0]['segment_miles'], 0.0)
        self.assertEqual(cps[0]['latitude'], self.points[0]['latitude'])
        self.assertEqual(cps[0]['longitude'], self.points[0]['longitude'])
        self.assertEqual(cps[-1]['mile_marker'], 100.0)
        self.assertEqual(cps[-1]['latitude'], self.points[-1]['latitude'])
        self.assertEqual(cps[-1]['longitude'], self.points[-1]['longitude'])

    def test_route_shorter_than_interval(self):
        # 8-mile route with 25-mile sampling
        short_m = 8.0 * 1609.344
        short_s = 600
        short_points = [self.points[0], self.points[1]]
        short_prog = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {'point_index': 1, 'distance_meters': short_m, 'travel_time_seconds': short_s},
        ]
        cps = generate_checkpoints(short_points, short_prog, short_m, short_s, self.dep_time, 25)
        self.assertEqual(len(cps), 2)
        self.assertEqual(cps[0]['mile_marker'], 0.0)
        self.assertEqual(cps[1]['mile_marker'], 8.0)
        self.assertEqual(cps[1]['segment_miles'], 8.0)

    def test_exact_interval_multiple_no_duplicate(self):
        # 50 miles total, interval 25
        m_50 = 50.0 * 1609.344
        prog_50 = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {'point_index': 2, 'distance_meters': m_50, 'travel_time_seconds': 3600},
        ]
        cps = generate_checkpoints(self.points[:3], prog_50, m_50, 3600, self.dep_time, 25)
        self.assertEqual(len(cps), 3)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 25.0, 50.0])

    def test_final_partial_segment(self):
        # 106.3 miles total, interval 50
        partial_m = 106.3 * 1609.344
        prog_part = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {'point_index': 4, 'distance_meters': partial_m, 'travel_time_seconds': 7500},
        ]
        cps = generate_checkpoints(self.points, prog_part, partial_m, 7500, self.dep_time, 50)
        self.assertEqual(len(cps), 4)  # 0, 50, 100, 106.3
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 50.0, 100.0, 106.3])
        self.assertEqual(cps[-1]['segment_miles'], 6.3)

    def test_multiple_route_legs_geometry(self):
        leg1 = [{'latitude': 40.0, 'longitude': -90.0}, {'latitude': 40.5, 'longitude': -89.5}]
        leg2 = [{'latitude': 40.5, 'longitude': -89.5}, {'latitude': 41.0, 'longitude': -89.0}]
        raw_route = {
            'summary': {'lengthInMeters': 160934.4, 'travelTimeInSeconds': 7200, 'departureTime': self.dep_time},
            'legs': [{'points': leg1}, {'points': leg2}],
            'progress': [
                {'pointIndex': 0, 'distanceInMeters': 0, 'travelTimeInSeconds': 0},
                {'pointIndex': 3, 'distanceInMeters': 160934, 'travelTimeInSeconds': 7200},
            ],
        }
        res = normalize_routes(
            {'routes': [raw_route]},
            {'latitude': 40.0, 'longitude': -90.0},
            {'latitude': 41.0, 'longitude': -89.0},
            interval_miles=25,
        )
        self.assertEqual(len(res[0]['geometry']), 4)
        self.assertGreater(len(res[0]['checkpoints']), 0)

    def test_repeated_coordinates_and_zero_distance_edges(self):
        repeated_points = [
            {'latitude': 40.0, 'longitude': -90.0},
            {'latitude': 40.0, 'longitude': -90.0},  # duplicate edge
            {'latitude': 41.0, 'longitude': -89.0},
        ]
        prog = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {'point_index': 2, 'distance_meters': 100000, 'travel_time_seconds': 4000},
        ]
        cps = generate_checkpoints(repeated_points, prog, 100000, 4000, self.dep_time, 25)
        self.assertGreater(len(cps), 1)

    def test_coordinate_interpolation_between_points(self):
        two_points = [
            {'latitude': 0.0, 'longitude': 0.0},
            {'latitude': 0.0, 'longitude': 2.0},
        ]
        total_m = 200000.0
        prog = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {'point_index': 1, 'distance_meters': total_m, 'travel_time_seconds': 5000},
        ]
        cps = generate_checkpoints(two_points, prog, total_m, 5000, self.dep_time, 25)
        for cp in cps:
            self.assertEqual(cp['latitude'], 0.0)
            self.assertTrue(0.0 <= cp['longitude'] <= 2.0)

    def test_progress_marker_cleaning_and_fallback(self):
        raw = [
            {'pointIndex': -1, 'distanceInMeters': 0, 'travelTimeInSeconds': 0},
            {'pointIndex': 999, 'distanceInMeters': 1000, 'travelTimeInSeconds': 100},
            {'pointIndex': 1, 'distanceInMeters': -500, 'travelTimeInSeconds': 100},
            {'pointIndex': 2, 'distanceInMeters': 50000, 'travelTimeInSeconds': -10},
        ]
        markers, source, warnings = process_progress_markers(raw, 5, 100000, 4000)
        self.assertEqual(source, 'interpolated_fallback')
        self.assertEqual(len(markers), 2)
        self.assertTrue(any('linear approximation' in w for w in warnings))

    def test_sparse_progress_markers_interpolation(self):
        sparse_prog = [
            {'point_index': 0, 'distance_meters': 0.0, 'travel_time_seconds': 0},
            {'point_index': 4, 'distance_meters': self.total_m, 'travel_time_seconds': self.total_s},
        ]
        cps = generate_checkpoints(self.points, sparse_prog, self.total_m, self.total_s, self.dep_time, 25)
        self.assertEqual(len(cps), 5)
        mid_cp = cps[2]
        self.assertEqual(mid_cp['mile_marker'], 50.0)
        dep_dt = datetime.fromisoformat(self.dep_time)
        mid_dt = datetime.fromisoformat(mid_cp['eta'])
        self.assertAlmostEqual((mid_dt - dep_dt).total_seconds(), 3600, delta=120)

    def test_eta_timezone_preservation(self):
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, '2026-11-03T08:00:00-06:00', 25)
        for cp in cps:
            self.assertTrue(cp['eta'].endswith('-06:00'))

    def test_eta_monotonicity(self):
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, self.dep_time, 10)
        unix_times = [cp['eta_unix'] for cp in cps]
        self.assertEqual(unix_times, sorted(unix_times))
        for i in range(1, len(unix_times)):
            self.assertGreaterEqual(unix_times[i], unix_times[i - 1])

    def test_daylight_saving_timestamp(self):
        dep_dst = '2026-11-01T01:30:00-05:00'
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, dep_dst, 25)
        dep_unix = cps[0]['eta_unix']
        arr_unix = cps[-1]['eta_unix']
        self.assertEqual(arr_unix - dep_unix, 7200)

    def test_segment_distances_sum_equals_total(self):
        cps = generate_checkpoints(self.points, self.progress, self.total_m, self.total_s, self.dep_time, 25)
        self.assertEqual(cps[0]['segment_miles'], 0.0)
        sum_segments = sum(cp['segment_miles'] for cp in cps)
        self.assertAlmostEqual(sum_segments, 100.0, places=1)

    def test_edge_case_24_99_miles(self):
        cps = generate_checkpoints(self.points, [], 24.99 * 1609.344, 1800, self.dep_time, 25, total_miles=24.99)
        self.assertEqual(len(cps), 2)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 24.99])
        self.assertEqual([cp['segment_miles'] for cp in cps], [0.0, 24.99])
        self.assertAlmostEqual(sum(cp['segment_miles'] for cp in cps), 24.99, places=2)

    def test_edge_case_25_00_miles(self):
        cps = generate_checkpoints(self.points, [], 25.00 * 1609.344, 1800, self.dep_time, 25, total_miles=25.00)
        self.assertEqual(len(cps), 2)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 25.0])
        self.assertEqual([cp['segment_miles'] for cp in cps], [0.0, 25.0])
        self.assertAlmostEqual(sum(cp['segment_miles'] for cp in cps), 25.0, places=2)

    def test_edge_case_25_01_miles(self):
        cps = generate_checkpoints(self.points, [], 25.01 * 1609.344, 1801, self.dep_time, 25, total_miles=25.01)
        self.assertEqual(len(cps), 3)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 25.0, 25.01])
        self.assertEqual([cp['segment_miles'] for cp in cps], [0.0, 25.0, 0.01])
        self.assertAlmostEqual(sum(cp['segment_miles'] for cp in cps), 25.01, places=2)

    def test_edge_case_25_02_miles(self):
        cps = generate_checkpoints(self.points, [], 25.02 * 1609.344, 1802, self.dep_time, 25, total_miles=25.02)
        self.assertEqual(len(cps), 3)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 25.0, 25.02])
        self.assertEqual([cp['segment_miles'] for cp in cps], [0.0, 25.0, 0.02])
        self.assertAlmostEqual(sum(cp['segment_miles'] for cp in cps), 25.02, places=2)

    def test_edge_case_49_99_miles(self):
        cps = generate_checkpoints(self.points, [], 49.99 * 1609.344, 3599, self.dep_time, 25, total_miles=49.99)
        self.assertEqual(len(cps), 3)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 25.0, 49.99])
        self.assertEqual([cp['segment_miles'] for cp in cps], [0.0, 25.0, 24.99])
        self.assertAlmostEqual(sum(cp['segment_miles'] for cp in cps), 49.99, places=2)

    def test_edge_case_50_00_miles(self):
        cps = generate_checkpoints(self.points, [], 50.00 * 1609.344, 3600, self.dep_time, 25, total_miles=50.00)
        self.assertEqual(len(cps), 3)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 25.0, 50.0])
        self.assertEqual([cp['segment_miles'] for cp in cps], [0.0, 25.0, 25.0])
        self.assertAlmostEqual(sum(cp['segment_miles'] for cp in cps), 50.0, places=2)

    def test_edge_case_50_01_miles(self):
        cps = generate_checkpoints(self.points, [], 50.01 * 1609.344, 3601, self.dep_time, 25, total_miles=50.01)
        self.assertEqual(len(cps), 4)
        self.assertEqual([cp['mile_marker'] for cp in cps], [0.0, 25.0, 50.0, 50.01])
        self.assertEqual([cp['segment_miles'] for cp in cps], [0.0, 25.0, 25.0, 0.01])
        self.assertAlmostEqual(sum(cp['segment_miles'] for cp in cps), 50.01, places=2)

    def test_efficiency_15000_geometry_points(self):
        import time
        N = 15000
        large_geometry = [
            {'latitude': 40.0 + i * 0.0005, 'longitude': -90.0 + i * 0.0005}
            for i in range(N)
        ]
        start_time = time.perf_counter()
        cps = generate_checkpoints(
            large_geometry,
            [],
            1000.0 * 1609.344,
            50000,
            self.dep_time,
            interval_miles=25,
            total_miles=1000.0,
        )
        duration = time.perf_counter() - start_time
        self.assertEqual(len(cps), 41)
        self.assertLess(duration, 0.5)


@override_settings(TOMTOM_API_KEY='valid-test-key')
class EndToEndRouteEndpointTests(SimpleTestCase):
    url = reverse('calculate_routes')

    @patch('requests.get')
    def test_successful_tomtom_route_calculation(self, mock_get):
        def mocked_get(url, params=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            if 'geocode' in url:
                if 'Chicago' in url:
                    resp.json.return_value = {
                        'results': [{'position': {'lat': 41.88323, 'lon': -87.6324}, 'matchConfidence': {'score': 1.0}}]
                    }
                else:
                    resp.json.return_value = {
                        'results': [{'position': {'lat': 39.7392, 'lon': -104.9903}, 'matchConfidence': {'score': 1.0}}]
                    }
            elif 'open-meteo' in url or 'forecast' in url:
                lats = params.get('latitude', '').split(',') if isinstance(params, dict) else ['0']
                resp.json.return_value = make_mock_weather_response(len(lats))
            else:
                resp.json.return_value = {'routes': [make_mock_tomtom_route()]}
            return resp

        mock_get.side_effect = mocked_get

        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertIn('routes', data)
        self.assertEqual(len(data['routes']), 1)
        r = data['routes'][0]
        self.assertEqual(r['id'], 1)
        self.assertEqual(r['distance_miles'], 1006.0)
        self.assertEqual(r['departure'], '2026-11-03T08:00:00-06:00')
        self.assertIn('geometry', r)
        self.assertIn('progress', r)
        self.assertIn('has_non_truck_sections', r)
        self.assertEqual(r['interval_miles'], 25)
        self.assertEqual(r['checkpoint_count'], len(r['checkpoints']))
        self.assertGreater(len(r['checkpoints']), 0)
        self.assertEqual(r['checkpoints'][0]['mile_marker'], 0.0)
        self.assertEqual(r['checkpoints'][0]['segment_miles'], 0.0)
        self.assertIn('eta', r['checkpoints'][0])
        self.assertIn('eta_unix', r['checkpoints'][0])
        self.assertIn('weather', r['checkpoints'][0])
        self.assertEqual(r['checkpoints'][0]['weather']['status'], 'available')

        # The API key must never leak into the response
        self.assertNotIn('valid-test-key', str(data))

    @patch('requests.get')
    def test_explicit_interval_miles_10(self, mock_get):
        def mocked_get(url, params=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            if 'geocode' in url:
                resp.json.return_value = {'results': [{'position': {'lat': 41.88323, 'lon': -87.6324}}]}
            elif 'open-meteo' in url or 'forecast' in url:
                lats = params.get('latitude', '').split(',') if isinstance(params, dict) else ['0']
                resp.json.return_value = make_mock_weather_response(len(lats))
            else:
                resp.json.return_value = {'routes': [make_mock_tomtom_route()]}
            return resp

        mock_get.side_effect = mocked_get

        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
            'interval_miles': 10,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        r = res.json()['routes'][0]
        self.assertEqual(r['interval_miles'], 10)
        self.assertEqual(r['checkpoint_count'], len(r['checkpoints']))
        # 1006.0 miles / 10 = ~102 checkpoints
        self.assertGreater(r['checkpoint_count'], 100)
        self.assertIn('weather', r['checkpoints'][0])

    @patch('requests.get')
    def test_accepts_load_weight_alias(self, mock_get):
        def mocked_get(url, params=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            if 'geocode' in url:
                resp.json.return_value = {'results': [{'position': {'lat': 41.88323, 'lon': -87.6324}}]}
            elif 'open-meteo' in url or 'forecast' in url:
                lats = params.get('latitude', '').split(',') if isinstance(params, dict) else ['0']
                resp.json.return_value = make_mock_weather_response(len(lats))
            else:
                resp.json.return_value = {'routes': [make_mock_tomtom_route()]}
            return resp

        mock_get.side_effect = mocked_get

        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight': 38000,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    @patch('requests.get')
    def test_naive_departure_preserves_chicago_local_time_throughout_flow(self, mock_get):
        captured_calls = []

        def mocked_get(url, params=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            if 'geocode' in url:
                resp.json.return_value = {'results': [{'position': {'lat': 41.88323, 'lon': -87.6324}}]}
            elif 'open-meteo' in url or 'forecast' in url:
                lats = params.get('latitude', '').split(',') if isinstance(params, dict) else ['0']
                # base_time: 1793714400 is 2026-11-03 14:00:00 UTC (08:00 Chicago local time)
                resp.json.return_value = make_mock_weather_response(len(lats), base_time=1793714400)
            else:
                captured_calls.append(('tomtom', url, params))
                # TomTom interprets naive departAt as Chicago local time and returns 08:00:00-06:00
                resp.json.return_value = {'routes': [make_mock_tomtom_route(dep='2026-11-03T08:00:00-06:00')]}
            return resp

        mock_get.side_effect = mocked_get

        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
            'interval_miles': 25,
        }
        res = self.client.post(self.url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # outbound TomTom parameter passed naive departure without converting to UTC
        tomtom_call = [c for c in captured_calls if c[0] == 'tomtom'][0]
        params_list = tomtom_call[2]
        depart_at_param = dict(params_list).get('departAt')
        self.assertEqual(depart_at_param, '2026-11-03T08:00:00')

        # normalized route departure is Chicago local time with offset -06:00
        route = res.json()['routes'][0]
        self.assertEqual(route['departure'], '2026-11-03T08:00:00-06:00')

        # first checkpoint ETA preserves 08:00 Chicago local time
        cp0 = route['checkpoints'][0]
        self.assertEqual(cp0['mile_marker'], 0.0)
        self.assertEqual(cp0['eta'], '2026-11-03T08:00:00-06:00')

        # eta_unix represents 2026-11-03 14:00:00 UTC (1793714400)
        expected_unix = 1793714400  # 2026-11-03 14:00:00 UTC = 08:00:00-06:00
        self.assertEqual(cp0['eta_unix'], expected_unix)

        # Open-Meteo matched exact forecast hour without offset distortion
        weather = cp0['weather']
        self.assertEqual(weather['status'], 'available')
        self.assertEqual(weather['forecast_time_unix'], expected_unix)
        self.assertEqual(weather['forecast_match_offset_seconds'], 0)


@override_settings(TOMTOM_API_KEY='')
class MissingApiKeyTests(SimpleTestCase):
    def test_missing_api_key_returns_configuration_error(self):
        url = reverse('calculate_routes')
        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
        }
        res = self.client.post(url, payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(res.json()['error']['code'], 'CONFIGURATION_ERROR')


class OpenMeteoWeatherTests(SimpleTestCase):
    """Open-Meteo fetching, parsing and hour matching."""

    @patch('requests.get')
    def test_request_construction_and_params(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = make_mock_weather_response(1)
        mock_get.return_value = mock_resp

        coords = [(41.8781, -87.6298)]
        res = fetch_open_meteo_batch(coords, 'https://api.open-meteo.com/v1/forecast')

        self.assertEqual(len(res), 1)
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], 'https://api.open-meteo.com/v1/forecast')
        params = kwargs['params']
        self.assertEqual(params['latitude'], '41.8781')
        self.assertEqual(params['longitude'], '-87.6298')
        self.assertEqual(params['hourly'], 'wind_speed_10m,rain,snowfall')
        self.assertEqual(params['wind_speed_unit'], 'mph')
        self.assertEqual(params['precipitation_unit'], 'inch')
        self.assertEqual(params['timeformat'], 'unixtime')
        self.assertEqual(params['forecast_days'], 7)

    @patch('routes.services.fetch_open_meteo_batch')
    def test_batch_limit_chunking_max_50(self, mock_batch):
        # Return mock results for each batch call
        mock_batch.side_effect = lambda batch, url: [make_mock_weather_response(1) for _ in batch]

        # Construct a route with 120 unique checkpoints
        checkpoints = [
            {
                'id': i,
                'latitude': 40.0 + (i * 0.05),
                'longitude': -90.0 - (i * 0.05),
                'mile_marker': i * 10.0,
                'segment_miles': 10.0,
                'eta': '2026-11-03T10:00:00+00:00',
                'eta_unix': 1793714400 + i * 3600,
            }
            for i in range(120)
        ]
        dummy_route = [{'checkpoints': checkpoints}]

        attach_weather_to_routes(dummy_route, 'https://api.open-meteo.com/v1/forecast')

        # 120 coordinates should be chunked into batches of: 50, 50, 20
        self.assertEqual(mock_batch.call_count, 3)
        call_batch_sizes = [len(call[0][0]) for call in mock_batch.call_args_list]
        self.assertEqual(call_batch_sizes, [50, 50, 20])

    def test_response_parsing_single_and_multi_location(self):
        single_loc = make_mock_weather_response(1)
        parsed_single = parse_weather_hourly(single_loc)
        self.assertIsNotNone(parsed_single)
        self.assertIn('time', parsed_single)
        self.assertIn('wind_speed_10m', parsed_single)
        self.assertEqual(len(parsed_single['time']), 168)

        multi_loc = make_mock_weather_response(2)
        parsed_0 = parse_weather_hourly(multi_loc[0])
        parsed_1 = parse_weather_hourly(multi_loc[1])
        self.assertIsNotNone(parsed_0)
        self.assertIsNotNone(parsed_1)

    def test_response_parsing_invalid_structures(self):
        # Missing hourly
        self.assertIsNone(parse_weather_hourly({'latitude': 40.0}))
        # hourly is not a dict
        self.assertIsNone(parse_weather_hourly({'hourly': 'invalid'}))
        # Missing variable
        self.assertIsNone(parse_weather_hourly({
            'hourly': {
                'time': [1000],
                'wind_speed_10m': [15.0],
                'rain': [0.1],
                # 'snowfall' missing
            }
        }))
        # Array length mismatch
        self.assertIsNone(parse_weather_hourly({
            'hourly': {
                'time': [1000, 2000],
                'wind_speed_10m': [15.0],
                'rain': [0.1, 0.2],
                'snowfall': [0.0, 0.0],
            }
        }))
        # Non-monotonic timestamps
        self.assertIsNone(parse_weather_hourly({
            'hourly': {
                'time': [2000, 1000],
                'wind_speed_10m': [15.0, 16.0],
                'rain': [0.1, 0.2],
                'snowfall': [0.0, 0.0],
            }
        }))

    def test_eta_matching_exact(self):
        hourly_data = {
            'time': [1000, 4600, 8200],  # gap of 3600
            'wind_speed_10m': [10.0, 20.0, 30.0],
            'rain': [0.0, 0.2, 0.5],
            'snowfall': [0.0, 0.0, 1.0],
        }
        res = match_checkpoint_weather(4600, hourly_data)
        self.assertEqual(res['status'], 'available')
        self.assertEqual(res['forecast_time_unix'], 4600)
        self.assertEqual(res['forecast_match_offset_seconds'], 0)
        self.assertEqual(res['wind_speed_mph'], 20.0)
        self.assertEqual(res['rain_in'], 0.2)
        self.assertEqual(res['snowfall_in'], 0.0)

    def test_eta_matching_before_and_after_hour(self):
        # 14:00 is timestamp 3600, 15:00 is timestamp 7200
        hourly_data = {
            'time': [3600, 7200],
            'wind_speed_10m': [10.0, 20.0],
            'rain': [0.0, 0.2],
            'snowfall': [0.0, 0.0],
        }
        # 14:20 is 3600 + 1200 = 4800 (closer to 14:00 by 1200s vs 2400s)
        res_before = match_checkpoint_weather(4800, hourly_data)
        self.assertEqual(res_before['status'], 'available')
        self.assertEqual(res_before['forecast_time_unix'], 3600)
        self.assertEqual(res_before['forecast_match_offset_seconds'], -1200)

        # 14:40 is 3600 + 2400 = 6000 (closer to 15:00 by 1200s vs 2400s)
        res_after = match_checkpoint_weather(6000, hourly_data)
        self.assertEqual(res_after['status'], 'available')
        self.assertEqual(res_after['forecast_time_unix'], 7200)
        self.assertEqual(res_after['forecast_match_offset_seconds'], 1200)

    def test_eta_matching_exact_30_min_boundary_tie_breaks_upward(self):
        # 14:00 is 3600, 15:00 is 7200. Exactly halfway is 5400 (14:30)
        hourly_data = {
            'time': [3600, 7200],
            'wind_speed_10m': [12.0, 18.0],
            'rain': [0.0, 0.1],
            'snowfall': [0.0, 0.0],
        }
        res = match_checkpoint_weather(5400, hourly_data)
        self.assertEqual(res['status'], 'available')
        # On tie, choose later hour (7200 / 15:00)
        self.assertEqual(res['forecast_time_unix'], 7200)
        self.assertEqual(res['forecast_match_offset_seconds'], 1800)
        self.assertEqual(res['wind_speed_mph'], 18.0)

    def test_eta_matching_out_of_range_returns_unavailable(self):
        # Times: 3600 and 10800 (gap of 2 hours, intermediate eta 7200 is 3600s away)
        hourly_data = {
            'time': [3600, 10800],
            'wind_speed_10m': [10.0, 20.0],
            'rain': [0.0, 0.0],
            'snowfall': [0.0, 0.0],
        }
        # eta 7200 is 3600 seconds away from nearest -> > 1800 seconds
        res = match_checkpoint_weather(7200, hourly_data)
        self.assertEqual(res['status'], 'unavailable')
        self.assertIsNone(res['wind_speed_mph'])
        self.assertIn('reason', res)

    def test_eta_matching_null_fields_returns_partial(self):
        hourly_data = {
            'time': [3600],
            'wind_speed_10m': [None],
            'rain': [0.05],
            'snowfall': [None],
        }
        res = match_checkpoint_weather(3600, hourly_data)
        self.assertEqual(res['status'], 'partial')
        self.assertIsNone(res['wind_speed_mph'])
        self.assertEqual(res['rain_in'], 0.05)
        self.assertIsNone(res['snowfall_in'])
        self.assertEqual(set(res['missing_fields']), {'wind_speed_mph', 'snowfall_in'})

    @patch('routes.services.fetch_open_meteo_batch')
    def test_coordinate_deduplication_across_routes(self, mock_batch):
        mock_batch.side_effect = lambda batch, url: [make_mock_weather_response(1) for _ in batch]

        # Two routes that share identical coordinates at checkpoints
        shared_coord = {'latitude': 41.88323, 'longitude': -87.63240}
        unique_coord_1 = {'latitude': 41.50000, 'longitude': -90.00000}
        unique_coord_2 = {'latitude': 42.00000, 'longitude': -91.00000}

        route_1 = {
            'checkpoints': [
                {'id': 1, **shared_coord, 'eta_unix': 1793714400},
                {'id': 2, **unique_coord_1, 'eta_unix': 1793718000},
            ]
        }
        route_2 = {
            'checkpoints': [
                {'id': 1, **shared_coord, 'eta_unix': 1793714400},
                {'id': 2, **unique_coord_2, 'eta_unix': 1793718000},
            ]
        }

        attach_weather_to_routes([route_1, route_2], 'https://api.open-meteo.com/v1/forecast')

        # Total checkpoints = 4, but unique coordinates = 3
        self.assertEqual(mock_batch.call_count, 1)
        batch_passed = mock_batch.call_args[0][0]
        self.assertEqual(len(batch_passed), 3)

        # Weather attached to all checkpoints
        self.assertIn('weather', route_1['checkpoints'][0])
        self.assertIn('weather', route_1['checkpoints'][1])
        self.assertIn('weather', route_2['checkpoints'][0])
        self.assertIn('weather', route_2['checkpoints'][1])

    @patch('requests.get')
    def test_provider_timeout_raises_504(self, mock_get):
        mock_get.side_effect = requests.Timeout('Connection timed out')
        with self.assertRaises(RouteServiceError) as ctx:
            fetch_open_meteo_batch([(40.0, -90.0)], 'https://api.open-meteo.com/v1/forecast', max_retries=0)
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertEqual(ctx.exception.code, 'PROVIDER_TIMEOUT')

    @patch('requests.get')
    def test_provider_server_error_raises_502(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_get.return_value = mock_resp
        with self.assertRaises(RouteServiceError) as ctx:
            fetch_open_meteo_batch([(40.0, -90.0)], 'https://api.open-meteo.com/v1/forecast', max_retries=0)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.code, 'PROVIDER_ERROR')

    @patch('requests.get')
    def test_provider_bounded_retries_on_transient_failure(self, mock_get):
        fail_resp = MagicMock()
        fail_resp.status_code = 500

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = make_mock_weather_response(1)

        # Fail first, succeed second
        mock_get.side_effect = [fail_resp, success_resp]

        res = fetch_open_meteo_batch([(40.0, -90.0)], 'https://api.open-meteo.com/v1/forecast', max_retries=1)
        self.assertEqual(len(res), 1)
        self.assertEqual(mock_get.call_count, 2)

    def test_empty_coordinates_returns_empty_list(self):
        res = fetch_open_meteo_batch([], 'https://api.open-meteo.com/v1/forecast')
        self.assertEqual(res, [])

    @patch('requests.get')
    def test_provider_location_count_mismatch_raises_502(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Returns 1 location when 2 requested
        mock_resp.json.return_value = make_mock_weather_response(1)
        mock_get.return_value = mock_resp

        coords = [(40.0, -90.0), (41.0, -91.0)]
        with self.assertRaises(RouteServiceError) as ctx:
            fetch_open_meteo_batch(coords, 'https://api.open-meteo.com/v1/forecast', max_retries=0)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.code, 'PROVIDER_ERROR')


class WindRiskClassificationTests(SimpleTestCase):
    """Wind thresholds and load rules."""

    def test_wind_base_thresholds(self):
        # Below 25 mph: Low
        self.assertEqual(classify_wind_risk(0.0), RISK_LOW)
        self.assertEqual(classify_wind_risk(24.9), RISK_LOW)

        # 25 to less than 35 mph: Moderate
        self.assertEqual(classify_wind_risk(25.0), RISK_MODERATE)
        self.assertEqual(classify_wind_risk(25.1), RISK_MODERATE)
        self.assertEqual(classify_wind_risk(34.9), RISK_MODERATE)

        # 35 to less than 45 mph: High
        self.assertEqual(classify_wind_risk(35.0), RISK_HIGH)
        self.assertEqual(classify_wind_risk(35.1), RISK_HIGH)
        self.assertEqual(classify_wind_risk(44.9), RISK_HIGH)

        # 45 to less than 55 mph: Severe
        self.assertEqual(classify_wind_risk(45.0), RISK_SEVERE)
        self.assertEqual(classify_wind_risk(45.1), RISK_SEVERE)
        self.assertEqual(classify_wind_risk(54.9), RISK_SEVERE)

        # 55 mph or above: No Travel
        self.assertEqual(classify_wind_risk(55.0), RISK_NO_TRAVEL)
        self.assertEqual(classify_wind_risk(55.1), RISK_NO_TRAVEL)
        self.assertEqual(classify_wind_risk(75.0), RISK_NO_TRAVEL)

    def test_cargo_override_at_45_to_55_mph(self):
        # 30,000 lb exactly -> Severe (no override)
        self.assertEqual(classify_wind_risk(50.0, load_weight_lb=30000), RISK_SEVERE)
        self.assertEqual(classify_wind_risk(45.0, load_weight_lb=30000), RISK_SEVERE)
        self.assertEqual(classify_wind_risk(54.9, load_weight_lb=30000), RISK_SEVERE)

        # Just above 30,000 lb -> No Travel (overridden)
        self.assertEqual(classify_wind_risk(50.0, load_weight_lb=30001), RISK_NO_TRAVEL)
        self.assertEqual(classify_wind_risk(45.0, load_weight_lb=30001), RISK_NO_TRAVEL)
        self.assertEqual(classify_wind_risk(54.9, load_weight_lb=30001), RISK_NO_TRAVEL)

        # Light cargo below 30,000 lb remains Severe
        self.assertEqual(classify_wind_risk(50.0, load_weight_lb=25000), RISK_SEVERE)

    def test_cargo_override_at_35_to_45_mph(self):
        # 40,000 lb exactly -> High (no override)
        self.assertEqual(classify_wind_risk(40.0, load_weight_lb=40000), RISK_HIGH)
        self.assertEqual(classify_wind_risk(35.0, load_weight_lb=40000), RISK_HIGH)
        self.assertEqual(classify_wind_risk(44.9, load_weight_lb=40000), RISK_HIGH)

        # Just above 40,000 lb -> Severe (overridden)
        self.assertEqual(classify_wind_risk(40.0, load_weight_lb=40001), RISK_SEVERE)
        self.assertEqual(classify_wind_risk(35.0, load_weight_lb=40001), RISK_SEVERE)
        self.assertEqual(classify_wind_risk(44.9, load_weight_lb=40001), RISK_SEVERE)

        # Light cargo below 40,000 lb remains High
        self.assertEqual(classify_wind_risk(40.0, load_weight_lb=38000), RISK_HIGH)

    def test_wind_at_least_55_mph_regardless_of_cargo(self):
        self.assertEqual(classify_wind_risk(55.0, load_weight_lb=0), RISK_NO_TRAVEL)
        self.assertEqual(classify_wind_risk(55.0, load_weight_lb=10000), RISK_NO_TRAVEL)
        self.assertEqual(classify_wind_risk(55.0, load_weight_lb=45000), RISK_NO_TRAVEL)
        self.assertEqual(classify_wind_risk(65.0, load_weight_lb=20000), RISK_NO_TRAVEL)


class RainRiskClassificationTests(SimpleTestCase):
    """Rain thresholds."""

    def test_rain_boundaries(self):
        # Below 0.10: Low
        self.assertEqual(classify_rain_risk(0.0), RISK_LOW)
        self.assertEqual(classify_rain_risk(0.09), RISK_LOW)

        # 0.10 through 0.25 inclusive: Moderate
        self.assertEqual(classify_rain_risk(0.10), RISK_MODERATE)
        self.assertEqual(classify_rain_risk(0.11), RISK_MODERATE)
        self.assertEqual(classify_rain_risk(0.24), RISK_MODERATE)
        self.assertEqual(classify_rain_risk(0.25), RISK_MODERATE)

        # Above 0.25 through 0.50 inclusive: High
        self.assertEqual(classify_rain_risk(0.251), RISK_HIGH)
        self.assertEqual(classify_rain_risk(0.26), RISK_HIGH)
        self.assertEqual(classify_rain_risk(0.49), RISK_HIGH)
        self.assertEqual(classify_rain_risk(0.50), RISK_HIGH)

        # Above 0.50 through 1.00 inclusive: Severe
        self.assertEqual(classify_rain_risk(0.501), RISK_SEVERE)
        self.assertEqual(classify_rain_risk(0.51), RISK_SEVERE)
        self.assertEqual(classify_rain_risk(0.99), RISK_SEVERE)
        self.assertEqual(classify_rain_risk(1.00), RISK_SEVERE)

        # Above 1.00: No Travel
        self.assertEqual(classify_rain_risk(1.001), RISK_NO_TRAVEL)
        self.assertEqual(classify_rain_risk(1.01), RISK_NO_TRAVEL)
        self.assertEqual(classify_rain_risk(2.50), RISK_NO_TRAVEL)


class SnowRiskClassificationTests(SimpleTestCase):
    """Snowfall thresholds."""

    def test_snow_boundaries(self):
        # Below 0.5: Low
        self.assertEqual(classify_snow_risk(0.0), RISK_LOW)
        self.assertEqual(classify_snow_risk(0.49), RISK_LOW)

        # 0.5 through 1.0 inclusive: Moderate
        self.assertEqual(classify_snow_risk(0.50), RISK_MODERATE)
        self.assertEqual(classify_snow_risk(0.51), RISK_MODERATE)
        self.assertEqual(classify_snow_risk(0.99), RISK_MODERATE)
        self.assertEqual(classify_snow_risk(1.00), RISK_MODERATE)

        # Above 1.0 through 2.0 inclusive: High
        self.assertEqual(classify_snow_risk(1.01), RISK_HIGH)
        self.assertEqual(classify_snow_risk(1.50), RISK_HIGH)
        self.assertEqual(classify_snow_risk(1.99), RISK_HIGH)
        self.assertEqual(classify_snow_risk(2.00), RISK_HIGH)

        # Above 2.0 through 3.0 inclusive: Severe
        self.assertEqual(classify_snow_risk(2.01), RISK_SEVERE)
        self.assertEqual(classify_snow_risk(2.50), RISK_SEVERE)
        self.assertEqual(classify_snow_risk(2.99), RISK_SEVERE)
        self.assertEqual(classify_snow_risk(3.00), RISK_SEVERE)

        # Above 3.0: No Travel
        self.assertEqual(classify_snow_risk(3.01), RISK_NO_TRAVEL)
        self.assertEqual(classify_snow_risk(4.00), RISK_NO_TRAVEL)


class CheckpointRiskAssessmentTests(SimpleTestCase):
    """Overall checkpoint risk and handling of missing weather."""

    def test_overall_risk_takes_worst_factor(self):
        # Wind Low, Rain Moderate, Snow Low -> Moderate
        w1 = {'status': 'available', 'wind_speed_mph': 10.0, 'rain_in': 0.15, 'snowfall_in': 0.0}
        r1 = assess_checkpoint_risk(w1)
        self.assertEqual(r1['assessment_status'], 'assessed')
        self.assertEqual(r1['wind_level'], RISK_LOW)
        self.assertEqual(r1['rain_level'], RISK_MODERATE)
        self.assertEqual(r1['snow_level'], RISK_LOW)
        self.assertEqual(r1['overall_level'], RISK_MODERATE)

        # Wind Low, Rain Low, Snow Severe -> Severe
        w2 = {'status': 'available', 'wind_speed_mph': 15.0, 'rain_in': 0.0, 'snowfall_in': 2.5}
        r2 = assess_checkpoint_risk(w2)
        self.assertEqual(r2['overall_level'], RISK_SEVERE)

        # Wind High, Rain Low, Snow Moderate -> High
        w3 = {'status': 'available', 'wind_speed_mph': 38.0, 'rain_in': 0.05, 'snowfall_in': 0.6}
        r3 = assess_checkpoint_risk(w3)
        self.assertEqual(r3['overall_level'], RISK_HIGH)

        # Wind Moderate, Rain Low, Snow No Travel -> No Travel
        w4 = {'status': 'available', 'wind_speed_mph': 28.0, 'rain_in': 0.05, 'snowfall_in': 3.5}
        r4 = assess_checkpoint_risk(w4)
        self.assertEqual(r4['overall_level'], RISK_NO_TRAVEL)

    def test_cargo_override_applied_to_checkpoint_wind_before_overall(self):
        # Wind 40 mph normally High, with 45,000 lb overrides to Severe
        w = {'status': 'available', 'wind_speed_mph': 40.0, 'rain_in': 0.0, 'snowfall_in': 0.0}
        r_light = assess_checkpoint_risk(w, load_weight_lb=35000)
        self.assertEqual(r_light['wind_level'], RISK_HIGH)
        self.assertEqual(r_light['overall_level'], RISK_HIGH)

        r_heavy = assess_checkpoint_risk(w, load_weight_lb=45000)
        self.assertEqual(r_heavy['wind_level'], RISK_SEVERE)
        self.assertEqual(r_heavy['overall_level'], RISK_SEVERE)

    def test_missing_and_partial_weather_produces_unassessed_status(self):
        # Missing weather object
        r_none = assess_checkpoint_risk(None)
        self.assertEqual(r_none['assessment_status'], 'unavailable')
        self.assertIsNone(r_none['overall_level'])

        # Provider unavailable
        w_unavail = {'status': 'unavailable'}
        r_unavail = assess_checkpoint_risk(w_unavail)
        self.assertEqual(r_unavail['assessment_status'], 'unavailable')
        self.assertIsNone(r_unavail['overall_level'])

        # Partial weather with missing snowfall
        w_part = {'status': 'partial', 'wind_speed_mph': 15.0, 'rain_in': 0.0, 'snowfall_in': None}
        r_part = assess_checkpoint_risk(w_part)
        self.assertEqual(r_part['assessment_status'], 'partial')
        self.assertEqual(r_part['wind_level'], RISK_LOW)
        self.assertIsNone(r_part['snow_level'])
        # Missing data must not look safe
        self.assertIsNone(r_part['overall_level'])


class RouteRiskAggregationTests(SimpleTestCase):
    """Miles per risk level and the distance-weighted average."""

    def test_unequal_segments_and_partial_segment(self):
        route = {
            'distance_miles': 104.4,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 25.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 25.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 4, 'segment_miles': 25.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 5, 'segment_miles': 25.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_HIGH}},
                {'id': 6, 'segment_miles': 4.4, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertTrue(res['is_fully_assessed'])
        self.assertEqual(res['low_miles'], 29.4)  # 25.0 + 4.4
        self.assertEqual(res['moderate_miles'], 50.0)  # 25.0 + 25.0
        self.assertEqual(res['high_miles'], 25.0)
        self.assertEqual(res['severe_miles'], 0.0)
        self.assertEqual(res['no_travel_miles'], 0.0)
        self.assertEqual(res['unassessed_miles'], 0.0)

        # Sum of categories matches total route distance
        total_sum = round(res['low_miles'] + res['moderate_miles'] + res['high_miles'] + res['severe_miles'] + res['no_travel_miles'] + res['unassessed_miles'], 1)
        self.assertEqual(total_sum, 104.4)

        # Average risk: (29.4*1 + 50.0*2 + 25.0*3) / 104.4 = (29.4 + 100.0 + 75.0) / 104.4 = 204.4 / 104.4 = 1.96
        self.assertEqual(res['average_risk'], 1.96)

    def test_route_with_no_travel_miles_has_null_average_risk(self):
        route = {
            'distance_miles': 100.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 80.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 20.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_NO_TRAVEL}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertEqual(res['no_travel_miles'], 20.0)
        self.assertIsNone(res['average_risk'])

    def test_route_with_incomplete_weather_reports_unassessed_miles_and_null_average(self):
        route = {
            'distance_miles': 100.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 60.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 40.0, 'risk': {'assessment_status': 'partial', 'overall_level': None}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertFalse(res['is_fully_assessed'])
        self.assertEqual(res['low_miles'], 60.0)
        self.assertEqual(res['unassessed_miles'], 40.0)
        self.assertIsNone(res['average_risk'])
        self.assertEqual(round(res['low_miles'] + res['unassessed_miles'], 1), 100.0)

    def test_independent_calculation_example_1_mixed_low_mod_high(self):
        # 145.0 miles: 45.0 Low, 50.0 Moderate, 50.0 High
        # Expected weighted sum = 45*1 + 50*2 + 50*3 = 295.0
        # Expected average risk = 295.0 / 145.0 = 2.03
        route = {
            'distance_miles': 145.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 45.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 50.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 4, 'segment_miles': 50.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_HIGH}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertTrue(res['is_fully_assessed'])
        self.assertEqual(res['low_miles'], 45.0)
        self.assertEqual(res['moderate_miles'], 50.0)
        self.assertEqual(res['high_miles'], 50.0)
        self.assertEqual(res['severe_miles'], 0.0)
        self.assertEqual(res['no_travel_miles'], 0.0)
        self.assertEqual(res['unassessed_miles'], 0.0)
        self.assertEqual(res['average_risk'], 2.03)
        self.assertEqual(round(res['low_miles'] + res['moderate_miles'] + res['high_miles'], 1), 145.0)

    def test_independent_calculation_example_2_with_severe_segment(self):
        # 220.0 miles: 50.0 Low, 70.0 Moderate, 60.0 High, 40.0 Severe
        # Expected weighted sum = 50*1 + 70*2 + 60*3 + 40*4 = 50 + 140 + 180 + 160 = 530.0
        # Expected average risk = 530.0 / 220.0 = 2.41
        route = {
            'distance_miles': 220.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 50.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 70.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 4, 'segment_miles': 60.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_HIGH}},
                {'id': 5, 'segment_miles': 40.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_SEVERE}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertTrue(res['is_fully_assessed'])
        self.assertEqual(res['low_miles'], 50.0)
        self.assertEqual(res['moderate_miles'], 70.0)
        self.assertEqual(res['high_miles'], 60.0)
        self.assertEqual(res['severe_miles'], 40.0)
        self.assertEqual(res['average_risk'], 2.41)
        self.assertEqual(round(res['low_miles'] + res['moderate_miles'] + res['high_miles'] + res['severe_miles'], 1), 220.0)

    def test_independent_calculation_example_3_with_no_travel_miles(self):
        # 180.0 miles: 100.0 Low, 60.0 Severe, 20.0 No Travel
        # Must have null average risk
        route = {
            'distance_miles': 180.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 100.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 60.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_SEVERE}},
                {'id': 4, 'segment_miles': 20.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_NO_TRAVEL}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertTrue(res['is_fully_assessed'])
        self.assertEqual(res['low_miles'], 100.0)
        self.assertEqual(res['severe_miles'], 60.0)
        self.assertEqual(res['no_travel_miles'], 20.0)
        self.assertIsNone(res['average_risk'])
        self.assertEqual(round(res['low_miles'] + res['severe_miles'] + res['no_travel_miles'], 1), 180.0)

    def test_zero_distance_route_safety(self):
        route = {
            'distance_miles': 0.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertTrue(res['is_fully_assessed'])
        self.assertEqual(res['low_miles'], 0.0)
        self.assertIsNone(res['average_risk'])

    def test_very_small_route_segments(self):
        # 0.3 miles: 0.1 Low, 0.1 Moderate, 0.1 High
        route = {
            'distance_miles': 0.3,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 0.1, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 0.1, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 4, 'segment_miles': 0.1, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_HIGH}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertEqual(res['low_miles'], 0.1)
        self.assertEqual(res['moderate_miles'], 0.1)
        self.assertEqual(res['high_miles'], 0.1)
        self.assertEqual(res['average_risk'], 2.0)  # (0.1*1 + 0.1*2 + 0.1*3) / 0.3 = 0.6 / 0.3 = 2.0
        self.assertEqual(round(res['low_miles'] + res['moderate_miles'] + res['high_miles'], 1), 0.3)

    def test_all_unavailable_weather_route(self):
        route = {
            'distance_miles': 100.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'unavailable'}},
                {'id': 2, 'segment_miles': 50.0, 'risk': {'assessment_status': 'unavailable'}},
                {'id': 3, 'segment_miles': 50.0, 'risk': {'assessment_status': 'unavailable'}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertFalse(res['is_fully_assessed'])
        self.assertEqual(res['unassessed_miles'], 100.0)
        self.assertEqual(res['low_miles'], 0.0)
        self.assertIsNone(res['average_risk'])

    def test_six_bucket_rounding_stays_within_0_05_miles(self):
        # Six buckets of 10.04 miles each (60.24 total)
        # With 1 decimal: each is 10.0 -> sum 60.0 (error 0.24 > 0.05!)
        # With 2 decimals: each is 10.04 -> sum 60.24 (error 0.00 <= 0.05!)
        route = {
            'distance_miles': 60.24,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 10.04, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 10.04, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 4, 'segment_miles': 10.04, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_HIGH}},
                {'id': 5, 'segment_miles': 10.04, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_SEVERE}},
                {'id': 6, 'segment_miles': 10.04, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_NO_TRAVEL}},
                {'id': 7, 'segment_miles': 10.04, 'risk': {'assessment_status': 'partial', 'overall_level': None}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertEqual(res['low_miles'], 10.04)
        self.assertEqual(res['moderate_miles'], 10.04)
        self.assertEqual(res['high_miles'], 10.04)
        self.assertEqual(res['severe_miles'], 10.04)
        self.assertEqual(res['no_travel_miles'], 10.04)
        self.assertEqual(res['unassessed_miles'], 10.04)

        bucket_sum = round(
            res['low_miles'] + res['moderate_miles'] + res['high_miles'] +
            res['severe_miles'] + res['no_travel_miles'] + res['unassessed_miles'],
            2
        )
        self.assertEqual(bucket_sum, 60.24)
        # Rounded buckets should stay within 0.05 miles of the total
        self.assertLessEqual(abs(bucket_sum - 60.24), 0.05)

    def test_uneven_rounding_in_both_directions_stays_within_0_05_miles(self):
        # 5 categories with 3-decimal segment values rounding in opposite directions
        route = {
            'distance_miles': 60.024,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 20.004, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 15.006, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 4, 'segment_miles': 12.004, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_HIGH}},
                {'id': 5, 'segment_miles': 8.006, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_SEVERE}},
                {'id': 6, 'segment_miles': 5.004, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_NO_TRAVEL}},
            ]
        }
        res = aggregate_route_risk(route)
        bucket_sum = round(
            res['low_miles'] + res['moderate_miles'] + res['high_miles'] +
            res['severe_miles'] + res['no_travel_miles'] + res['unassessed_miles'],
            2
        )
        self.assertLessEqual(abs(bucket_sum - 60.02), 0.05)

    def test_partially_assessed_route_precision_within_0_05_miles(self):
        # Partially assessed route with uneven fractional segments
        route = {
            'distance_miles': 100.0,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 33.333, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 33.333, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_MODERATE}},
                {'id': 4, 'segment_miles': 33.333, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_HIGH}},
                {'id': 5, 'segment_miles': 0.001, 'risk': {'assessment_status': 'unavailable'}},
            ]
        }
        res = aggregate_route_risk(route)
        bucket_sum = round(
            res['low_miles'] + res['moderate_miles'] + res['high_miles'] +
            res['severe_miles'] + res['no_travel_miles'] + res['unassessed_miles'],
            2
        )
        self.assertLessEqual(abs(bucket_sum - 100.0), 0.05)

    def test_tiny_no_travel_mileage_invalidates_average_risk_and_eligibility(self):
        # 99.996 mi Low, 0.004 mi No Travel -> 0.004 rounds to 0.00 for display,
        # but raw accumulator must invalidate average_risk and disqualify route from recommendation
        route = {
            'id': 1,
            'distance_miles': 100.0,
            'duration_seconds': 3600,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 99.996, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 0.004, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_NO_TRAVEL}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertEqual(res['low_miles'], 100.0)
        self.assertEqual(res['no_travel_miles'], 0.0)  # Rounded to 2 decimals for display
        self.assertTrue(res['is_fully_assessed'])
        self.assertTrue(res['has_no_travel'])  # Detected by raw accumulator
        self.assertIsNone(res['average_risk'])  # Invalidate average risk

        route['risk_summary'] = res
        rec = evaluate_route_recommendations([route])
        self.assertIsNone(rec['recommended_route_id'])
        self.assertEqual(rec['status'], 'no_safe_route')
        self.assertTrue(rec['all_routes_unsafe'])
        self.assertTrue(rec['is_weather_complete'])
        self.assertFalse(route['is_recommended'])

    def test_tiny_unassessed_mileage_invalidates_average_risk_and_eligibility(self):
        # 99.996 mi Low, 0.004 mi unassessed -> 0.004 rounds to 0.00 for display,
        # but raw accumulator must mark route as not fully assessed and disqualify from recommendation
        route = {
            'id': 1,
            'distance_miles': 100.0,
            'duration_seconds': 3600,
            'checkpoints': [
                {'id': 1, 'segment_miles': 0.0, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 2, 'segment_miles': 99.996, 'risk': {'assessment_status': 'assessed', 'overall_level': RISK_LOW}},
                {'id': 3, 'segment_miles': 0.004, 'risk': {'assessment_status': 'unavailable'}},
            ]
        }
        res = aggregate_route_risk(route)
        self.assertEqual(res['low_miles'], 100.0)
        self.assertEqual(res['unassessed_miles'], 0.0)  # Rounded to 2 decimals for display
        self.assertFalse(res['is_fully_assessed'])  # Detected by raw accumulator
        self.assertFalse(res['has_no_travel'])
        self.assertIsNone(res['average_risk'])

        route['risk_summary'] = res
        rec = evaluate_route_recommendations([route])
        self.assertIsNone(rec['recommended_route_id'])
        self.assertEqual(rec['status'], 'incomplete_weather')
        self.assertFalse(rec['all_routes_unsafe'])
        self.assertFalse(rec['is_weather_complete'])
        self.assertFalse(route['is_recommended'])


class RouteRecommendationRankingTests(SimpleTestCase):
    """Ranking order, tie-breakers and the no-recommendation cases."""

    def test_priority_1_fewest_severe_miles(self):
        # Route 1 has 5 Severe miles, Route 2 has 0 Severe miles (even with more High miles)
        r1 = {
            'id': 1,
            'duration_seconds': 3600,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 5.0, 'high_miles': 0.0, 'average_risk': 1.8},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 4000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 15.0, 'average_risk': 2.1},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertEqual(rec['recommended_route_id'], 2)
        self.assertEqual(rec['message'], "Route 2 is recommended because it has the fewest severe-risk miles.")
        self.assertTrue(r2['is_recommended'])
        self.assertFalse(r1['is_recommended'])

    def test_priority_2_fewest_high_miles_on_severe_tie(self):
        r1 = {
            'id': 1,
            'duration_seconds': 3600,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 10.0, 'average_risk': 1.5},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 3600,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 2.0, 'average_risk': 1.6},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertEqual(rec['recommended_route_id'], 2)
        self.assertEqual(rec['message'], "Route 2 is recommended because it has the fewest high-risk miles.")

    def test_priority_3_lowest_average_risk_on_severe_and_high_tie(self):
        r1 = {
            'id': 1,
            'duration_seconds': 4000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.8},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 4500,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.2},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertEqual(rec['recommended_route_id'], 2)
        self.assertEqual(rec['message'], "Route 2 is recommended because it has the lowest average weather risk.")

    def test_priority_4_shortest_duration_on_risk_tie(self):
        r1 = {
            'id': 1,
            'duration_seconds': 5000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.5},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 4200,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.5},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertEqual(rec['recommended_route_id'], 2)
        self.assertEqual(rec['message'], "Both routes have equal weather-risk assessments. Route 2 is recommended because it has the shortest estimated travel time.")

    def test_priority_5_provider_order_breaks_remaining_ties(self):
        r1 = {
            'id': 1,
            'duration_seconds': 4000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.5},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 4000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.5},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertEqual(rec['recommended_route_id'], 1)
        self.assertEqual(rec['message'], "Both routes have equal weather-risk assessments and travel times. Route 1 is recommended based on provider route ordering.")

    def test_three_routes_equal_risk_tie_recommends_shortest_duration_with_explanation(self):
        r1 = {
            'id': 1,
            'duration_seconds': 51056,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.0},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 57700,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.0},
        }
        r3 = {
            'id': 3,
            'duration_seconds': 69357,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.0},
        }
        rec = evaluate_route_recommendations([r1, r2, r3])
        self.assertEqual(rec['recommended_route_id'], 1)
        self.assertEqual(rec['status'], 'recommended')
        self.assertTrue(rec['is_weather_complete'])
        self.assertFalse(rec['all_routes_unsafe'])
        self.assertTrue(r1['is_recommended'])
        self.assertFalse(r2['is_recommended'])
        self.assertFalse(r3['is_recommended'])
        self.assertEqual(
            rec['message'],
            "All three routes have equal weather-risk assessments. Route 1 is recommended because it has the shortest estimated travel time."
        )

    def test_safe_route_with_incomplete_alternative_is_recommended_with_notice(self):
        r1 = {
            'id': 1,
            'duration_seconds': 4000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': 1.2},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 3500,
            'risk_summary': {'is_fully_assessed': False, 'unassessed_miles': 50.0, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertEqual(rec['recommended_route_id'], 1)
        self.assertEqual(rec['status'], 'recommended')
        self.assertTrue(r1['is_recommended'])
        self.assertFalse(r2['is_recommended'])
        self.assertFalse(rec['is_weather_complete'])
        self.assertFalse(rec['all_routes_unsafe'])
        self.assertIn("Route 1 is recommended as the only fully assessed safe route available.", rec['message'])
        self.assertIn("Note: Route 2 could not be fully assessed due to missing weather data.", rec['message'])

    def test_multiple_safe_routes_with_incomplete_alternative_recommends_best_safe_route(self):
        # Scenario:
        # Route 1: Fully assessed safe route with 2.0 High miles
        # Route 2: Fully assessed safe route with 10.0 High miles
        # Route 3: Incomplete weather assessment (unassessed miles)
        r1 = {
            'id': 1,
            'duration_seconds': 4000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 2.0, 'average_risk': 1.4},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 4500,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 10.0, 'average_risk': 1.8},
        }
        r3 = {
            'id': 3,
            'duration_seconds': 3500,
            'risk_summary': {'is_fully_assessed': False, 'unassessed_miles': 30.0, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        rec = evaluate_route_recommendations([r1, r2, r3])
        # Route 1 selected using approved ranking priority (fewest High miles: 2.0 vs 10.0)
        self.assertEqual(rec['recommended_route_id'], 1)
        # status must be recommended
        self.assertEqual(rec['status'], 'recommended')
        # is_weather_complete must be false
        self.assertFalse(rec['is_weather_complete'])
        # all_routes_unsafe must be false
        self.assertFalse(rec['all_routes_unsafe'])
        # Route recommendations correctly assigned
        self.assertTrue(r1['is_recommended'])
        self.assertFalse(r2['is_recommended'])
        self.assertFalse(r3['is_recommended'])
        # Message explains why winner was selected and discloses Route 3 incomplete weather
        self.assertEqual(
            rec['message'],
            "Route 1 is recommended because it has the fewest high-risk miles. Note: Route 3 could not be fully assessed due to missing weather data."
        )
        # Should not claim it is the "only fully assessed safe route"
        self.assertNotIn("only fully assessed", rec['message'])

    def test_unsafe_route_plus_incomplete_alternative_returns_incomplete_weather(self):
        r1 = {
            'id': 1,
            'duration_seconds': 3000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 15.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 3500,
            'risk_summary': {'is_fully_assessed': False, 'unassessed_miles': 40.0, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertIsNone(rec['recommended_route_id'])
        self.assertEqual(rec['status'], 'incomplete_weather')
        self.assertEqual(rec['message'], MSG_INCOMPLETE_WEATHER)
        self.assertFalse(rec['all_routes_unsafe'])
        self.assertFalse(rec['is_weather_complete'])
        self.assertFalse(r1['is_recommended'])
        self.assertFalse(r2['is_recommended'])

    def test_mixed_unsafe_and_eligible_safe_alternatives_recommends_best_eligible(self):
        r1 = {
            'id': 1,
            'duration_seconds': 3000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 10.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 4500,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 5.0, 'average_risk': 1.6},
        }
        r3 = {
            'id': 3,
            'duration_seconds': 5000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 15.0, 'average_risk': 2.0},
        }
        rec = evaluate_route_recommendations([r1, r2, r3])
        self.assertEqual(rec['recommended_route_id'], 2)
        self.assertEqual(rec['status'], 'recommended')
        self.assertTrue(r2['is_recommended'])
        self.assertFalse(r1['is_recommended'])
        self.assertFalse(r3['is_recommended'])
        self.assertTrue(rec['is_weather_complete'])
        self.assertFalse(rec['all_routes_unsafe'])
        self.assertEqual(rec['message'], "Route 2 is recommended because it has the fewest high-risk miles.")

    def test_no_travel_route_never_recommended(self):
        # Route 1 has No Travel miles, Route 2 is fully safe
        r1 = {
            'id': 1,
            'duration_seconds': 2000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 5.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 4000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 0.0, 'severe_miles': 1.0, 'high_miles': 5.0, 'average_risk': 1.6},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertEqual(rec['recommended_route_id'], 2)
        self.assertEqual(rec['message'], "Route 2 is recommended as the only fully assessed safe route available.")
        self.assertFalse(r1['is_recommended'])
        self.assertTrue(r2['is_recommended'])

    def test_all_routes_unsafe_returns_exact_message(self):
        r1 = {
            'id': 1,
            'duration_seconds': 3000,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 10.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 3500,
            'risk_summary': {'is_fully_assessed': True, 'no_travel_miles': 5.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertIsNone(rec['recommended_route_id'])
        self.assertEqual(rec['status'], 'no_safe_route')
        self.assertEqual(rec['message'], MSG_NO_SAFE_ROUTE)
        self.assertTrue(rec['all_routes_unsafe'])
        self.assertFalse(r1['is_recommended'])
        self.assertFalse(r2['is_recommended'])

    def test_incomplete_weather_returns_distinct_unassessed_status(self):
        r1 = {
            'id': 1,
            'duration_seconds': 3000,
            'risk_summary': {'is_fully_assessed': False, 'unassessed_miles': 25.0, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        r2 = {
            'id': 2,
            'duration_seconds': 3500,
            'risk_summary': {'is_fully_assessed': False, 'unassessed_miles': 50.0, 'no_travel_miles': 0.0, 'severe_miles': 0.0, 'high_miles': 0.0, 'average_risk': None},
        }
        rec = evaluate_route_recommendations([r1, r2])
        self.assertIsNone(rec['recommended_route_id'])
        self.assertEqual(rec['status'], 'incomplete_weather')
        self.assertEqual(rec['message'], MSG_INCOMPLETE_WEATHER)
        self.assertFalse(rec['all_routes_unsafe'])
        self.assertFalse(rec['is_weather_complete'])


@override_settings(TOMTOM_API_KEY='valid-test-key')
class RiskApiIntegrationTests(SimpleTestCase):
    """Risk and recommendation fields in the API response."""

    @patch('requests.get')
    def test_api_returns_recommendation_and_risk_summary(self, mock_get):
        def mocked_get(url, params=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            if 'geocode' in url:
                resp.json.return_value = {'results': [{'position': {'lat': 41.88323, 'lon': -87.6324}}]}
            elif 'open-meteo' in url or 'forecast' in url:
                lats = params.get('latitude', '').split(',') if isinstance(params, dict) else ['0']
                # base_time: 1793714400 (Chicago 08:00)
                resp.json.return_value = make_mock_weather_response(len(lats), base_time=1793714400)
            else:
                resp.json.return_value = {'routes': [make_mock_tomtom_route(dep='2026-11-03T08:00:00-06:00')]}
            return resp

        mock_get.side_effect = mocked_get

        payload = {
            'origin': 'Chicago, IL',
            'destination': 'Denver, CO',
            'departure': '2026-11-03T08:00:00',
            'load_weight_lb': 38000,
            'interval_miles': 25,
        }
        res = self.client.post(reverse('calculate_routes'), payload, content_type='application/json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        # Root level recommendation object
        self.assertIn('recommendation', data)
        rec = data['recommendation']
        self.assertIn('recommended_route_id', rec)
        self.assertEqual(rec['recommended_route_id'], 1)
        self.assertEqual(rec['status'], 'recommended')
        self.assertIn('message', rec)

        # Route level risk_summary and is_recommended
        route = data['routes'][0]
        self.assertIn('risk_summary', route)
        rs = route['risk_summary']
        self.assertIn('low_miles', rs)
        self.assertIn('moderate_miles', rs)
        self.assertIn('high_miles', rs)
        self.assertIn('severe_miles', rs)
        self.assertIn('no_travel_miles', rs)
        self.assertIn('unassessed_miles', rs)
        self.assertIn('average_risk', rs)
        self.assertIn('is_fully_assessed', rs)
        self.assertIn('has_no_travel', rs)
        self.assertTrue(route['is_recommended'])

        # Checkpoint level risk object
        cp0 = route['checkpoints'][0]
        self.assertIn('risk', cp0)
        cpr = cp0['risk']
        self.assertEqual(cpr['assessment_status'], 'assessed')
        self.assertIn('wind_level', cpr)
        self.assertIn('rain_level', cpr)
        self.assertIn('snow_level', cpr)
        self.assertIn('overall_level', cpr)




def make_wind_route(route_id, duration_s, winds):
    """A route with one 25-mile checkpoint per wind reading and no rain or snow."""
    checkpoints = [
        {
            'id': i,
            'mile_marker': i * 25.0,
            'segment_miles': 25.0,
            'weather': {'status': 'available', 'wind_speed_mph': wind, 'rain_in': 0.0, 'snowfall_in': 0.0},
        }
        for i, wind in enumerate(winds, start=1)
    ]
    return {
        'id': route_id,
        'distance_miles': len(winds) * 25.0,
        'duration_seconds': duration_s,
        'checkpoints': checkpoints,
    }


class DepartureShiftTests(SimpleTestCase):
    """What the forecast slider relies on: a later departure moves every ETA and lookup."""

    def test_later_departure_shifts_every_eta(self):
        geometry = [
            {'latitude': 41.8781, 'longitude': -87.6298},
            {'latitude': 41.5000, 'longitude': -89.0000},
            {'latitude': 41.0000, 'longitude': -91.0000},
        ]
        markers = [
            {'distance_meters': 0, 'travel_time_seconds': 0, 'point_index': 0},
            {'distance_meters': 80467, 'travel_time_seconds': 3600, 'point_index': 1},
            {'distance_meters': 160934, 'travel_time_seconds': 7200, 'point_index': 2},
        ]
        args = (geometry, markers, 160934, 7200)

        morning = generate_checkpoints(*args, '2026-11-04T08:00:00', 25, 100.0)
        evening = generate_checkpoints(*args, '2026-11-04T20:00:00', 25, 100.0)

        self.assertEqual(len(morning), len(evening))
        self.assertGreaterEqual(len(morning), 4)
        for a, b in zip(morning, evening):
            shift = datetime.fromisoformat(b['eta']) - datetime.fromisoformat(a['eta'])
            self.assertEqual(shift, timedelta(hours=12))

    def test_shifted_eta_picks_matching_forecast_hour(self):
        base = int(datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp())
        hourly = parse_weather_hourly({
            'hourly': {
                'time': [base + h * 3600 for h in range(48)],
                'wind_speed_10m': [10.0 + h for h in range(48)],
                'rain': [0.0] * 48,
                'snowfall': [0.0] * 48,
            }
        })

        for hour in (8, 20, 32):
            weather = match_checkpoint_weather(base + hour * 3600, hourly)
            self.assertEqual(weather['status'], 'available')
            self.assertEqual(weather['wind_speed_mph'], 10.0 + hour)

    def test_recommendation_follows_the_weather(self):
        load = 35000

        _, rec = process_route_risks([
            make_wind_route(1, 14 * 3600, [15.0, 15.0, 15.0, 15.0]),
            make_wind_route(2, 16 * 3600, [15.0, 40.0, 15.0, 15.0]),
        ], load)
        self.assertEqual(rec['recommended_route_id'], 1)

        # Twelve hours later the wind has moved onto route 1.
        later_routes, rec = process_route_risks([
            make_wind_route(1, 14 * 3600, [15.0, 40.0, 40.0, 15.0]),
            make_wind_route(2, 16 * 3600, [15.0, 15.0, 15.0, 15.0]),
        ], load)
        self.assertEqual(rec['recommended_route_id'], 2)
        self.assertFalse(later_routes[0]['is_recommended'])
        self.assertTrue(later_routes[1]['is_recommended'])

    def test_segment_miles_add_up_to_route_distance(self):
        geometry = [
            {'latitude': 41.8781 + i * 0.01, 'longitude': -87.6298 + i * 0.01}
            for i in range(100)
        ]
        total_miles = round(100000.0 / 1609.344, 1)

        checkpoints = generate_checkpoints(
            geometry=geometry,
            progress_markers=[],
            total_distance_m=100000.0,
            total_time_s=3600,
            departure_str='2026-11-04T08:00:00',
            interval_miles=25,
            total_miles=total_miles,
        )

        segment_total = sum(cp['segment_miles'] for cp in checkpoints)
        self.assertAlmostEqual(segment_total, total_miles, delta=0.05)
        # The last segment is shorter than the interval, not padded out.
        self.assertNotEqual(segment_total, len(checkpoints) * 25.0)
