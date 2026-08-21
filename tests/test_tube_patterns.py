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

    def test_three_holes_per_column_on_a_2x1_face(self):
        self.assertEqual(tube_patterns.hole_rows(2.0), [0.5, 1.0, 1.5])

    def test_one_hole_per_column_on_a_1x1_face(self):
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
        pattern = tube_patterns.generate(2.0, 0.5, TOOL, mode='holes')
        self.assertEqual(pattern['circles'], [])
        self.assertTrue(any('too short' in w for w in pattern['warnings']))

    def test_holes_use_number_10_clearance(self):
        pattern = tube_patterns.generate(2.0, 12.0, TOOL, mode='holes')
        self.assertTrue(all(abs(c['diameter'] - 0.201) < 1e-9 for c in pattern['circles']))

    def test_hole_count_matches_rows_times_run(self):
        pattern = tube_patterns.generate(2.0, 12.0, TOOL, mode='holes')
        expected = len(tube_patterns.hole_rows(2.0)) * len(tube_patterns.hole_run(12.0))
        self.assertEqual(len(tube_patterns.hole_rows(2.0)), 3)
        self.assertEqual(len(pattern['circles']), expected)


class TestTrussPockets(unittest.TestCase):
    """The lightening pockets, which are the part that can damage a tube if wrong."""

    def setUp(self):
        self.pattern = tube_patterns.generate(2.0, 24.0, TOOL, mode='lightening')
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

    def test_a_lightening_pattern_has_no_holes_to_collide_with(self):
        self.assertEqual(self.pattern['circles'], [])

    def test_pockets_keep_material_along_both_long_edges(self):
        """The corner radius of the extrusion is the stiffest part of the section."""
        for poly in self.polys:
            minx, _, maxx, _ = poly.bounds
            self.assertGreaterEqual(minx, tube_patterns.LIGHTENING_EDGE_MARGIN - 1e-9)
            self.assertLessEqual(maxx, 2.0 - tube_patterns.LIGHTENING_EDGE_MARGIN + 1e-9)

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

    def test_pockets_are_separated_by_the_web_at_every_length(self):
        """The specific guarantee: consecutive triangles never overlap, and the gap is
        the web - not merely 'some' clearance that could drift to nothing."""
        for length in (4.0, 4.75, 8.0, 12.5, 24.0, 36.0, 48.0):
            polys = [Polygon(r) for r in tube_patterns.generate(2.0, length, TOOL, mode='lightening')['pockets']]
            for a, b in zip(polys, polys[1:]):
                self.assertFalse(a.intersects(b) and a.intersection(b).area > 1e-12,
                                 f'pockets overlap on a {length}" tube')
                self.assertAlmostEqual(a.distance(b), tube_patterns.MIN_WEB, places=6,
                                       msg=f'gap drifted on a {length}" tube')

    def test_triangles_are_worth_cutting(self):
        """Guards the size against silently shrinking back. A 2x1 triangle spans the full
        band between the hole rows and most of a two-inch cell."""
        poly = Polygon(tube_patterns.generate(2.0, 24.0, TOOL, mode='lightening')['pockets'][0])
        minx, miny, maxx, maxy = poly.bounds
        self.assertGreater(maxy - miny, 1.5, 'triangle got short along the tube')
        self.assertGreater(maxx - minx, 1.4, 'triangle no longer spans the face')
        self.assertGreater(poly.area, 1.2, 'triangle area shrank')

    def test_the_overlap_guard_actually_catches_an_overlap(self):
        """The guard is only worth having if it fires. Two triangles that genuinely share
        area must be detected, or the check in truss_pockets is decoration."""
        overlapping = [
            [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (0.0, 0.0)],
            [(0.0, 0.5), (1.0, 0.5), (0.0, 1.5), (0.0, 0.5)],
        ]
        self.assertEqual(tube_patterns._first_overlap(overlapping), (0, 1))

    def test_the_overlap_guard_ignores_shapes_that_only_touch(self):
        """Edge contact is not overlap - nothing is cut twice - so it must not be
        reported, or every real pattern would be thrown away."""
        touching = [
            [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (0.0, 0.0)],
            [(0.0, 1.0), (1.0, 1.0), (0.0, 2.0), (0.0, 1.0)],
        ]
        self.assertIsNone(tube_patterns._first_overlap(touching))

    def test_a_1x1_face_can_be_lightened(self):
        """With no holes competing for the face, even a 1" face has a band worth cutting -
        which was impossible while holes and pockets shared a face."""
        pattern = tube_patterns.generate(1.0, 24.0, TOOL, mode='lightening')
        self.assertTrue(pattern['pockets'])

    def test_pockets_dropped_when_the_tool_cannot_clear_them(self):
        """A cutter wider than the triangle's inscribed circle cannot clear the corner.
        Emitting the pocket anyway would leave the post-processor to silently skip or
        mangle it, so the pattern drops it and says so."""
        pattern = tube_patterns.generate(2.0, 24.0, tool_diameter=1.25, mode='lightening')
        self.assertEqual(pattern['pockets'], [])
        self.assertTrue(any('does not fit' in w for w in pattern['warnings']))

    def test_holes_and_pockets_are_mutually_exclusive(self):
        """The core rule: a drilled face has no room to lighten, and a trussed face has
        nothing solid left to bolt through."""
        holes = tube_patterns.generate(2.0, 24.0, TOOL, mode='holes')
        light = tube_patterns.generate(2.0, 24.0, TOOL, mode='lightening')
        self.assertTrue(holes['circles']);  self.assertEqual(holes['pockets'], [])
        self.assertTrue(light['pockets']);  self.assertEqual(light['circles'], [])

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            tube_patterns.generate(2.0, 24.0, TOOL, mode='both')


class TestLoadTubePattern(unittest.TestCase):
    """The pattern reaching the post-processor through the same door a DXF uses."""

    def _pp(self, face_width=2.0, length=12.0, mode='holes'):
        # A drilled pattern REQUIRES the tool to be the drill; load_tube_pattern refuses
        # anything else, which is the point of the test below.
        tool = tube_patterns.HOLE_DIAMETER if mode == 'holes' else TOOL
        pp = FRCPostProcessor(0.0625, tool)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(face_width, length, mode=mode)
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
        self.assertTrue(self._pp(mode='lightening').pockets)

    def test_a_1x1_loads_with_holes_and_no_pockets(self):
        pp = self._pp(face_width=1.0)
        self.assertTrue(pp.holes)
        self.assertEqual(pp.pockets, [])

    def test_holes_are_drilled_not_milled(self):
        """The whole point of sizing the tool to the hole: classify_holes must mark every
        hole for a straight peck, because a twist drill cannot be fed sideways."""
        pp = self._pp()
        self.assertTrue(pp.holes)
        for hole in pp.holes:
            self.assertTrue(hole['needs_peck_drill'],
                            'hole would be milled with a helical entry, not drilled')

    def test_a_hole_pattern_refuses_a_milling_cutter(self):
        """An end mill narrower than the hole would cut each hole out sideways. Refused
        rather than silently milled - that is the exact bug class this project shipped
        before."""
        pp = FRCPostProcessor(0.0625, 0.157)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        with self.assertRaises(ValueError) as caught:
            pp.load_tube_pattern(2.0, 12.0, mode='holes')
        self.assertIn('twist drill', str(caught.exception))


class TestTubeGCodeFormatting(unittest.TestCase):
    """The formatting rules, checked against a TUBE program.

    These same rules are covered elsewhere against a plate program. Tube output goes
    through different code that emits its own header, setup and phase comments, and that
    code had never been checked - which is exactly how bracketed comments survived there.
    """

    @classmethod
    def setUpClass(cls):
        pp = FRCPostProcessor(0.0625, tube_patterns.HOLE_DIAMETER)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(2.0, 12.0, mode='holes')
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
        from frc_cam_gui_app import app, limiter
        app.config['TESTING'] = True
        # /process allows 10 requests a minute. That is right for the deployed app and
        # wrong here: this class makes more than that, and the limiter's 429 surfaces as
        # a KeyError on the response body rather than as anything that reads like rate
        # limiting - which cost a while to spot the first time.
        limiter.enabled = False
        cls.client = app.test_client()

    def _post(self, **overrides):
        data = {'material': 'aluminum_tube', 'tube_pattern': 'holes',
                'tube_size': '2x1-flat', 'tube_pattern_length': '24',
                'tube_height': '1.0',
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

    def test_the_two_modes_cut_different_things(self):
        """Counted by TOOLPATHS, not by the word 'pocket' - the tube header says
        'machining holes and pockets only' whether or not any pocket exists."""
        holes = self._post(tube_pattern='holes').get_json()['gcode']
        light = self._post(tube_pattern='lightening').get_json()['gcode']
        pocket_marker, drill_marker = 'Position at pocket center', 'Peck drill straight down'
        self.assertEqual(holes.count(pocket_marker), 0)
        self.assertGreater(holes.count(drill_marker), 0)
        self.assertGreater(light.count(pocket_marker), 0)
        self.assertEqual(light.count(drill_marker), 0)

    def test_a_hole_job_reports_the_drill_in_its_header(self):
        gcode = self._post(tube_pattern='holes').get_json()['gcode']
        self.assertIn('twist drill', gcode)

    def test_the_response_carries_geometry_for_the_cad_preview(self):
        """The viewer draws the tube itself, which it cannot do from G-code alone -
        nothing in a toolpath distinguishes a hole from a circular pocket."""
        preview = self._post(tube_pattern='holes').get_json()['tube_preview']
        self.assertEqual(preview['face_width'], 2.0)
        self.assertEqual(preview['length'], 24.0)
        self.assertTrue(preview['holes'])
        self.assertEqual(preview['pockets'], [])
        light = self._post(tube_pattern='lightening').get_json()['tube_preview']
        self.assertTrue(light['pockets'])
        self.assertEqual(light['holes'], [])

    def test_an_unknown_pattern_is_refused(self):
        response = self._post(tube_pattern='swiss-cheese')
        self.assertEqual(response.status_code, 400)

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
