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
import re
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
        self.assertTrue(any('to helix into' in w or 'nothing to clear' in w
                            for w in pattern['warnings']), pattern['warnings'])

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
        # Two programs: a drilled one (no facing - squaring with a drill in the spindle
        # is refused) and a milled one that DOES square and cut to length, so the header,
        # facing and cut-off comments are covered by the rules too.
        drilled = FRCPostProcessor(0.0625, tube_patterns.HOLE_DIAMETER)
        drilled.apply_material_preset('aluminum_tube')
        drilled.tube_height = 1.0
        drilled.load_tube_pattern(2.0, 12.0, mode='holes')
        r1 = drilled.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=False, cut_to_length=False,
            tube_width=2.0, tube_length=12.0)
        assert r1.success, r1.errors

        milled = FRCPostProcessor(0.0625, TOOL)
        milled.apply_material_preset('aluminum_tube')
        milled.tube_height = 1.0
        milled.load_tube_pattern(2.0, 12.0, mode='lightening')
        r2 = milled.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=True, cut_to_length=True,
            tube_width=2.0, tube_length=12.0)
        assert r2.success, r2.errors

        cls.gcode = r1.gcode + '\n' + r2.gcode
        cls.lines = cls.gcode.split('\n')

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


class TestAuditFindings(unittest.TestCase):
    """Regressions for bugs found by auditing the generated programs.

    Every one of these produced a program that passed the whole suite and the G-code
    audit while being physically wrong. They are the reason this class exists.
    """

    def _drill_pp(self, wall=0.0625, height=1.0):
        pp = FRCPostProcessor(wall, tube_patterns.HOLE_DIAMETER)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = height
        return pp

    def test_a_drilled_hole_goes_fully_through_the_wall(self):
        """A twist drill cuts a cone. Stopping the TIP at the wall bottom leaves a
        pinhole: a 0.201" hole through 1/16" wall exited at 0.027", which no #10 screw
        passes - and passing a #10 is the entire purpose of the pattern."""
        wall = 0.0625
        pp = self._drill_pp(wall=wall)
        pp.load_tube_pattern(2.0, 12.0, mode='holes')
        gcode = pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=False, cut_to_length=False,
            tube_width=2.0, tube_length=12.0).gcode
        tip = min(float(m) for m in re.findall(r'G1 Z([-\d.]+) F[\d.]+\s*;\s*Peck', gcode))
        point = FRCPostProcessor.drill_point_length(tube_patterns.HOLE_DIAMETER)
        wall_bottom = 1.0 - wall
        self.assertLessEqual(tip + point, wall_bottom + 1e-6,
                             'drill point never clears the wall - the exit is a pinhole')

    def test_a_drill_is_never_asked_to_mill(self):
        """Squaring and cut-to-length feed the tool sideways, full width. With a drill in
        the spindle and no tool change in the program, that snapped the drill."""
        for square, cut in ((True, False), (False, True), (True, True)):
            pp = self._drill_pp()
            pp.load_tube_pattern(2.0, 12.0, mode='holes')
            result = pp.generate_tube_pattern_gcode(
                tube_height=1.0, square_end=square, cut_to_length=cut,
                tube_width=2.0, tube_length=12.0)
            self.assertFalse(result.success,
                             f'square_end={square} cut_to_length={cut} was allowed')
            self.assertIn('milling operations', ' '.join(result.errors))

    def test_no_lateral_feed_while_the_drill_is_in_metal(self):
        """The property that matters, asserted directly on the output rather than
        inferred from which branch generated it."""
        pp = self._drill_pp()
        pp.load_tube_pattern(2.0, 12.0, mode='holes')
        gcode = pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=False, cut_to_length=False,
            tube_width=2.0, tube_length=12.0).gcode
        top = 1.0
        x = y = z = None
        for line in gcode.split('\n'):
            code = line.split(';')[0].split('(')[0].strip()
            if not code or not re.match(r'G0?[0-3]\b', code):
                continue
            nx = ny = nz = None
            for tok in code.split():
                if tok.startswith('X'):
                    nx = float(tok[1:])
                elif tok.startswith('Y'):
                    ny = float(tok[1:])
                elif tok.startswith('Z'):
                    nz = float(tok[1:])
            feed = code.split()[0] in ('G1', 'G01', 'G2', 'G02', 'G3', 'G03')
            moved = (nx is not None and nx != x) or (ny is not None and ny != y)
            if moved and feed and z is not None and z < top - 1e-6:
                self.fail(f'drill fed sideways at Z={z}: {line.strip()}')
            x, y, z = (nx if nx is not None else x,
                       ny if ny is not None else y,
                       nz if nz is not None else z)

    def test_cut_to_length_without_a_length_is_refused(self):
        pp = FRCPostProcessor(0.0625, TOOL)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(2.0, 12.0, mode='lightening')
        result = pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=False, cut_to_length=True,
            tube_width=2.0, tube_length=None)
        self.assertFalse(result.success)
        self.assertIn('tube length', ' '.join(result.errors).lower())

    def test_the_program_lifts_after_every_pause(self):
        """The operator has just had their hands in the envelope and may have jogged Z.
        Resuming into a lateral rapid at that height drags the tool over the part."""
        pp = FRCPostProcessor(0.0625, TOOL)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(2.0, 12.0, mode='lightening')
        lines = pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=False, cut_to_length=False,
            tube_width=2.0, tube_length=12.0).gcode.split('\n')
        for i, line in enumerate(lines):
            if not line.startswith('M0'):
                continue
            for follow in lines[i:]:
                code = follow.split(';')[0].strip()
                if not re.match(r'G0?[0-3]\b', code):
                    continue
                toks = code.split()
                if any(t[:1] in ('X', 'Y') for t in toks) and not any(t[:1] == 'Z' for t in toks):
                    self.fail(f'XY move after pause before any retract: {follow.strip()}')
                break

    def test_metric_jobs_are_refused_rather_than_silently_wrong(self):
        """Every constant here is inches and the tube program hard-codes G20, so a metric
        run emitted inch-mode G-code holding millimetre numbers: a 610 mm tube became a
        610 INCH tube and the Z offset went below the jig."""
        pp = FRCPostProcessor(1.6, tube_patterns.HOLE_DIAMETER, units='mm')
        pp.tube_height = 25.4
        with self.assertRaises(ValueError) as caught:
            pp.load_tube_pattern(50.8, 610.0, mode='holes')
        self.assertIn('inch-only', str(caught.exception))

    def test_a_stale_error_does_not_condemn_the_next_pattern(self):
        pp = FRCPostProcessor(0.0625, 0.675)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(1.4, 12.0, mode='lightening')   # tool too big; may error
        pp.errors.append('a stale error from the previous load')
        pp2 = FRCPostProcessor(0.0625, tube_patterns.HOLE_DIAMETER)
        pp2.apply_material_preset('aluminum_tube')
        pp2.tube_height = 1.0
        pp2.errors.append('left over')
        pp2.load_tube_pattern(2.0, 12.0, mode='holes')
        self.assertEqual(pp2.errors, [], 'load_tube_pattern must clear stale errors')

    def test_a_pocket_the_tool_cannot_enter_is_dropped(self):
        """Sized against what the cutter SWEEPS helixing in, not its bare radius. A 3/8"
        tool on a 1" face removed metal from X 0.132 to X 0.788 of a pocket bounded at
        0.25..0.75 - through the edge margin protecting the extrusion corner."""
        pattern = tube_patterns.generate(1.0, 24.0, 0.375, mode='lightening')
        self.assertEqual(pattern['pockets'], [])
        self.assertTrue(pattern['warnings'])

    def test_nan_and_infinity_are_refused(self):
        for bad in (float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                tube_patterns.generate(2.0, bad, TOOL, mode='holes')
            with self.assertRaises(ValueError):
                tube_patterns.generate(bad, 24.0, TOOL, mode='holes')

    def test_three_rows_only_where_the_web_survives(self):
        """1.5" was a round number, not a derived one: it put three rows on a 1.5" face
        with 0.049" of metal between them, a knife edge in 1/16" wall."""
        self.assertEqual(len(tube_patterns.hole_rows(1.5)), 1)
        self.assertEqual(len(tube_patterns.hole_rows(2.0)), 3)
        rows = tube_patterns.hole_rows(2.0)
        self.assertGreaterEqual((rows[1] - rows[0]) - tube_patterns.HOLE_DIAMETER,
                                tube_patterns.MIN_WEB - 1e-9)

    def test_a_face_too_narrow_for_a_hole_gets_none(self):
        pattern = tube_patterns.generate(0.15, 6.0, TOOL, mode='holes')
        self.assertEqual(pattern['circles'], [])
        self.assertTrue(any('too narrow' in w for w in pattern['warnings']))


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

    def test_a_tube_longer_than_the_machine_is_refused(self):
        """The one that needs no hostile input: 24" is the most ordinary FRC tube length
        and it is the placeholder in the field, but it is longer than the Y travel of the
        machine this was written for. It used to return 200 with a 3D preview."""
        response = self._post(tube_pattern_length='2000')
        self.assertEqual(response.status_code, 400)
        self.assertIn('does not fit the machine', response.get_json()['error'])

    def test_an_unknown_tube_size_is_refused_not_guessed(self):
        """_parse_tube_size answers 1x1 for anything it does not recognise, so a typo
        became 23 holes down the centre of a 2" face instead of 69 in three rows."""
        for bad in ('2\u00d71', 'A' * 300, '', '3x3'):
            response = self._post(tube_size=bad)
            self.assertEqual(response.status_code, 400, f'{bad!r} was accepted')

    def test_physically_impossible_dimensions_are_refused(self):
        """Each of these produced a successful program with wrong Z: a negative height
        put the whole program below the work zero - including its 'safe' retract - and a
        wall thicker than the tube pecked inches through the jig."""
        for field, value in (('tube_height', '-1'), ('tube_height', '0'),
                             ('tube_height', 'nan'), ('thickness', '5'),
                             ('thickness', '0'), ('thickness', '-0.1')):
            response = self._post(**{field: value})
            self.assertEqual(response.status_code, 400,
                             f'{field}={value} was accepted')

    def test_non_finite_length_is_refused(self):
        for value in ('nan', '1e309', 'inf'):
            self.assertEqual(self._post(tube_pattern_length=value).status_code, 400)

    def test_the_response_reports_the_tool_that_will_be_loaded(self):
        """The drill is substituted server-side; echoing the user's end mill back had the
        summary chip and the program header disagree about what to put in the spindle."""
        body = self._post(tube_pattern='holes', tool_diameter='0.25').get_json()
        self.assertAlmostEqual(body['parameters']['tool_diameter'],
                               tube_patterns.HOLE_DIAMETER, places=6)
        self.assertIn('twist drill', body['gcode'])

    def test_the_standing_orientation_uses_its_real_height(self):
        """2x1 on its 1" face is a 2" TALL tube. Keeping the form's 1.0 put the safe-Z
        retract 0.75" inside the tube and drilled every hole an inch below the wall."""
        gcode = self._post(tube_size='2x1-standing').get_json()['gcode']
        self.assertIn('( Tube height: 2.000" )', gcode)

    def test_an_ignored_dxf_is_reported(self):
        """Silently discarding an attached file meant downloading a program named after
        a part that contained none of it."""
        import io as _io
        data = {'material': 'aluminum_tube', 'tube_pattern': 'holes',
                'tube_size': '2x1-flat', 'tube_pattern_length': '12',
                'tube_height': '1.0', 'thickness': '0.0625',
                'tool_diameter': '0.157', 'square_end': '0', 'cut_to_length': '0',
                'file': (_io.BytesIO(b'0\nSECTION\n'), 'my_bracket.dxf')}
        body = self.client.post('/process', data=data,
                                content_type='multipart/form-data').get_json()
        self.assertTrue(any('not used' in w for w in body.get('warnings', [])),
                        body.get('warnings'))


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


class TestPostProcessorGuards(unittest.TestCase):
    """Inputs that used to hang or crash rather than be refused."""

    def test_a_non_positive_tool_is_refused_at_construction(self):
        """A negative tool made the pocket-clearing loop step OUTWARD every pass, so
        neither exit condition could fire and the request hung inside shapely."""
        for bad in (0, -0.25, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                FRCPostProcessor(0.25, bad)

    def test_a_non_positive_thickness_is_refused(self):
        for bad in (0, -0.1, float('nan')):
            with self.assertRaises(ValueError):
                FRCPostProcessor(bad, 0.157)


if __name__ == '__main__':
    unittest.main()
