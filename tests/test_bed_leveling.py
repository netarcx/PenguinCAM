"""Bed-leveling/spoilboard-surfacing generator and API coverage."""

import math
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bed_leveling import (BedLevelingError, generate_bed_leveling, parse_spec,
                          raster_path)
from frc_cam_gui_app import app
from team_config import TeamConfig


def valid_data(**changes):
    data = {
        'width': 24,
        'height': 18,
        'tool_diameter': 1,
        'stepover_percent': 60,
        'depth': 0.01,
        'feed_rate': 100,
        'plunge_rate': 20,
        'spindle_speed': 18000,
        'safe_z': 0.25,
        'raster_direction': 'long',
    }
    data.update(changes)
    return data


class TestBedLevelingGenerator(unittest.TestCase):
    def test_raster_covers_edges_and_never_exceeds_stepover(self):
        spec = parse_spec(valid_data(width=10, height=4, tool_diameter=1,
                                     stepover_percent=60))
        path = raster_path(spec)
        xs = [p[0] for p in path]
        rows = sorted(set(round(p[1], 8) for p in path))
        self.assertAlmostEqual(min(xs), 0.0)
        self.assertAlmostEqual(max(xs), 10.0)
        self.assertAlmostEqual(rows[0], 0.0)
        self.assertAlmostEqual(rows[-1], 4.0)
        self.assertLessEqual(max(b - a for a, b in zip(rows, rows[1:])), 0.6 + 1e-9)
        # The linking motion is axis-aligned, so it cannot leave an uncut diagonal.
        for first, second in zip(path, path[1:]):
            self.assertTrue(math.isclose(first[0], second[0]) or
                            math.isclose(first[1], second[1]))

    def test_program_has_safe_modal_setup_automatic_spindle_and_shutdown(self):
        result = generate_bed_leveling(parse_spec(valid_data(width=10, height=4)))
        text = result.gcode
        self.assertIn('G20  ; Inches', text)
        self.assertIn('G54  ; Work coordinate system 1', text)
        self.assertNotIn('M0', text)
        self.assertIn('S18000 M3  ; Spindle on', text)
        self.assertLess(text.index('S18000 M3'), text.index('G1 Z-0.0100'))
        self.assertIn('G1 Z-0.0100 F20.0', text)
        self.assertEqual(text.count('M30'), 1)
        self.assertNotIn('M9', text)  # coolant codes are config-only in PenguinCAM
        text.encode('ascii')
        for line in text.splitlines():
            self.assertLessEqual(line.count('('), 1)
            if '(' in line:
                self.assertNotIn('[', line)
                self.assertNotIn(']', line)
        self.assertTrue(result.filename.endswith('.nc'))

    def test_raster_can_run_long_way_or_short_way(self):
        long_path = raster_path(parse_spec(valid_data(width=10, height=4,
                                                       raster_direction='long')))
        short_path = raster_path(parse_spec(valid_data(width=10, height=4,
                                                        raster_direction='short')))
        # The first cutting stroke follows X for long-way and Y for short-way.
        self.assertEqual(long_path[:2], [(0.0, 0.0), (10.0, 0.0)])
        self.assertEqual(short_path[:2], [(0.0, 0.0), (0.0, 4.0)])
        self.assertLess(len(long_path), len(short_path))
        for path in (long_path, short_path):
            self.assertEqual(min(x for x, _ in path), 0.0)
            self.assertEqual(max(x for x, _ in path), 10.0)
            self.assertEqual(min(y for _, y in path), 0.0)
            self.assertEqual(max(y for _, y in path), 4.0)

    def test_rejects_unknown_raster_direction(self):
        with self.assertRaisesRegex(BedLevelingError, 'direction'):
            parse_spec(valid_data(raster_direction='diagonal'))

    def test_dimensions_are_limited_to_machine_travel(self):
        with self.assertRaisesRegex(BedLevelingError, 'machine X travel'):
            parse_spec(valid_data(width=25), machine_width=24, machine_height=24)
        with self.assertRaisesRegex(BedLevelingError, 'machine Y travel'):
            parse_spec(valid_data(height=25), machine_width=24, machine_height=24)
        with self.assertRaisesRegex(BedLevelingError, 'machine Z travel'):
            parse_spec(valid_data(safe_z=3), machine_z=2)
        self.assertEqual(parse_spec(valid_data(safe_z=3), machine_z=8).safe_z, 3)

    def test_rejects_aggressive_or_invalid_inputs(self):
        cases = [
            ({'depth': 0.2}, 'Cut depth'),
            ({'stepover_percent': 95}, 'Stepover'),
            ({'tool_diameter': 20, 'height': 10}, 'Cutter diameter'),
            ({'spindle_speed': 18000.5}, 'whole number'),
            ({'safe_z': float('nan')}, 'finite'),
            ({'tool_diameter': 0.001}, 'raster passes'),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(BedLevelingError, message):
                parse_spec(valid_data(**changes))


class TestBedLevelingRoute(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_api_returns_preview_and_program(self):
        data = valid_data(width=10)
        data['length'] = data.pop('height')
        response = self.client.post('/api/bed-leveling', json=data)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertGreater(len(payload['path']), 2)
        self.assertGreater(payload['stats']['rows'], 1)
        self.assertEqual(payload['stats']['passes'], payload['stats']['rows'])
        self.assertEqual(payload['stats']['pass_axis'], 'Y')
        self.assertIn('SPOILBOARD SURFACING', payload['gcode'])
        self.assertEqual(payload['area'], {'width': 10.0, 'length': 18.0})
        # Submitted manual numbers are intentionally ignored: these come from the
        # machine/material/tool chipload model.
        self.assertEqual(payload['feeds']['source'], 'chipload model')
        self.assertNotEqual(payload['feeds']['feed_rate'], data['feed_rate'])
        self.assertIn(f"S{payload['feeds']['spindle_speed']} M3", payload['gcode'])

    def test_api_recalculates_when_tool_changes(self):
        one_flute = valid_data(width=10, height=8, tool_diameter=0.125,
                               flutes=1, material='plywood')
        two_flute = dict(one_flute, flutes=2)
        first = self.client.post('/api/bed-leveling', json=one_flute).get_json()
        second = self.client.post('/api/bed-leveling', json=two_flute).get_json()
        self.assertTrue(first['success'])
        self.assertTrue(second['success'])
        self.assertGreater(second['feeds']['feed_rate'], first['feeds']['feed_rate'])

    def test_api_rejects_fractional_flutes(self):
        response = self.client.post('/api/bed-leveling',
                                    json=valid_data(flutes=2.5))
        self.assertEqual(response.status_code, 400)
        self.assertIn('whole number', response.get_json()['error'])

    def test_api_explains_validation_error(self):
        response = self.client.post('/api/bed-leveling', json=valid_data(depth=1))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('Cut depth', response.get_json()['error'])

    def test_api_rejects_non_object_json_cleanly(self):
        response = self.client.post('/api/bed-leveling', json=[])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('JSON object', response.get_json()['error'])

    def test_page_renders_generator_controls(self):
        with mock.patch('frc_cam_gui_app._require_onshape_auth', return_value=None):
            response = self.client.get('/bed-leveling')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        html = response.get_data(as_text=True)
        self.assertIn('id="level-form"', html)
        self.assertIn('id="level-canvas"', html)
        self.assertIn('bed_leveling.js', html)

    def test_page_defaults_come_from_selected_machine_config(self):
        config = {
            'version': 2,
            'default_machine': 'metric_router',
            'machines': {'metric_router': {
                'name': 'Metric router',
                'machine': {'dimensions': {
                    'x_max': '800mm', 'y_max': '500mm', 'z_max': '130mm'}},
                'machining': {
                    'bed_leveling': {
                        'tool_diameter': '25.4mm',
                        'stepover_percent': 55,
                        'depth': '0.2mm',
                        'safe_z': '12.7mm',
                        'feed_rate': 82,
                        'plunge_rate': 17,
                        'spindle_speed': 16000,
                    },
                },
            }},
        }
        with self.client.session_transaction() as session:
            session['team_config_data'] = config
            session['machine_id'] = 'metric_router'
        with mock.patch('frc_cam_gui_app._require_onshape_auth', return_value=None):
            response = self.client.get('/bed-leveling')
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200, html)
        self.assertIn('id="level-width" type="number" min="0.1" max="31.496062992125985"', html)
        self.assertIn('id="level-length" type="number" min="0.1" max="19.68503937007874"', html)
        self.assertIn('id="level-tool" type="number" min="0.01" step="0.001" value="0.9999999999999999"', html)
        self.assertIn('id="level-stepover" type="number" min="10" max="90" step="1" value="55"', html)
        self.assertIn('id="level-feed" type="number" value="82" readonly', html)

    def test_api_uses_requested_machine_travel_from_config(self):
        config = {
            'version': 2,
            'default_machine': 'large',
            'machines': {
                'large': {'machine': {'dimensions': {
                    'x_max': 48, 'y_max': 48, 'z_max': 8}}},
                'small': {'machine': {'dimensions': {
                    'x_max': 12, 'y_max': 8, 'z_max': 3}}},
            },
        }
        with self.client.session_transaction() as session:
            session['team_config_data'] = config
            session['machine_id'] = 'large'
        response = self.client.post(
            '/api/bed-leveling', json=valid_data(machine_id='small', width=13, height=8))
        self.assertEqual(response.status_code, 400)
        self.assertIn('12', response.get_json()['error'])


class TestBedLevelingConfig(unittest.TestCase):
    def test_missing_leveling_profile_derives_from_machine_settings(self):
        config = TeamConfig({
            'version': 2,
            'default_machine': 'router',
            'machines': {'router': {
                'default_tool': {'diameter': '6mm'},
                'machining': {
                    'default_material': 'plywood',
                    'z_reference': {
                        'sacrifice_board_depth': '0.2mm',
                        'clearance_height': '10mm',
                    },
                },
                'materials': {'plywood': {
                    'stepover_percentage': 0.42,
                    'feed_rate': 73,
                    'plunge_rate': 19,
                    'spindle_speed': 17000,
                }},
            }},
        })
        defaults = config.bed_leveling_defaults('router')
        self.assertAlmostEqual(defaults['tool_diameter'], 6 / 25.4)
        self.assertAlmostEqual(defaults['stepover_percent'], 42)
        self.assertAlmostEqual(defaults['depth'], 0.2 / 25.4)
        self.assertAlmostEqual(defaults['safe_z'], 10 / 25.4)
        self.assertEqual(defaults['feed_rate'], 73)
        self.assertEqual(defaults['plunge_rate'], 19)
        self.assertEqual(defaults['spindle_speed'], 17000)

    def test_spoilboard_defaults_to_plywood_not_job_material(self):
        config = TeamConfig()
        defaults = config.bed_leveling_defaults()
        self.assertEqual(defaults['material'], 'plywood')
        self.assertEqual(defaults['stepover_percent'], 65)
        self.assertEqual(defaults['flutes'], 2)


if __name__ == '__main__':
    unittest.main()
