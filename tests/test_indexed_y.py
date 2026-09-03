"""Regression coverage for end-for-end oversized Y machining."""

import os
import io
import json
import re
import tempfile
import unittest

import ezdxf

import tooling
from frc_cam_postprocessor import build_resume_programs
from team_config import TeamConfig


class IndexedYTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.dxf')
        os.close(handle)
        doc = ezdxf.new()
        model = doc.modelspace()
        model.add_lwpolyline([(0, 0), (9, 0), (9, 49), (0, 49)], close=True)
        model.add_circle((4.5, 4.5), 0.25)
        model.add_circle((4.5, 44.5), 0.25)
        doc.saveas(self.path)
        self.config = TeamConfig({
            'version': 2, 'default_machine': 'omio_x8',
            'machines': {'omio_x8': {'machine': {'dimensions': {
                'x_max': 23.5, 'y_max': 36, 'z_max': 5}}}},
        })

    def tearDown(self):
        os.unlink(self.path)

    def spec(self, length=50):
        return {
            'material': 'plywood', 'thickness': .25, 'machine_id': 'omio_x8',
            'stock': {'width': 10, 'height': length, 'from_library': True},
            'indexing': {'axis': 'y', 'method': 'rotate_180', 'fixture': {}},
            'tools': [{'slot': 1, 'name': '4 mm end mill', 'diameter': .157,
                       'flutes': 1, 'type': 'endmill'}],
            'parts': [{'file_index': 0, 'name': 'long plate',
                       'place_x': .5, 'place_y': .5,
                       'operations': [{'op_type': 'holes', 'tool_slot': 1},
                                      {'op_type': 'perimeter', 'tool_slot': 1}]}],
        }

    def generate(self, spec=None):
        job = tooling.job_from_dict(spec or self.spec(), {0: self.path}, config=self.config)
        return tooling.generate_multitool_job(job, timestamp='2026-09-03 12:00:00')

    def test_generates_two_setups_and_safe_recovery(self):
        result = self.generate()
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.stats['setup_count'], 2)
        self.assertAlmostEqual(result.stats['overlap'], 22)
        self.assertEqual(result.gcode.count('TURN STOCK END FOR END'), 1)
        before, after = result.gcode.split('M0  ; Program pause', 1)
        self.assertIn('M5  ; Spindle off', before)
        self.assertIn('RESUME CHECKPOINT FLIP01', after)
        self.assertIn('M3', after)
        resumes = build_resume_programs(result.gcode, result.filename)
        self.assertIn('FLIP01', [item['checkpoint'] for item in resumes])

    def test_every_setup_coordinate_stays_inside_machine_travel(self):
        result = self.generate()
        words = [(axis, float(value)) for line in result.gcode.splitlines()
                 if line.startswith(('G0', 'G1', 'G2', 'G3'))
                 for axis, value in re.findall(r'([XY])(-?\d+(?:\.\d*)?)', line)]
        self.assertGreaterEqual(min(v for a, v in words if a == 'X'), -1e-6)
        self.assertLessEqual(max(v for a, v in words if a == 'X'), 23.5 + 1e-6)
        self.assertGreaterEqual(min(v for a, v in words if a == 'Y'), -1e-6)
        self.assertLessEqual(max(v for a, v in words if a == 'Y'), 36 + 1e-6)

    def test_setup_two_is_the_rotated_far_end_and_holes_are_not_duplicated(self):
        result = self.generate()
        setup1, setup2 = result.gcode.split('RESUME CHECKPOINT FLIP01', 1)
        self.assertIn('X5.1021 Y5.0000', setup1)
        # Original far hole at (5,45) becomes (10-5, 50-45).
        self.assertIn('X4.8979 Y5.0000', setup2)
        self.assertEqual(result.gcode.count('(Hole 1 -'), 2)

    def test_fixture_has_three_pins_witness_and_automatic_cutting_words(self):
        fixture = self.generate().stats['fixture_gcode']
        self.assertEqual(fixture.count('(Locator P'), 3)
        self.assertIn('Shallow L witness', fixture)
        self.assertRegex(fixture, r'\bS\d+')
        self.assertRegex(fixture, r'\bF\d')
        self.assertIn('M3', fixture)
        self.assertLess(fixture.index('M3'), fixture.index('(Locator P1)'))

    def test_refuses_length_without_safe_overlap(self):
        result = self.generate(self.spec(length=71.5))
        self.assertFalse(result.success)
        self.assertTrue(any('overlap' in error.lower() for error in result.errors))

    def test_requires_exact_saved_stock_and_one_part(self):
        spec = self.spec()
        spec['stock'].pop('from_library')
        with self.assertRaisesRegex(tooling.ToolingError, 'exact saved stock'):
            tooling.job_from_dict(spec, {0: self.path}, config=self.config)
        spec = self.spec()
        spec['parts'].append(dict(spec['parts'][0], name='second'))
        with self.assertRaisesRegex(tooling.ToolingError, 'exactly one'):
            tooling.job_from_dict(spec, {0: self.path}, config=self.config)


class IndexedYUiContractTest(unittest.TestCase):
    def test_wizard_explains_turn_and_sends_indexing_contract(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, 'templates', 'wizard.html'), encoding='utf-8') as f:
            html = f.read()
        with open(os.path.join(root, 'static', 'wizard.js'), encoding='utf-8') as f:
            js = f.read()
        with open(os.path.join(root, 'static', 'multitool.js'), encoding='utf-8') as f:
            multitool = f.read()
        self.assertIn('Two-setup long part', html)
        self.assertIn('Mirror for back side', html)
        self.assertIn('Same face stays up', html)
        self.assertIn('SETUP 2 · ROTATE 180°', js)
        self.assertIn("method: 'rotate_180'", multitool)
        self.assertIn('pin_depth', multitool)


class IndexedYRouteTest(unittest.TestCase):
    def test_route_returns_fixture_and_flip_recovery_downloads(self):
        from frc_cam_gui_app import app
        app.config['TESTING'] = True
        client = app.test_client()
        path = self._long_dxf()
        with open(path, 'rb') as handle:
            dxf = handle.read()
        os.unlink(path)
        spec = {
            'material': 'plywood', 'thickness': .25, 'machine_id': 'omio_x8',
            'stock': {'width': 10, 'height': 50, 'from_library': True},
            'indexing': {'axis': 'y', 'method': 'rotate_180', 'fixture': {}},
            'tools': [{'slot': 1, 'name': '4 mm', 'diameter': .157,
                       'flutes': 1, 'type': 'endmill'}],
            'parts': [{'file_index': 0, 'name': 'long', 'place_x': .5, 'place_y': .5,
                       'rotation': 0, 'mirror': False,
                       'operations': [{'op_type': 'holes', 'tool_slot': 1},
                                      {'op_type': 'perimeter', 'tool_slot': 1}]}],
        }
        with client.session_transaction() as session:
            session['team_config_data'] = {
                'version': 2, 'default_machine': 'omio_x8',
                'machines': {'omio_x8': {'machine': {'dimensions': {
                    'x_max': 23.5, 'y_max': 36, 'z_max': 5}}}},
            }
        response = client.post('/process-multitool', data={
            'file_0': (io.BytesIO(dxf), 'long.dxf'), 'job': json.dumps(spec),
            'timestamp': '2026-09-03 12:00:00',
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body['indexing']['setup_count'], 2)
        self.assertTrue(body['fixture_file'])
        self.assertIn('FLIP01', [item['checkpoint'] for item in body['restart_files']])
        fixture = client.get('/download/' + body['fixture_file']['filename'])
        self.assertEqual(fixture.status_code, 200)
        self.assertIn(b'OVERSIZED-Y LOCATOR FIXTURE', fixture.data)

    @staticmethod
    def _long_dxf():
        handle, path = tempfile.mkstemp(suffix='.dxf')
        os.close(handle)
        doc = ezdxf.new(); model = doc.modelspace()
        model.add_lwpolyline([(0, 0), (9, 0), (9, 49), (0, 49)], close=True)
        model.add_circle((4.5, 4.5), .25); model.add_circle((4.5, 44.5), .25)
        doc.saveas(path)
        return path


if __name__ == '__main__':
    unittest.main()
