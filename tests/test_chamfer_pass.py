"""The standard-mode deburr / chamfer pass: a V-bit edge break appended to a
single-tool program behind a manual tool change.

What matters here, in order:
  1. Ordering - the chamfer runs after the profile and BEFORE tab removal, because the
     tabs are the only thing holding the part while the V-bit runs.
  2. Geometry refusals - a pass that cannot physically work (wider than the bit can
     cut, deeper than the stock, a part with a waist thinner than two breaks) must fail
     the program, never silently cut.
  3. The depth math - depth = width / tan(angle / 2), checked at two angles so a wrong
     tangent cannot hide behind the 90-degree case where depth == width.
"""
import io
import json
import math
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

from frc_cam_postprocessor import (
    FRCPostProcessor, assemble_job_gcode, parse_chamfer_spec,
)
from team_config import TeamConfig


def _plate_dxf(size=4.0, hole_diameter=0.5, pocket=None):
    """A square plate with one centred hole (and optionally one square pocket)."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (size, 0), (size, size), (0, size)], close=True)
    if hole_diameter:
        msp.add_circle((size / 2, size / 2), hole_diameter / 2)
    if pocket:
        x0, y0, x1, y1 = pocket
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


def _build_pp(dxf_path, thickness=0.25, tool=0.157, config=None):
    pp = FRCPostProcessor(material_thickness=thickness, tool_diameter=tool,
                          units='inch', config=config)
    pp.apply_material_preset('plywood')
    pp.load_dxf(dxf_path)
    pp.transform_coordinates('bottom-left', 0)
    pp.identify_perimeter_and_pockets()
    pp.classify_holes()
    return pp


def _chamfer(width=0.02, bit=0.5, angle=90.0, targets=('perimeter', 'holes', 'pockets')):
    return parse_chamfer_spec({'width': width, 'bit_diameter': bit,
                               'bit_angle': angle, 'targets': list(targets)})


class TestParseChamferSpec(unittest.TestCase):
    def test_accepts_numbers_and_numeric_strings(self):
        spec = parse_chamfer_spec({'width': '0.02', 'bit_diameter': '0.5',
                                   'bit_angle': '60', 'targets': 'perimeter, holes'})
        self.assertEqual(spec['width'], 0.02)
        self.assertEqual(spec['bit_diameter'], 0.5)
        self.assertEqual(spec['bit_angle'], 60.0)
        self.assertEqual(spec['targets'], ['perimeter', 'holes'])

    def test_defaults_angle_and_targets(self):
        spec = parse_chamfer_spec({'width': 0.02, 'bit_diameter': 0.25})
        self.assertEqual(spec['bit_angle'], 90.0)
        self.assertEqual(spec['targets'], ['perimeter'])

    def test_rejects_bad_values(self):
        base = {'width': 0.02, 'bit_diameter': 0.5, 'bit_angle': 90}
        for bad in ({**base, 'width': 0}, {**base, 'width': -0.1},
                    {**base, 'width': float('nan')}, {**base, 'width': 'wide'},
                    {**base, 'bit_diameter': 0}, {**base, 'bit_diameter': 5},
                    {**base, 'bit_angle': 0}, {**base, 'bit_angle': 180},
                    {**base, 'targets': ['edges']}, {**base, 'targets': []},
                    'not a dict'):
            with self.assertRaises(ValueError):
                parse_chamfer_spec(bad)

    def test_missing_width_or_bit_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_chamfer_spec({'bit_diameter': 0.5})
        with self.assertRaises(ValueError):
            parse_chamfer_spec({'width': 0.02})


class TestSingleProgram(unittest.TestCase):
    """generate_gcode with a chamfer pass on a plate with a hole and a pocket."""

    @classmethod
    def setUpClass(cls):
        cls.dxf = _plate_dxf(pocket=(0.75, 0.75, 1.5, 1.5))
        pp = _build_pp(cls.dxf)
        pp.chamfer_pass = _chamfer()
        cls.pp = pp
        with io.StringIO() as buf:
            from contextlib import redirect_stdout
            with redirect_stdout(buf):
                cls.result = pp.generate_gcode(suggested_filename='cham',
                                               timestamp='2026-08-24 12:00:00')

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.dxf)

    def test_generates(self):
        self.assertTrue(self.result.success, self.result.errors)

    def test_ordering_profile_then_chamfer_then_tab_removal(self):
        g = self.result.gcode
        i_perimeter = g.find('PERIMETER WITH TABS')
        i_change = g.find('TOOL CHANGE - DEBURR CHAMFER PASS')
        i_chamfer = g.find('===== CHAMFER =====')
        i_back = g.find('TOOL CHANGE - BACK TO END MILL')
        i_tabs = g.find('TAB REMOVAL')
        self.assertTrue(-1 < i_perimeter < i_change < i_chamfer < i_back < i_tabs,
                        f'sections out of order: {i_perimeter} {i_change} '
                        f'{i_chamfer} {i_back} {i_tabs}')

    def test_chamfers_every_requested_edge(self):
        g = self.result.gcode
        self.assertIn('(Perimeter)', g)
        self.assertIn('(Hole 0.500 in dia', g)
        self.assertIn('(Pocket at', g)

    def test_header_lists_both_tools(self):
        g = self.result.gcode
        self.assertIn('MANUAL TOOL CHANGES REQUIRED', g)
        self.assertIn('90 deg V-bit', g)
        self.assertIn('end mill', g)

    def test_depth_is_width_at_90_degrees(self):
        # cut Z = material top - width for a 90 deg bit: 0.25 - 0.02 = 0.23
        self.assertIn('tip depth 0.0200" below material top', self.result.gcode)
        self.assertIn('Z0.2300', self.result.gcode)

    def test_operator_rezero_instructions_present(self):
        self.assertIn('Re-zero G54 Z to the sacrifice board surface', self.result.gcode)

    def test_comment_rules(self):
        for line in self.result.gcode.split('\n'):
            self.assertIsNone(re.search(r'\([^)]*\(', line),
                              f'nested comment: {line!r}')
            self.assertNotIn('[', line, f'bracket in line: {line!r}')
            self.assertTrue(all(ord(c) < 128 for c in line),
                            f'non-ASCII in line: {line!r}')

    def test_spindle_restarts_after_each_pause(self):
        lines = self.result.gcode.split('\n')
        for i, line in enumerate(lines):
            if line.startswith('M0'):
                after = '\n'.join(lines[i:i + 12])
                self.assertIn('M3', after, 'spindle not restarted after a pause')


class TestDepthFollowsAngle(unittest.TestCase):
    def test_narrower_bit_goes_deeper(self):
        # depth = width / tan(angle/2): 60 deg -> 0.02 / tan(30) = 0.0346
        self.assertAlmostEqual(FRCPostProcessor.chamfer_depth(0.02, 60.0),
                               0.02 / math.tan(math.radians(30)), places=6)
        self.assertAlmostEqual(FRCPostProcessor.chamfer_depth(0.02, 90.0), 0.02,
                               places=9)

    def test_sixty_degree_program_states_the_deeper_tip(self):
        dxf = _plate_dxf()
        try:
            pp = _build_pp(dxf)
            pp.chamfer_pass = _chamfer(angle=60.0, targets=('perimeter',))
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                result = pp.generate_gcode(timestamp='2026-08-24 12:00:00')
            self.assertTrue(result.success, result.errors)
            self.assertIn('tip depth 0.0346" below material top', result.gcode)
        finally:
            os.remove(dxf)


class TestRefusals(unittest.TestCase):
    def _generate(self, pp):
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            return pp.generate_gcode(timestamp='2026-08-24 12:00:00')

    def test_width_beyond_the_bits_reach(self):
        dxf = _plate_dxf()
        try:
            pp = _build_pp(dxf)
            # A 0.25" bit reaches 0.125" sideways; ask for more.
            pp.chamfer_pass = _chamfer(width=0.2, bit=0.25, targets=('perimeter',))
            result = self._generate(pp)
            self.assertFalse(result.success)
            self.assertTrue(any('exceeds what' in e for e in result.errors),
                            result.errors)
        finally:
            os.remove(dxf)

    def test_depth_through_the_stock(self):
        dxf = _plate_dxf()
        try:
            # 30 deg bit, 0.15" break -> tip 0.56" down through 0.25" stock.
            pp = _build_pp(dxf)
            pp.chamfer_pass = _chamfer(width=0.15, bit=0.5, angle=30.0,
                                       targets=('perimeter',))
            result = self._generate(pp)
            self.assertFalse(result.success)
            self.assertTrue(any('through' in e for e in result.errors), result.errors)
        finally:
            os.remove(dxf)

    def test_tabless_part_refuses_the_pass(self):
        dxf = _plate_dxf()
        try:
            config = TeamConfig({'machining': {'tabs': {'enabled': False}}})
            pp = _build_pp(dxf, config=config)
            self.assertFalse(pp.tabs_enabled)
            pp.chamfer_pass = _chamfer(targets=('perimeter',))
            result = self._generate(pp)
            self.assertFalse(result.success)
            self.assertTrue(any('needs tabs' in e for e in result.errors),
                            result.errors)
        finally:
            os.remove(dxf)

    def test_narrow_waist_refuses_the_perimeter_break(self):
        # Two 1" lobes joined by a 0.02"-wide neck: each side's 0.02" chamfer meets the
        # other's in the middle of the waist. The erosion test must catch this even
        # though the part as a whole survives erosion (as two islands).
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([
            (0, 0), (1, 0), (1, 0.49), (2, 0.49), (2, 0), (3, 0),
            (3, 1), (2, 1), (2, 0.51), (1, 0.51), (1, 1), (0, 1),
        ], close=True)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        try:
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.157,
                                  units='inch')
            pp.apply_material_preset('plywood')
            pp.load_dxf(path)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            pp.chamfer_pass = _chamfer(width=0.02, targets=('perimeter',))
            result = self._generate(pp)
            self.assertFalse(result.success)
            self.assertTrue(any('too narrow' in e for e in result.errors),
                            result.errors)
        finally:
            os.remove(path)

    def test_layered_part_refuses_the_pass(self):
        dxf = _plate_dxf()
        try:
            pp = _build_pp(dxf)
            pp.chamfer_pass = _chamfer()
            pp.layer_data = {'DEPTH_0.1': []}   # pretend 2.5D layers were found
            result = self._generate(pp)
            self.assertFalse(result.success)
            self.assertTrue(any('2D parts only' in e for e in result.errors),
                            result.errors)
        finally:
            os.remove(dxf)


class TestJobAssembly(unittest.TestCase):
    """Multi-part jobs share ONE V-bit change for the whole sheet."""

    @classmethod
    def setUpClass(cls):
        cls.dxf = _plate_dxf(size=3.0)
        from contextlib import redirect_stdout
        part_jobs = []
        header_pp = None
        with redirect_stdout(io.StringIO()):
            for i, offset in enumerate((0.0, 4.0)):
                pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.157,
                                      units='inch')
                pp.apply_material_preset('plywood')
                pp.load_dxf(cls.dxf)
                pp.transform_coordinates('bottom-left', 0,
                                         placement_offset=(offset, 0.0),
                                         enforce_bounds=False)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                pp.chamfer_pass = _chamfer(targets=('perimeter', 'holes'))
                phases = pp.generate_part_phases()
                assert not phases['errors'], phases['errors']
                part_jobs.append({'name': f'p{i}', 'place_x': offset, 'place_y': 0.0,
                                  'rotation': 0, **{k: phases[k] for k in
                                  ('interior', 'perimeter', 'chamfer', 'tab_removal')}})
                header_pp = header_pp or pp
            cls.result = assemble_job_gcode(part_jobs, header_pp,
                                            timestamp='2026-08-24 12:00:00',
                                            suggested_filename='chamjob')

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.dxf)

    def test_phase_order(self):
        g = self.result.gcode
        i_perim = g.find('PHASE: PERIMETERS')
        i_to_v = g.find('TOOL CHANGE - DEBURR CHAMFER PASS')
        i_cham = g.find('PHASE: DEBURR CHAMFER PASS')
        i_back = g.find('TOOL CHANGE - BACK TO END MILL')
        i_tabs = g.find('PHASE: TAB REMOVAL')
        self.assertTrue(-1 < i_perim < i_to_v < i_cham < i_back < i_tabs,
                        f'phases out of order: {i_perim} {i_to_v} {i_cham} '
                        f'{i_back} {i_tabs}')

    def test_one_shared_change_each_way(self):
        g = self.result.gcode
        self.assertEqual(g.count('TOOL CHANGE - DEBURR CHAMFER PASS'), 1)
        self.assertEqual(g.count('TOOL CHANGE - BACK TO END MILL'), 1)

    def test_every_part_chamfered(self):
        chamfer_phase = self.result.gcode.split('PHASE: DEBURR CHAMFER PASS')[1] \
                                         .split('TOOL CHANGE - BACK TO END MILL')[0]
        self.assertIn('PART 1', chamfer_phase)
        self.assertIn('PART 2', chamfer_phase)

    def test_job_header_lists_both_tools(self):
        self.assertIn('MANUAL TOOL CHANGES REQUIRED', self.result.gcode)

    def test_job_without_chamfer_is_unchanged(self):
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.157,
                                  units='inch')
            pp.apply_material_preset('plywood')
            pp.load_dxf(self.dxf)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            phases = pp.generate_part_phases()
            result = assemble_job_gcode(
                [{'name': 'p', 'place_x': 0, 'place_y': 0, 'rotation': 0,
                  **{k: phases[k] for k in ('interior', 'perimeter', 'chamfer',
                                            'tab_removal')}}],
                pp, timestamp='2026-08-24 12:00:00')
        self.assertNotIn('TOOL CHANGE', result.gcode)
        self.assertNotIn('CHAMFER', result.gcode)
        self.assertNotIn('MANUAL TOOL CHANGES', result.gcode)


class TestProcessJobRoute(unittest.TestCase):
    """The /process-job route carries the chamfer spec to every part."""

    @classmethod
    def setUpClass(cls):
        from frc_cam_gui_app import app
        app.config['TESTING'] = True
        cls.client = app.test_client()
        dxf = _plate_dxf(size=3.0)
        with open(dxf, 'rb') as fh:
            cls.dxf_bytes = fh.read()
        os.remove(dxf)

    def _post(self, chamfer):
        job = {'material': 'plywood', 'tool_diameter': 0.157, 'thickness': 0.25,
               'tab_spacing': 6.0, 'stock': {'width': 10, 'height': 10},
               'name': 'chamjob',
               'parts': [{'file_index': 0, 'name': 'plate',
                          'place_x': 0, 'place_y': 0, 'rotation': 0}]}
        if chamfer is not None:
            job['chamfer'] = chamfer
        data = {'job': json.dumps(job), 'timestamp': '2026-08-24 12:00:00',
                'file_0': (io.BytesIO(self.dxf_bytes), 'plate.dxf')}
        return self.client.post('/process-job', data=data,
                                content_type='multipart/form-data')

    def test_chamfered_job(self):
        response = self._post({'width': 0.02, 'bit_diameter': 0.5, 'bit_angle': 90,
                               'targets': ['perimeter', 'holes']})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertIn('PHASE: DEBURR CHAMFER PASS', body['gcode'])

    def test_bad_spec_is_a_400_naming_the_problem(self):
        response = self._post({'width': 'wide', 'bit_diameter': 0.5})
        self.assertEqual(response.status_code, 400)
        self.assertIn('width', response.get_json()['error'])

    def test_omitted_chamfer_leaves_the_program_alone(self):
        response = self._post(None)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertNotIn('CHAMFER', body['gcode'])


if __name__ == '__main__':
    unittest.main()
