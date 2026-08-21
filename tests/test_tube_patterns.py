"""Pre-designed tube patterns: the generated geometry, and the G-code it produces.

Two jobs here. The first is the pattern itself - where the holes land, that the truss
pockets never eat into a hole or into each other, and that a pocket too tight for the
cutter is dropped rather than emitted. The second is a gap this feature exposed: the
G-code formatting rules (no nested comments, no square brackets, ASCII only) were only
ever checked against a standard plate program, so the tube path had been emitting
bracketed comments since tubing was added. Those rules are checked here against a real
tube program.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely.geometry import Point, Polygon

import tube_patterns
from frc_cam_postprocessor import FRCPostProcessor

TOOL = 0.157


class TestPatternGeometry(unittest.TestCase):
    """Where the holes and pockets land."""

    def test_two_rows_on_a_2x1_face(self):
        self.assertEqual(tube_patterns.hole_rows(2.0), [0.5, 1.5])

    def test_single_centred_row_on_a_1x1_face(self):
        self.assertEqual(tube_patterns.hole_rows(1.0), [0.5])

    def test_holes_are_on_half_inch_centres(self):
        ys = tube_patterns.hole_run(12.0)
        gaps = {round(b - a, 6) for a, b in zip(ys, ys[1:])}
        self.assertEqual(gaps, {0.5})

    def test_hole_run_is_centred_on_the_tube(self):
        """Equal material at both ends - a tube stays symmetric when it is cut down."""
        length = 13.3
        ys = tube_patterns.hole_run(length)
        self.assertAlmostEqual(ys[0], length - ys[-1], places=9)

    def test_holes_clear_both_ends(self):
        for length in (4.0, 12.0, 13.3, 36.0):
            ys = tube_patterns.hole_run(length)
            self.assertGreaterEqual(ys[0], tube_patterns.MIN_END_MARGIN - 1e-9)
            self.assertLessEqual(ys[-1], length - tube_patterns.MIN_END_MARGIN + 1e-9)

    def test_tube_too_short_gets_no_holes_rather_than_a_torn_one(self):
        self.assertEqual(tube_patterns.hole_run(0.5), [])

    def test_short_tube_warns_instead_of_failing(self):
        pattern = tube_patterns.generate(2.0, 0.5, TOOL)
        self.assertEqual(pattern['circles'], [])
        self.assertTrue(any('too short' in w for w in pattern['warnings']))

    def test_holes_use_number_10_clearance(self):
        pattern = tube_patterns.generate(2.0, 12.0, TOOL)
        self.assertTrue(all(abs(c['diameter'] - 0.201) < 1e-9 for c in pattern['circles']))

    def test_hole_count_matches_rows_times_run(self):
        pattern = tube_patterns.generate(2.0, 12.0, TOOL)
        expected = len(tube_patterns.hole_rows(2.0)) * len(tube_patterns.hole_run(12.0))
        self.assertEqual(len(pattern['circles']), expected)


class TestTrussPockets(unittest.TestCase):
    """The lightening pockets, which are the part that can damage a tube if wrong."""

    def setUp(self):
        self.pattern = tube_patterns.generate(2.0, 24.0, TOOL)
        self.polys = [Polygon(ring) for ring in self.pattern['pockets']]

    def test_a_2x1_face_gets_pockets(self):
        self.assertTrue(self.polys)

    def test_pockets_are_right_triangles(self):
        for poly in self.polys:
            ring = list(poly.exterior.coords)[:-1]
            self.assertEqual(len(ring), 3, 'pocket should be a triangle')
            # Exactly one angle of 90 degrees.
            right_angles = 0
            for i in range(3):
                a, b, c = ring[i], ring[(i + 1) % 3], ring[(i + 2) % 3]
                v1 = (a[0] - b[0], a[1] - b[1])
                v2 = (c[0] - b[0], c[1] - b[1])
                if abs(v1[0] * v2[0] + v1[1] * v2[1]) < 1e-9:
                    right_angles += 1
            self.assertEqual(right_angles, 1, 'pocket should have one right angle')

    def test_pockets_alternate_orientation(self):
        """The zigzag is the point: consecutive triangles must not be identical, or the
        web between them runs straight along the tube and carries load in bending."""
        apex_x = []
        for poly in self.polys:
            ring = list(poly.exterior.coords)[:-1]
            ys = [p[1] for p in ring]
            apex = ring[ys.index(max(ys))]      # the vertex off the shared base
            apex_x.append(apex[0])
        self.assertGreater(len(set(round(x, 4) for x in apex_x)), 1)

    def test_pockets_never_touch_a_hole(self):
        holes = [Point(c['center']).buffer(c['radius']) for c in self.pattern['circles']]
        for poly in self.polys:
            for hole in holes:
                self.assertFalse(poly.intersects(hole) and poly.intersection(hole).area > 1e-12)

    def test_pockets_keep_the_minimum_web_to_every_hole(self):
        holes = [Point(c['center']).buffer(c['radius']) for c in self.pattern['circles']]
        worst = min(poly.distance(hole) for poly in self.polys for hole in holes)
        self.assertGreaterEqual(worst, tube_patterns.MIN_WEB - 1e-6)

    def test_pockets_do_not_overlap_each_other(self):
        for i, a in enumerate(self.polys):
            for b in self.polys[i + 1:]:
                self.assertFalse(a.intersects(b) and a.intersection(b).area > 1e-12)

    def test_pockets_stay_on_the_face(self):
        for poly in self.polys:
            minx, miny, maxx, maxy = poly.bounds
            self.assertGreaterEqual(minx, 0.0)
            self.assertLessEqual(maxx, 2.0)
            self.assertGreaterEqual(miny, 0.0)
            self.assertLessEqual(maxy, 24.0)

    def test_a_1x1_face_gets_no_pockets(self):
        """One hole row leaves no band to lighten - not an error, just holes only."""
        pattern = tube_patterns.generate(1.0, 24.0, TOOL)
        self.assertEqual(pattern['pockets'], [])

    def test_pockets_dropped_when_the_tool_cannot_clear_them(self):
        """A cutter wider than the triangle's inscribed circle cannot clear the corner.
        Emitting the pocket anyway would leave the post-processor to silently skip or
        mangle it, so the pattern drops it and says so."""
        pattern = tube_patterns.generate(2.0, 24.0, tool_diameter=0.75)
        self.assertEqual(pattern['pockets'], [])
        self.assertTrue(any('does not fit' in w for w in pattern['warnings']))

    def test_holes_only_when_pockets_are_switched_off(self):
        pattern = tube_patterns.generate(2.0, 24.0, TOOL, pockets=False)
        self.assertEqual(pattern['pockets'], [])
        self.assertTrue(pattern['circles'])


class TestLoadTubePattern(unittest.TestCase):
    """The pattern reaching the post-processor through the same door a DXF uses."""

    def _pp(self, face_width=2.0, length=12.0):
        pp = FRCPostProcessor(0.0625, TOOL)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(face_width, length)
        return pp

    def test_holes_are_classified_not_hand_built(self):
        pp = self._pp()
        self.assertTrue(pp.holes)
        for hole in pp.holes:
            self.assertIn('needs_peck_drill', hole)

    def test_no_perimeter_is_invented_for_a_tube_face(self):
        """The tube face is the boundary; a perimeter here would be a cut across it."""
        self.assertIsNone(self._pp().perimeter)

    def test_pockets_reach_the_post_processor(self):
        self.assertTrue(self._pp().pockets)

    def test_a_1x1_loads_with_holes_and_no_pockets(self):
        pp = self._pp(face_width=1.0)
        self.assertTrue(pp.holes)
        self.assertEqual(pp.pockets, [])


class TestTubeGCodeFormatting(unittest.TestCase):
    """The formatting rules, checked against a TUBE program.

    These same rules are covered elsewhere against a plate program. Tube output goes
    through different code that emits its own header, setup and phase comments, and that
    code had never been checked - which is exactly how bracketed comments survived there.
    """

    @classmethod
    def setUpClass(cls):
        pp = FRCPostProcessor(0.0625, TOOL)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(2.0, 12.0)
        result = pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=True, cut_to_length=True,
            tube_width=2.0, tube_length=12.0)
        assert result.success, result.errors
        cls.gcode = result.gcode
        cls.lines = result.gcode.split('\n')

    def test_no_nested_comments(self):
        for n, line in enumerate(self.lines, 1):
            depth = 0
            for char in line:
                if char == '(':
                    depth += 1
                    self.assertLessEqual(depth, 1, f'line {n} nests comments: {line.strip()}')
                elif char == ')':
                    depth = max(0, depth - 1)

    def test_no_square_brackets_in_comments(self):
        for n, line in enumerate(self.lines, 1):
            inside = False
            for char in line:
                if char == '(':
                    inside = True
                elif char == ')':
                    inside = False
                elif inside and char in '[]':
                    self.fail(f'line {n} has a square bracket in a comment: {line.strip()}')

    def test_pure_ascii(self):
        for n, line in enumerate(self.lines, 1):
            try:
                line.encode('ascii')
            except UnicodeEncodeError as exc:
                self.fail(f'line {n} is not ASCII: {line.strip()} ({exc})')

    def test_both_faces_are_machined(self):
        self.assertIn('PHASE 1', self.gcode)
        self.assertIn('PHASE 2', self.gcode)

    def test_program_actually_cuts_the_pattern(self):
        self.assertIn('HOLES', self.gcode.upper())


class TestProcessRoute(unittest.TestCase):
    """The /process route, which now has a path that takes no file at all."""

    @classmethod
    def setUpClass(cls):
        os.environ['PENGUINCAM_LOCAL'] = '1'
        from frc_cam_gui_app import app
        app.config['TESTING'] = True
        cls.client = app.test_client()

    def _post(self, **overrides):
        data = {'material': 'aluminum_tube', 'tube_pattern': 'standard',
                'tube_size': '2x1-flat', 'tube_pattern_length': '24',
                'tube_pattern_pockets': '1', 'tube_height': '1.0',
                'thickness': '0.0625', 'tool_diameter': '0.157',
                'square_end': '0', 'cut_to_length': '0'}
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post('/process', data=data,
                                content_type='multipart/form-data')

    def test_a_pattern_job_needs_no_dxf(self):
        response = self._post()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        self.assertIn('PHASE 1', response.get_json()['gcode'])

    def test_pattern_job_without_a_length_is_refused(self):
        """Length is what decides how many holes fit; guessing it would be worse than
        refusing, because the operator would get a program for the wrong tube."""
        response = self._post(tube_pattern_length='0')
        self.assertEqual(response.status_code, 400)
        self.assertIn('length', response.get_json()['error'].lower())

    def test_holes_only_when_pockets_are_unticked(self):
        """Checked by counting pocket TOOLPATHS, not the word 'pocket' - the tube header
        says 'machining holes and pockets only' whether or not any pocket exists."""
        with_pockets = self._post(tube_pattern_pockets='1').get_json()['gcode']
        without = self._post(tube_pattern_pockets='0').get_json()['gcode']
        marker = 'Position at pocket center'
        self.assertGreater(with_pockets.count(marker), 0)
        self.assertEqual(without.count(marker), 0)

    def test_a_1x1_still_generates(self):
        response = self._post(tube_size='1x1')
        self.assertEqual(response.status_code, 200)

    def test_a_normal_job_still_requires_a_file(self):
        """Regression guard: the file requirement was loosened for generated patterns
        only, and must still hold for every other job."""
        response = self.client.post('/process', data={'material': 'plywood',
                                                      'thickness': '0.25'},
                                    content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        self.assertIn('file', response.get_json()['error'].lower())

    def test_a_tube_job_without_a_pattern_still_requires_a_file(self):
        response = self._post(tube_pattern='none')
        self.assertEqual(response.status_code, 400)
        self.assertIn('file', response.get_json()['error'].lower())


if __name__ == '__main__':
    unittest.main()
