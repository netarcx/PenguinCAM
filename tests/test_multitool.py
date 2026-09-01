"""Multi-tool operations: the model, the ordering, the chamfer geometry, and the routes.

The multi-tool path deliberately reuses the single-tool generators (one post-processor per
operation, each already correct for its own tool), so these tests concentrate on what is
genuinely new: scoping features to an operation, ordering operations across parts without
disturbing any part's own sequence, holding tabs until the end, the V-tool chamfer
geometry, and the safety rules that every emitted comment must still obey.
"""

import io
import json
import math
import os
import re
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import drill_sizes
import feeds_speeds
import tooling
from tooling import MultiToolJob, Operation, PartOps, Tool, ToolingError
from frc_cam_postprocessor import (
    FRCPostProcessor, build_output_filename, build_resume_programs, sanitize_comment,
)
from team_config import TeamConfig


# ------------------------------------------------------------------ fixtures

def make_plate_dxf(path=None):
    """A 6x4 plate: four small (0.196") holes, one 0.750" bore, one rectangular pocket."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True)
    for (x, y) in [(1, 1), (5, 1), (1, 3), (5, 3)]:
        msp.add_circle((x, y), 0.098)
    msp.add_circle((3, 2), 0.375)
    msp.add_lwpolyline([(2, 2.6), (4, 2.6), (4, 3.4), (2, 3.4)], close=True)
    path = path or tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


def three_tools():
    return [
        Tool(1, '1/8 in 1-flute endmill', 0.125, 1),
        Tool(2, '1/4 in 1-flute endmill', 0.250, 1),
        Tool(3, '1/2 in 90 deg V-bit', 0.500, 2, type='vbit', included_angle=90.0),
    ]


def standard_ops():
    return [
        Operation('holes', 1, 'Small holes', scope={'max_diameter': 0.4}),
        Operation('holes', 2, 'Big bore', scope={'min_diameter': 0.4}),
        Operation('pockets', 2, 'Pockets'),
        Operation('perimeter', 2),
        Operation('chamfer', 3, 'Edge break',
                  scope={'targets': ['perimeter'], 'width': 0.02}),
    ]


def build_job(parts=None, tools=None, **kwargs):
    """A one-plate plywood job. Any MultiToolJob field can be overridden by keyword,
    including material and thickness."""
    dxf = make_plate_dxf()
    parts = parts or [PartOps(dxf_path=dxf, name='plate', operations=standard_ops())]
    fields = {'material': 'plywood', 'thickness': 0.25,
              'tools': tools or three_tools(), 'parts': parts}
    fields.update(kwargs)
    return MultiToolJob(**fields)


def generate(job, **kwargs):
    """Run a job with the post-processor's chatty progress output suppressed."""
    kwargs.setdefault('timestamp', '2026-08-20 12:00:00')
    with redirect_stdout(io.StringIO()):
        return tooling.generate_multitool_job(job, **kwargs)


# --------------------------------------------------------------------- model

class TestToolValidation(unittest.TestCase):
    """A bad tool must be rejected with a sentence the student can act on, not a
    traceback three layers down in the post-processor."""

    def test_rejects_impossible_tools(self):
        with self.assertRaises(ToolingError):
            Tool(1, 'zero', 0.0)
        with self.assertRaises(ToolingError):
            Tool(0, 'slot zero', 0.25)
        with self.assertRaises(ToolingError):
            Tool(1, 'no flutes', 0.25, flutes=0)
        with self.assertRaises(ToolingError):
            Tool(1, 'strange', 0.25, type='laser')
        with self.assertRaises(ToolingError):
            Tool(1, 'flat V', 0.25, type='vbit', included_angle=180)

    def test_duplicate_slots_rejected(self):
        with self.assertRaises(ToolingError) as ctx:
            build_job(tools=[Tool(1, 'a', 0.125), Tool(1, 'b', 0.25)])
        self.assertIn('T1', str(ctx.exception))

    def test_operation_must_reference_a_loaded_tool(self):
        dxf = make_plate_dxf()
        with self.assertRaises(ToolingError) as ctx:
            MultiToolJob(material='plywood', thickness=0.25, tools=[Tool(1, 'a', 0.125)],
                         parts=[PartOps(dxf_path=dxf, name='p',
                                        operations=[Operation('perimeter', 7)])])
        self.assertIn('T7', str(ctx.exception))

    def test_unknown_operation_type_rejected(self):
        with self.assertRaises(ToolingError):
            Operation('engrave', 1)

    def test_used_tools_excludes_the_unused(self):
        """A tool listed but never cut with must not appear in the header - the operator
        would go hunting for a cutter the program never asks for."""
        job = build_job(parts=[PartOps(dxf_path=make_plate_dxf(), name='p',
                                       operations=[Operation('perimeter', 2)])])
        self.assertEqual([t.slot for t in job.used_tools], [2])


class TestDrillScopeValidation(unittest.TestCase):
    """Numbers in `scope` go straight into Z arithmetic. Every one of them is checked at
    the door, because past it there is nothing between a typo and a commanded move.

    Both of these were verified as generating a "successful" program: `point_angle: 5`
    put the last peck at G1 Z-2.2239, 2.2 inches below the sacrifice board, and
    `spot_depth: 100` commanded a feed move 99.75 inches down.
    """

    def test_point_angle_must_be_a_real_drill_point(self):
        for angle in (5, 0, -118, 200, float('nan'), float('inf')):
            with self.subTest(angle=angle):
                with self.assertRaises(ToolingError) as ctx:
                    Operation('holes', 1, scope={'point_angle': angle})
                message = str(ctx.exception)
                self.assertIn('point_angle', message)
                self.assertIn('118', message)      # says what a normal value looks like

    def test_ordinary_point_angles_are_accepted(self):
        for angle in (118, 135, 90, 60, 150):
            with self.subTest(angle=angle):
                op = Operation('holes', 1, scope={'point_angle': angle})
                self.assertAlmostEqual(op.drill_point_angle, float(angle))

    def test_spot_depth_must_be_positive_finite_and_shallow(self):
        for depth in (100, 0, -1, float('nan'), float('inf'), 0.5):
            with self.subTest(depth=depth):
                with self.assertRaises(ToolingError) as ctx:
                    Operation('holes', 1, scope={'purpose': 'spot', 'spot_depth': depth})
                self.assertIn('spot_depth', str(ctx.exception))

    def test_spot_depth_cannot_exceed_the_stock(self):
        with self.assertRaises(ToolingError) as ctx:
            build_job(thickness=0.06,
                      tools=[Tool(1, 'centre', 0.125, 2, type='drill'),
                             Tool(2, 'em', 0.25, 2)],
                      parts=[PartOps(dxf_path=make_plate_dxf(), name='p', operations=[
                          Operation('holes', 1, scope={'purpose': 'spot',
                                                       'spot_depth': 0.2}),
                          Operation('perimeter', 2)])])
        self.assertIn('spot_depth', str(ctx.exception))

    def test_scope_size_ranges_are_validated(self):
        for key in ('min_diameter', 'max_diameter'):
            for bad in (float('nan'), -1, 'wide'):
                with self.subTest(key=key, bad=bad):
                    with self.assertRaises(ToolingError):
                        Operation('holes', 1, scope={key: bad})
        for key in ('min_area', 'max_area'):
            for bad in (float('inf'), -0.5):
                with self.subTest(key=key, bad=bad):
                    with self.assertRaises(ToolingError):
                        Operation('pockets', 1, scope={key: bad})

    def test_size_tolerance_is_validated_at_the_door(self):
        with self.assertRaises(ToolingError):
            Operation('holes', 1, scope={'size_tolerance': float('nan')})
        with self.assertRaises(ToolingError):
            Operation('holes', 1, scope={'size_tolerance': -0.01})

    def test_tool_fields_reject_nan(self):
        for field, value in (('diameter', float('nan')), ('flutes', float('nan')),
                             ('included_angle', float('nan'))):
            with self.subTest(field=field):
                with self.assertRaises(ToolingError):
                    Tool.from_dict({'slot': 1, 'name': 'x', 'diameter': 0.25,
                                    field: value})

    def test_bad_scope_from_an_ops_file_is_a_clean_refusal(self):
        """`json.loads` accepts a bare NaN literal, so this is a real posted payload."""
        data = json.loads('{"op_type": "holes", "tool_slot": 1, '
                          '"scope": {"spot_depth": NaN}}')
        with self.assertRaises(ToolingError):
            Operation.from_dict(data)


class TestOperationOrdering(unittest.TestCase):
    """order_operations groups work by tool but must never reorder a part's own list."""

    def _parts(self, *op_slot_lists):
        return [PartOps(dxf_path='x.dxf', name=f'p{i}',
                        operations=[Operation('holes', slot) for slot in slots])
                for i, slots in enumerate(op_slot_lists)]

    def test_groups_identical_plans_across_parts(self):
        parts = self._parts([1, 2, 3], [1, 2, 3])
        order = tooling.order_operations(parts)
        slots = [parts[p].operations[o].tool_slot for p, o in order]
        self.assertEqual(slots, [1, 1, 2, 2, 3, 3])   # 2 changes, not 5

    def test_preserves_each_parts_own_sequence(self):
        parts = self._parts([1, 2, 1], [1])
        order = tooling.order_operations(parts)
        part0 = [o for p, o in order if p == 0]
        self.assertEqual(part0, [0, 1, 2])            # never rearranged, even to save a swap

    def test_every_operation_is_emitted_exactly_once(self):
        parts = self._parts([2, 1, 2], [1, 1], [3])
        order = tooling.order_operations(parts)
        self.assertEqual(sorted(order), sorted([(0, 0), (0, 1), (0, 2),
                                                (1, 0), (1, 1), (2, 0)]))


class TestScopeSelection(unittest.TestCase):
    def setUp(self):
        self.job = build_job()
        with redirect_stdout(io.StringIO()):
            self.features = tooling.survey_part(self.job, self.job.parts[0])

    def test_survey_finds_every_hole_and_pocket(self):
        self.assertEqual(len(self.features['holes']), 5)
        self.assertEqual(self.features['hole_sizes'], [0.196, 0.75])
        self.assertEqual(len(self.features['pockets']), 1)
        self.assertTrue(self.features['has_perimeter'])
        # The 2.0 x 0.8 rectangular pocket admits a 0.8 in circle - `inscribed` is the
        # largest tool that can machine the pocket, and the standard setups split
        # pockets between the 1/8 and 1/4 end mills with it.
        self.assertAlmostEqual(self.features['pockets'][0]['inscribed'], 0.8, delta=0.01)

    def test_diameter_range_splits_the_holes(self):
        small = tooling.selected_hole_keys(self.features, {'max_diameter': 0.4})
        large = tooling.selected_hole_keys(self.features, {'min_diameter': 0.4})
        self.assertEqual(len(small), 4)
        self.assertEqual(len(large), 1)
        self.assertFalse(small & large)

    def test_empty_scope_selects_everything(self):
        self.assertEqual(len(tooling.selected_hole_keys(self.features, {})), 5)

    def test_explicit_indices_win_over_ranges(self):
        keys = tooling.selected_hole_keys(self.features, {'indices': [0], 'max_diameter': 0.001})
        self.assertEqual(len(keys), 1)

    def test_uncovered_holes_are_reported_not_silently_left(self):
        """A hole no operation claims would be found missing only once the part is off
        the machine, so it has to fail generation."""
        job = build_job(parts=[PartOps(
            dxf_path=make_plate_dxf(), name='plate',
            operations=[Operation('holes', 2, scope={'min_diameter': 0.4}),
                        Operation('perimeter', 2)])])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('not cut by any operation' in e for e in result.errors),
                        result.errors)

    def test_holes_claimed_twice_are_reported(self):
        job = build_job(parts=[PartOps(
            dxf_path=make_plate_dxf(), name='plate',
            operations=[Operation('holes', 1), Operation('holes', 2)])])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('more than one operation' in e for e in result.errors),
                        result.errors)

    def test_out_of_scope_holes_do_not_trip_the_too_small_check(self):
        """The 0.196" holes are smaller than the 1/4" cutter. The operation that uses that
        cutter is scoped away from them, so it must not reject them - the 1/8" operation
        is the one that drills them."""
        result = generate(build_job())
        self.assertTrue(result.success, result.errors)


class TestUndersizedHoleSpotting(unittest.TestCase):
    """A hole smaller than every tool in the job can still be centre-marked by a spot
    operation for hand drilling, so the survey must LIST it (flagged) rather than reject
    it - the rejection blocked the spot-then-drill-press workflow outright and hid the
    hole from the scope pickers that would have set it up."""

    @staticmethod
    def make_dxf():
        """A 3x2 plate with a 0.089" hole (smaller than every tool below) and a 0.196"."""
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (3, 0), (3, 2), (0, 2)], close=True)
        msp.add_circle((1, 1), 0.089 / 2)
        msp.add_circle((2, 1), 0.098)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        return path

    @staticmethod
    def tools():
        # The user's real setup: nothing here can make a 0.089" hole.
        return [Tool(1, '1/8 in endmill', 0.125, 1),
                Tool(2, '5/32 centre drill', 0.15625, 2, type='drill')]

    def job(self, operations):
        return MultiToolJob(material='plywood', thickness=0.25, tools=self.tools(),
                            parts=[PartOps(dxf_path=self.make_dxf(), name='plate',
                                           operations=operations)])

    def test_survey_lists_the_undersized_hole_flagged(self):
        job = self.job([Operation('perimeter', 1)])
        with redirect_stdout(io.StringIO()):
            features = tooling.survey_part(job, job.parts[0])
        by_dia = {h['diameter']: h for h in features['holes']}
        self.assertEqual(sorted(by_dia), [0.089, 0.196])
        self.assertTrue(by_dia[0.089]['too_small'])
        self.assertFalse(by_dia[0.196]['too_small'])
        self.assertFalse(any('too small' in e for e in features['errors']),
                         features['errors'])

    def test_spot_operation_may_cover_it(self):
        result = generate(self.job([
            Operation('holes', 2, 'Spot tiny', scope={'max_diameter': 0.1,
                                                      'purpose': 'spot'}),
            Operation('holes', 1, 'Mill holes', scope={'min_diameter': 0.1}),
            Operation('perimeter', 1)]))
        self.assertTrue(result.success, result.errors)
        self.assertIn('CENTRE DRILLING', result.gcode)
        self.assertTrue(any('spotted but never drilled' in w for w in result.warnings),
                        result.warnings)

    def test_left_uncovered_it_is_an_error_naming_the_fixes(self):
        result = generate(self.job([
            Operation('holes', 1, 'Mill holes', scope={'min_diameter': 0.1}),
            Operation('perimeter', 1)]))
        self.assertFalse(result.success)
        self.assertTrue(any('smaller than every tool' in e for e in result.errors),
                        result.errors)

    def test_claimed_by_an_end_mill_it_still_fails_as_too_small(self):
        result = generate(self.job([Operation('holes', 1, 'All holes'),
                                    Operation('perimeter', 1)]))
        self.assertFalse(result.success)
        self.assertTrue(any('too small' in e for e in result.errors), result.errors)

    def test_suggester_spots_what_nothing_can_make(self):
        """A size with no standard drill that is also smaller than the mill must come
        back as a spot operation, not swept into the bore range - that proposed a plan
        that failed the instant it ran."""
        plan = tooling.suggest_tooling(
            {'hole_sizes': [0.004], 'pockets': [], 'has_perimeter': True},
            mill_diameter=0.25)
        spots = [o for o in plan['operations']
                 if o.scope.get('purpose') == drill_sizes.PURPOSE_SPOT]
        self.assertEqual(len(spots), 1)
        self.assertFalse([o for o in plan['operations'] if o.name == 'Bore large holes'])
        self.assertTrue(any('centre-marked' in n for n in plan['notes']), plan['notes'])


class TestAluminumEntryChipload(unittest.TestCase):
    """Entry moves in aluminum must never rub. A ramp or helix commanded below the
    minimum chipload at the program's RPM heats instead of cutting, welds chips to the
    flutes, and shatters the tool - a real 1/4 in end mill died to a 9 ipm entry at
    12000 RPM after a well-meaning "slow the ramp down" derate. Slower is only safer
    in materials that do not seize."""

    @staticmethod
    def make_dxf():
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (5, 0), (5, 3), (0, 3)], close=True)
        msp.add_lwpolyline([(1.5, 1.0), (3.5, 1.0), (3.5, 2.0), (1.5, 2.0)], close=True)
        msp.add_circle((4.2, 2.5), 0.375)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        return path

    def assert_entries_make_a_chip(self, diameter, flutes):
        job = MultiToolJob(
            material='aluminum', thickness=0.25, machine_id='omio_x8',
            tools=[Tool(1, 'mill', diameter, flutes)],
            parts=[PartOps(dxf_path=self.make_dxf(), name='p', operations=[
                Operation('holes', 1), Operation('pockets', 1),
                Operation('perimeter', 1)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        minimum = feeds_speeds.MATERIALS['aluminum_6063']['chipload_min']
        # Each operation commands its own S word, so every entry move is judged
        # against the spindle speed actually active on its line.
        rpm, checked = None, 0
        for line in result.gcode.splitlines():
            s = re.search(r'S(\d+)', line)
            if s:
                rpm = int(s.group(1))
            if rpm and ('Ramp segment' in line or 'Helical' in line):
                for f in re.findall(r'F([\d.]+)', line):
                    feed, floor = float(f), rpm * flutes * minimum
                    checked += 1
                    self.assertGreaterEqual(
                        feed, floor - 0.1,
                        f'{line.strip()!r} commands {feed} ipm = '
                        f'{feed / (rpm * flutes):.5f} in/tooth at {rpm} RPM - below '
                        f'the {minimum} rubbing floor. This is how end mills shatter.')
        self.assertGreater(checked, 0)

    def test_quarter_inch_entries_make_a_chip(self):
        self.assert_entries_make_a_chip(0.25, 1)

    def test_eighth_inch_entries_make_a_chip(self):
        self.assert_entries_make_a_chip(0.125, 1)

    def test_two_flute_entries_make_a_chip(self):
        # A 2-flute needs TWICE the feed to keep the same per-tooth chip, so a ramp
        # that was safe for a single flute rubs with two. The floor scales by flutes.
        self.assert_entries_make_a_chip(0.25, 2)
        self.assert_entries_make_a_chip(0.125, 2)


class TestFeeds(unittest.TestCase):
    def test_feeds_track_the_tool_not_the_material_preset(self):
        small, _ = tooling.compute_tool_feeds(Tool(1, 'small', 0.125, 1), 'plywood', None, 'pockets')
        large, _ = tooling.compute_tool_feeds(Tool(2, 'large', 0.375, 1), 'plywood', None, 'pockets')
        self.assertGreater(large['feed_xy'], small['feed_xy'])
        self.assertGreater(large['stepover'], small['stepover'])

    def test_flutes_past_the_evacuation_limit_buy_no_feed_in_aluminum(self):
        """A 4-flute in 6061 was fed at 4x the per-tooth rate - 172 IPM wanted, 150
        after the machine clamp - on the theory that every flute takes a healthy chip.
        In a slot in gummy aluminum the extra flutes cannot clear their chips: they
        pack, weld, and snap the tool (this broke real 1/8 in cutters). Feed must stop
        scaling at the material's evacuation limit, and the operator must be told."""
        two = feeds_speeds.calculate_feeds('omio_x8', 'aluminum_6061',
                                           {'diameter': 0.125, 'flutes': 2}, 'profile')
        four = feeds_speeds.calculate_feeds('omio_x8', 'aluminum_6061',
                                            {'diameter': 0.125, 'flutes': 4}, 'profile')
        self.assertEqual(four['feed_xy'], two['feed_xy'])
        self.assertLess(four['feed_xy'], 100.0)   # nowhere near the old 150 IPM
        self.assertTrue(any('2-flute rate' in w for w in four['warnings']),
                        four['warnings'])
        # With four teeth sharing a two-flute feed, each takes half a chip: the
        # rubbing check must say so and point at the actual fix.
        self.assertTrue(any('below the recommended minimum' in w
                            for w in four['warnings']), four['warnings'])
        self.assertFalse(any('2-flute rate' in w for w in two['warnings']))

    def test_job_level_max_pass_depth_reaches_every_operation(self):
        """The operator's depth-per-pass ceiling: more, shallower passes for fragile
        or multi-flute cutters. Applied after every automatic clamp, clamp-only."""
        import re
        shallow = generate(build_job(max_pass_depth=0.05))
        self.assertTrue(shallow.success, shallow.errors)
        for m in re.finditer(r'passes @ (\d+\.\d+)" each', shallow.gcode):
            self.assertLessEqual(float(m.group(1)), 0.05 + 1e-9, shallow.gcode[:200])
        # A ceiling above the automatic value changes nothing.
        loose = generate(build_job(max_pass_depth=5.0))
        normal = generate(build_job())
        self.assertEqual(loose.gcode, normal.gcode)

    def test_metal_feed_is_anchored_to_the_safe_preset(self):
        """The chipload model may only derate the router's aluminum ceiling."""
        import re
        job = build_job(material='aluminum', thickness=0.125,
                        tools=[Tool(1, '8th', 0.125, 2)],
                        parts=[PartOps(dxf_path=make_bare_dxf(), name='P',
                                       operations=[Operation('perimeter', 1)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        anchor = 30.0 * (0.125 / 0.157) ** feeds_speeds.DIAMETER_EXPONENT
        for feed in set(float(f) for f in
                        re.findall(r'X-?[\d.]+ Y-?[\d.]+ F([\d.]+)', result.gcode)):
            if feed >= 199.0:
                continue                     # traverse moves above the material
            self.assertLessEqual(feed, anchor + 0.1, f'cutting feed {feed}')
        self.assertTrue(any('tested' in w and 'held to' in w for w in result.warnings),
                        result.warnings)
        # Wood keeps the model's numbers - no anchor warning there.
        wood = generate(build_job(tools=[Tool(1, '8th', 0.125, 4)],
                                  parts=[PartOps(dxf_path=make_bare_dxf(), name='P',
                                                 operations=[Operation('perimeter', 1)])]))
        self.assertFalse(any('tested' in w and 'held to' in w for w in wood.warnings),
                         wood.warnings)

    def test_high_flute_aluminum_tool_is_refused(self):
        job = build_job(material='aluminum', thickness=0.125,
                        tools=[Tool(1, 'wrong cutter', 0.125, 4)],
                        parts=[PartOps(dxf_path=make_bare_dxf(), name='P',
                                       operations=[Operation('perimeter', 1)])])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('1- or 2-flute' in e and 'snap' in e for e in result.errors),
                        result.errors)

    def test_flutes_still_scale_feed_in_wood(self):
        """Wood clears chips; the evacuation cap is a property of gummy metals and
        must not slow every plywood job down."""
        one = feeds_speeds.calculate_feeds('omio_x8', 'plywood',
                                           {'diameter': 0.0625, 'flutes': 1}, 'profile')
        two = feeds_speeds.calculate_feeds('omio_x8', 'plywood',
                                           {'diameter': 0.0625, 'flutes': 2}, 'profile')
        self.assertGreater(two['feed_xy'], one['feed_xy'])

    def test_explicit_overrides_win(self):
        pp = FRCPostProcessor(0.25, 0.25)
        pp.apply_material_preset('plywood')
        tool = Tool(1, 'pinned', 0.25, 1, feed_rate=42.0, spindle_speed=12000)
        feeds, _ = tooling.compute_tool_feeds(tool, 'plywood', None, 'perimeter')
        with redirect_stdout(io.StringIO()):
            tooling.apply_tool_feeds(pp, tool, feeds)
        self.assertAlmostEqual(pp.feed_rate, 42.0)
        self.assertEqual(pp.spindle_speed, 12000)

    def test_override_rescales_the_ramp_feed_with_it(self):
        """Ramp and plunge feeds are fractions of the cutting feed; pinning the cutting
        feed without moving them would leave a ramp faster than the cut it feeds into."""
        tool = Tool(1, 'pinned', 0.25, 1, feed_rate=40.0)
        feeds, _ = tooling.compute_tool_feeds(tool, 'plywood', None, 'perimeter')
        pp = FRCPostProcessor(0.25, 0.25)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset('plywood')
            tooling.apply_tool_feeds(pp, tool, feeds)
        self.assertLess(pp.ramp_feed_rate, pp.feed_rate)
        self.assertLess(pp.plunge_rate, pp.feed_rate)

    def test_material_alias_maps_to_the_feeds_model(self):
        self.assertEqual(tooling.resolve_feeds_material('aluminum'), 'aluminum_6063')
        self.assertEqual(tooling.resolve_feeds_material('polycarb'), 'polycarbonate')

    def test_team_defined_material_skips_the_model(self):
        """None means "the team's preset is the answer" - not "use plywood". Quoting
        brass or delrin off wood's chipload model overwrote tested numbers."""
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'M', 'materials': {'something_custom': {'name': 'Custom'}}}}})
        self.assertIsNone(tooling.resolve_feeds_material('something_custom', cfg))

    def test_material_nobody_knows_is_refused(self):
        with self.assertRaises(ToolingError):
            tooling.resolve_feeds_material('something_custom')


class TestChamferGeometry(unittest.TestCase):
    """The V-tool rides centred on the true edge; the only number that matters is how far
    below the material top its tip goes."""

    def test_ninety_degree_vbit_depth_equals_width(self):
        self.assertAlmostEqual(FRCPostProcessor.chamfer_depth(0.02, 90.0), 0.02)

    def test_narrower_vbit_must_go_deeper_for_the_same_break(self):
        self.assertGreater(FRCPostProcessor.chamfer_depth(0.02, 60.0),
                           FRCPostProcessor.chamfer_depth(0.02, 90.0))
        # 60 deg included -> 30 deg half angle -> depth = width / tan(30)
        self.assertAlmostEqual(FRCPostProcessor.chamfer_depth(0.02, 60.0),
                               0.02 / math.tan(math.radians(30)))

    def test_chamfer_cuts_at_the_derived_depth(self):
        pp = FRCPostProcessor(0.25, 0.5)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset('plywood')
        ring = [{'points': [(0, 0), (4, 0), (4, 4), (0, 4)], 'clockwise': True,
                 'label': 'Perimeter', 'min_radius': None}]
        lines = pp._generate_chamfer_gcode(ring, 0.03, 90.0)
        self.assertEqual(pp.errors, [])
        depths = [float(t[1:]) for line in lines for t in line.split()
                  if t.startswith('Z') and line.startswith('G1')]
        self.assertAlmostEqual(min(depths), pp.material_top - 0.03, places=4)
        self.assertGreater(min(depths), 0.0)   # never through the stock

    def test_chamfer_wider_than_the_tool_is_refused(self):
        pp = FRCPostProcessor(0.25, 0.125)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset('plywood')
        ring = [{'points': [(0, 0), (4, 0), (4, 4), (0, 4)], 'clockwise': True,
                 'label': 'Perimeter', 'min_radius': None}]
        with redirect_stdout(io.StringIO()):
            pp._generate_chamfer_gcode(ring, 0.20, 90.0)
        self.assertTrue(any('exceeds what' in e for e in pp.errors), pp.errors)

    def test_chamfer_deeper_than_the_stock_is_refused(self):
        pp = FRCPostProcessor(0.05, 0.5)          # 0.05" stock
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset('plywood')
        ring = [{'points': [(0, 0), (4, 0), (4, 4), (0, 4)], 'clockwise': True,
                 'label': 'Perimeter', 'min_radius': None}]
        with redirect_stdout(io.StringIO()):
            pp._generate_chamfer_gcode(ring, 0.06, 90.0)
        self.assertTrue(any('through' in e for e in pp.errors), pp.errors)

    def test_chamfer_wider_than_a_hole_radius_is_refused(self):
        pp = FRCPostProcessor(0.25, 0.5)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset('plywood')
        ring = [{'points': pp._tessellate_circle(2, 2, 0.05), 'clockwise': False,
                 'label': 'Hole 0.100 in dia', 'min_radius': 0.05}]
        with redirect_stdout(io.StringIO()):
            pp._generate_chamfer_gcode(ring, 0.06, 90.0)
        self.assertTrue(any('tightest radius' in e for e in pp.errors), pp.errors)

    def test_outside_edges_run_clockwise_and_inside_edges_counter_clockwise(self):
        """Climb milling: an outside edge is cut CW, an inside edge CCW."""
        pp = FRCPostProcessor(0.25, 0.5)
        square = [(0, 0), (4, 0), (4, 4), (0, 4)]

        def signed_area(ring):
            return 0.5 * sum(ring[i][0] * ring[(i + 1) % len(ring)][1]
                             - ring[(i + 1) % len(ring)][0] * ring[i][1]
                             for i in range(len(ring)))

        self.assertLess(signed_area(pp._orient_ring(square, True)), 0)     # CW
        self.assertGreater(signed_area(pp._orient_ring(square, False)), 0)  # CCW


def make_necked_dxf(neck=0.300):
    """An H-shaped part: two full-height bars joined by a thin horizontal neck. The waist
    is the only narrow spot, so eroding the whole part does not vanish - it splits."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    lo, hi = 2.0 - neck / 2.0, 2.0 + neck / 2.0
    msp.add_lwpolyline([(0, 0), (2, 0), (2, lo), (4, lo), (4, 0), (6, 0),
                        (6, 4), (4, 4), (4, hi), (2, hi), (2, 4), (0, 4)], close=True)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


def make_bare_dxf(width=6.0, height=4.0):
    """A plain rectangle, so a perimeter-only plan is valid with any single tool."""
    doc = ezdxf.new('R2010')
    doc.modelspace().add_lwpolyline(
        [(0, 0), (width, 0), (width, height), (0, height)], close=True)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


class TestProfileFreesThePart(unittest.TestCase):
    """Multi-tool is the first mode that can schedule work AFTER a profile, so it is the
    first that can leave a part loose on the table under a running cutter. Tabs are what
    make that safe; with tabs off there is no toolpath that rescues it."""

    NO_TABS = TeamConfig({'machining': {'tabs': {'enabled': False}}})

    def test_operation_after_a_tabless_profile_is_refused(self):
        job = build_job(tools=three_tools(), config=self.NO_TABS, parts=[PartOps(
            dxf_path=make_plate_dxf(), name='P',
            operations=[Operation('holes', 1), Operation('perimeter', 2),
                        Operation('chamfer', 3,
                                  scope={'targets': ['perimeter'], 'width': 0.02})])])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('loose on the table' in e for e in result.errors), result.errors)

    def test_tabless_profile_beside_another_part_is_refused(self):
        dxf = make_plate_dxf()
        job = build_job(tools=three_tools(), config=self.NO_TABS, parts=[
            PartOps(dxf_path=dxf, name='a', place_x=0,
                    operations=[Operation('holes', 1), Operation('perimeter', 2)]),
            PartOps(dxf_path=dxf, name='b', place_x=7,
                    operations=[Operation('holes', 1), Operation('perimeter', 2)]),
        ])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('still being machined' in e for e in result.errors), result.errors)

    def test_a_lone_tabless_part_is_still_fine(self):
        """Nothing follows the profile and nothing else is on the sheet, so cutting
        through is exactly what should happen."""
        job = build_job(tools=three_tools(), config=self.NO_TABS, parts=[PartOps(
            dxf_path=make_plate_dxf(), name='P',
            operations=[Operation('holes', 1, scope={'max_diameter': 0.4}),
                        Operation('holes', 2, scope={'min_diameter': 0.4}),
                        Operation('pockets', 2), Operation('perimeter', 2)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)

    def test_the_same_plan_with_tabs_enabled_works(self):
        result = generate(build_job())          # standard_ops: chamfer after the profile
        self.assertTrue(result.success, result.errors)


class TestStepdownClamp(unittest.TestCase):
    """Depth of cut is clamped to the material preset and never raised by the chipload
    model - taking an extra pass costs time, over-committing costs a cutter."""

    def _stepdown(self, material, diameter, flutes):
        pp = FRCPostProcessor(0.25, diameter)
        tool = Tool(1, 'T', diameter, flutes)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset(material)
            preset = pp.max_slotting_depth
            feeds, _ = tooling.compute_tool_feeds(tool, material, None, 'perimeter')
            tooling.apply_tool_feeds(pp, tool, feeds)
        return preset, feeds['slot_stepdown'], pp.max_slotting_depth

    def test_a_big_cutter_never_exceeds_the_tested_preset(self):
        preset, model, applied = self._stepdown('aluminum', 0.375, 2)
        self.assertGreater(model, preset)       # the model really does want to go deeper
        self.assertLessEqual(applied, preset + 1e-9)

    def test_a_small_cutter_is_still_scaled_down(self):
        """The preset clamp scales small cutters down. Since the 2026-09-01
        machine-realism derate (0.04" at the reference), the diameter-scaled
        preset binds at or before the chipload model's stepdown, so the applied
        depth equals that scaled preset - the model ceiling no longer bites here."""
        preset, model, applied = self._stepdown('aluminum', 0.125, 1)
        # `preset` is captured after apply_material_preset, which already scaled
        # it to the 1/8" tool - well under the 0.04" reference value.
        self.assertLess(preset, 0.04)
        self.assertAlmostEqual(applied, preset)
        self.assertLessEqual(applied, model + 1e-9)

    def test_an_aluminium_profile_is_multi_pass_again(self):
        job = build_job(tools=[Tool(1, '3/8 2F', 0.375, 2)],
                        parts=[PartOps(dxf_path=make_bare_dxf(), name='P',
                                       operations=[Operation('perimeter', 1)])],
                        material='aluminum')
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        self.assertIn('Multi-pass perimeter', result.gcode)


class TestSpindlePowerGuard(unittest.TestCase):
    """The chipload model looks at one tooth at a time and never at total load, so it
    will hand a big cutter a legitimate chipload and a feed that together ask more of the
    spindle than it has. On a router that is how end mills break: the spindle bogs, the
    cutter grabs, the tool snaps."""

    #: A team config still carrying the pre-derate aluminum numbers. The shared safety
    #: envelope now catches this before the power guard needs to.
    HOT_ALUMINUM = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
        'materials': {'aluminum': {'feed_rate': 55.0, 'ramp_feed_rate': 35.0,
                                   'max_slotting_depth': 0.2}}}}})

    def _applied(self, material, diameter, flutes, machine='omio_x8', config=None):
        pp = FRCPostProcessor(0.25, diameter, config=config)
        tool = Tool(1, 'T', diameter, flutes)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset(material)
            preset = pp.max_slotting_depth
            feeds, _ = tooling.compute_tool_feeds(tool, material, machine, 'perimeter')
            tooling.apply_tool_feeds(pp, tool, feeds)
        return (preset, pp.max_slotting_depth,
                getattr(pp, 'power_limited_depth', False), feeds)

    def test_a_big_cutter_in_aluminium_is_depth_limited(self):
        # A stale config cannot restore the old 0.2" full-width pass.
        preset, applied, bound, _ = self._applied('aluminum', 0.5, 4,
                                                  config=self.HOT_ALUMINUM)
        self.assertFalse(bound)
        self.assertLessEqual(preset, 0.06)
        self.assertLessEqual(applied, preset)

    def test_the_resulting_load_is_inside_the_spindle(self):
        for diameter, flutes in ((0.25, 2), (0.375, 2), (0.5, 3), (0.5, 4)):
            with self.subTest(diameter=diameter, flutes=flutes):
                _, applied, _, feeds = self._applied('aluminum', diameter, flutes)
                total = 0.25 + 0.008
                passes = max(1, math.ceil(total / applied))
                doc = total / passes
                mrr = doc * diameter * feeds['feed_xy']     # profile cut = full width
                hp = mrr * feeds_speeds.MATERIALS['aluminum_6061']['unit_power_hp']
                self.assertLessEqual(hp, feeds_speeds.usable_horsepower('omio_x8') + 1e-6)

    def test_it_never_binds_on_wood_or_plastic(self):
        """Unit power for plywood is a sixth of aluminium's, so the limit is chip
        evacuation and feed rate, not the motor. A guard that fired here would just make
        every wood job slower for no reason."""
        for material in ('plywood', 'polycarbonate'):
            for diameter, flutes in ((0.157, 1), (0.25, 2), (0.5, 4)):
                with self.subTest(material=material, diameter=diameter):
                    preset, applied, bound, feeds = self._applied(material, diameter, flutes)
                    self.assertFalse(bound, 'power guard should not bind here')
                    expected = min(preset, feeds['slot_stepdown'])
                    self.assertAlmostEqual(applied, expected, places=6)

    def test_the_tested_reference_tool_is_untouched(self):
        """The 4 mm single-flute is what the team's aluminium preset was tuned against.
        If the guard moved that, the guard is wrong."""
        preset, applied, bound, _ = self._applied('aluminum', 0.157, 1)
        self.assertFalse(bound)
        self.assertAlmostEqual(applied, preset, places=2)

    def test_the_limit_tracks_the_machine(self):
        weak = feeds_speeds.max_depth_for_power('generic_light_router', 'aluminum_6061',
                                                0.375, 150.0)
        strong = feeds_speeds.max_depth_for_power('omio_x8', 'aluminum_6061', 0.375, 150.0)
        self.assertLess(weak, strong)

    def test_an_unrated_spindle_imposes_no_limit(self):
        self.assertIsNone(feeds_speeds.max_depth_for_power(
            {'rpm_min': 6000, 'rpm_max': 24000, 'xy_feed_max': 150.0, 'z_feed_max': 60.0},
            'aluminum_6061', 0.375, 150.0))

    def test_the_operator_is_told_the_high_flute_tool_is_unsafe(self):
        job = build_job(material='aluminum', tools=[Tool(1, '1/2 4F', 0.5, 4)],
                        config=self.HOT_ALUMINUM,
                        parts=[PartOps(dxf_path=make_bare_dxf(), name='P',
                                       operations=[Operation('perimeter', 1)])])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('1- or 2-flute' in e for e in result.errors), result.errors)

    def test_achieved_chipload_stays_above_the_rubbing_floor(self):
        """Too little chip per tooth in aluminium means rubbing, heat, built-up edge and
        then a broken cutter - the opposite failure to too much."""
        floor = feeds_speeds.MATERIALS['aluminum_6061']['chipload_min']
        for diameter, flutes in ((0.118, 1), (0.125, 1), (0.157, 1), (0.25, 2), (0.375, 2)):
            with self.subTest(diameter=diameter, flutes=flutes):
                _, _, _, feeds = self._applied('aluminum', diameter, flutes)
                chip = feeds['feed_xy'] / (feeds['rpm'] * flutes)
                self.assertGreaterEqual(chip, floor)


class TestDepthIsRejectedWhereItCannotApply(unittest.TestCase):
    def test_perimeter_rejects_a_depth(self):
        with self.assertRaises(ToolingError) as ctx:
            Operation('perimeter', 2, depth=0.1)
        self.assertIn('never cut free', str(ctx.exception))

    def test_chamfer_rejects_a_depth(self):
        with self.assertRaises(ToolingError) as ctx:
            Operation('chamfer', 3, depth=0.01,
                      scope={'targets': ['perimeter'], 'width': 0.02})
        self.assertIn('follows from its width', str(ctx.exception))

    def test_a_pocket_still_takes_one(self):
        self.assertEqual(Operation('pockets', 2, depth=0.125).depth, 0.125)


class TestToolSuitsOperation(unittest.TestCase):
    """A V-tool sent to clear a pocket produces a perfectly well-formed program that
    ruins the part, because the post-processor only ever sees a diameter."""

    def _refuse(self, ops, tools=None):
        job = build_job(tools=tools or three_tools(),
                        parts=[PartOps(dxf_path=make_plate_dxf(), name='P', operations=ops)])
        return generate(job)

    def test_vbit_cannot_mill(self):
        for op_type in ('holes', 'pockets', 'perimeter'):
            with self.subTest(op_type=op_type):
                ops = ([Operation(op_type, 3)] if op_type == 'holes'
                       else [Operation('holes', 1), Operation(op_type, 3)])
                result = self._refuse(ops)
                self.assertFalse(result.success)
                self.assertTrue(any('V-tool' in e for e in result.errors), result.errors)

    def test_drill_cannot_profile(self):
        tools = [Tool(1, 'small', 0.125, 1), Tool(2, 'drill', 0.25, 2, type='drill')]
        result = self._refuse([Operation('holes', 1), Operation('perimeter', 2)], tools)
        self.assertFalse(result.success)
        self.assertTrue(any('only cuts on its tip' in e for e in result.errors), result.errors)

    def test_chamfer_needs_a_vbit(self):
        """Previously only a warning, so the job generated and cut a 'chamfer' with the
        flat end of an end mill."""
        tools = [Tool(1, 'small', 0.125, 1), Tool(2, 'big', 0.25, 1)]
        result = self._refuse(
            [Operation('holes', 1),
             Operation('chamfer', 2, scope={'targets': ['perimeter'], 'width': 0.02})],
            tools)
        self.assertFalse(result.success)
        self.assertTrue(any('pointed V-tool' in e for e in result.errors), result.errors)


class TestHostileNumbers(unittest.TestCase):
    """Feed and speed overrides go straight into F and S words; a negative F is not a slow
    cut, it is undefined behaviour at the controller."""

    def test_bad_tool_overrides_are_refused(self):
        for label, kwargs in (('negative feed', {'feed_rate': -100}),
                              ('zero feed', {'feed_rate': 0.0}),
                              ('NaN feed', {'feed_rate': float('nan')}),
                              ('infinite plunge', {'plunge_rate': float('inf')}),
                              ('absurd rpm', {'spindle_speed': 999999}),
                              ('NaN diameter', {'diameter': float('nan')}),
                              ('infinite diameter', {'diameter': float('inf')})):
            with self.subTest(label):
                spec = {'slot': 1, 'name': 't', 'diameter': 0.125, 'flutes': 1}
                spec.update(kwargs)
                with self.assertRaises(ToolingError):
                    Tool(**spec)

    def test_good_overrides_survive(self):
        tool = Tool(1, 't', 0.125, 1, feed_rate=40.0, plunge_rate=12.0, spindle_speed=16000)
        self.assertEqual((tool.feed_rate, tool.plunge_rate, tool.spindle_speed),
                         (40.0, 12.0, 16000))

    def test_bad_chamfer_widths_are_refused(self):
        for width in (-0.05, 0.0, float('nan'), float('inf')):
            with self.subTest(width=width):
                with self.assertRaises(ToolingError):
                    Operation('chamfer', 3, scope={'targets': ['perimeter'], 'width': width})

    def test_a_pinned_feed_does_not_emit_full_float_precision(self):
        job = build_job(tools=[Tool(1, 'pinned', 0.125, 1, feed_rate=47.0)],
                        parts=[PartOps(dxf_path=make_bare_dxf(), name='P',
                                       operations=[Operation('perimeter', 1)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        for line in result.gcode.splitlines():
            for token in line.split():
                if token.startswith('F') and '.' in token:
                    self.assertLessEqual(len(token.split('.')[1]), 2, line)


class TestChamferFit(unittest.TestCase):
    def test_a_chamfer_wider_than_a_neck_is_refused(self):
        """The part erodes to two healthy islands, so a whole-part 'is it empty' test
        passes while the waist between them is machined away."""
        job = build_job(tools=three_tools(), thickness=1.0, parts=[PartOps(
            dxf_path=make_necked_dxf(0.300), name='dogbone',
            operations=[Operation('perimeter', 2),
                        Operation('chamfer', 3,
                                  scope={'targets': ['perimeter'], 'width': 0.20})])])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('too narrow somewhere' in e for e in result.errors), result.errors)

    def test_a_chamfer_that_fits_the_neck_is_allowed(self):
        job = build_job(tools=three_tools(), thickness=1.0, parts=[PartOps(
            dxf_path=make_necked_dxf(0.300), name='dogbone',
            operations=[Operation('perimeter', 2),
                        Operation('chamfer', 3,
                                  scope={'targets': ['perimeter'], 'width': 0.02})])])
        self.assertTrue(generate(job).success)

    def test_the_fit_helper_distinguishes_the_three_cases(self):
        from shapely.geometry import Polygon as P
        wide = P([(0, 0), (4, 0), (4, 4), (0, 4)])
        self.assertTrue(tooling._chamfer_fits(wide, 0.2))       # comfortable
        self.assertFalse(tooling._chamfer_fits(P([(0, 0), (4, 0), (4, 0.1), (0, 0.1)]), 0.2))
        necked = P([(0, 0), (2, 0), (2, 1.85), (4, 1.85), (4, 0), (6, 0),
                    (6, 4), (4, 4), (4, 2.15), (2, 2.15), (2, 4), (0, 4)])
        self.assertFalse(tooling._chamfer_fits(necked, 0.2))     # erosion splits it
        self.assertTrue(tooling._chamfer_fits(necked, 0.02))


def make_layered_dxf():
    """A 2.5D DXF: geometry split across Z_* depth layers."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    doc.layers.add('Z_0p500')
    doc.layers.add('Z_0p250')
    msp.add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True,
                       dxfattribs={'layer': 'Z_0p500'})
    msp.add_circle((1, 1), 0.15, dxfattribs={'layer': 'Z_0p500'})
    msp.add_lwpolyline([(2, 2), (4, 2), (4, 3), (2, 3)], close=True,
                       dxfattribs={'layer': 'Z_0p250'})
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


def make_duplicate_hole_dxf():
    """The 6x4 plate with one circle pasted exactly on top of another."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True)
    for (x, y) in [(1, 1), (5, 1), (1, 3), (5, 3)]:
        msp.add_circle((x, y), 0.098)
    msp.add_circle((1, 1), 0.098)                 # the duplicate
    msp.add_circle((3, 2), 0.375)
    msp.add_lwpolyline([(2, 2.6), (4, 2.6), (4, 3.4), (2, 3.4)], close=True)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


def covering_ops():
    """A plan that cuts every feature make_plate_dxf has, exactly once."""
    return [Operation('holes', 1, scope={'max_diameter': 0.4}),
            Operation('holes', 2, scope={'min_diameter': 0.4}),
            Operation('pockets', 2), Operation('perimeter', 2)]


class TestFeatureCoverage(unittest.TestCase):
    """Every machinable feature must be cut by exactly one operation. A feature nobody
    claims is absent from the program and is discovered with the part off the machine."""

    def _run(self, ops, dxf=None):
        return generate(build_job(parts=[PartOps(
            dxf_path=dxf or make_plate_dxf(), name='plate', operations=ops)]))

    def test_an_uncovered_pocket_is_reported(self):
        """Pockets were not checked at all, so a plan of [holes, perimeter] generated
        cleanly and simply never cut the pocket."""
        result = self._run([Operation('holes', 1), Operation('perimeter', 2)])
        self.assertFalse(result.success)
        self.assertTrue(any('pocket(s) are not cut' in e for e in result.errors),
                        result.errors)

    def test_uncovered_holes_are_reported_without_a_holes_operation(self):
        """The check used to be gated on the part having a holes operation, so a plan of
        [pockets, perimeter] on a 5-hole plate passed silently and drilled nothing."""
        result = self._run([Operation('pockets', 2), Operation('perimeter', 2)])
        self.assertFalse(result.success)
        self.assertTrue(any('hole(s) are not cut' in e for e in result.errors),
                        result.errors)

    def test_a_profile_only_plan_reports_both_kinds(self):
        result = self._run([Operation('perimeter', 2)])
        self.assertFalse(result.success)
        self.assertGreaterEqual(len(result.errors), 2)

    def test_a_complete_plan_generates(self):
        self.assertTrue(self._run(covering_ops()).success)

    def test_pockets_claimed_twice_are_reported(self):
        result = self._run([Operation('holes', 1, scope={'max_diameter': 0.4}),
                            Operation('holes', 2, scope={'min_diameter': 0.4}),
                            Operation('pockets', 2), Operation('pockets', 2),
                            Operation('perimeter', 2)])
        self.assertFalse(result.success)
        self.assertTrue(any('more than one operation' in e for e in result.errors),
                        result.errors)

    def test_coincident_duplicates_are_reported(self):
        """Two features that round to the same identity key are indistinguishable to
        every scope and to the cut-twice guard, so the duplicate is bored again in
        silence. It is a CAD mistake, so name it rather than guess."""
        result = self._run(covering_ops(), dxf=make_duplicate_hole_dxf())
        self.assertFalse(result.success)
        self.assertTrue(any('on top of each other' in e for e in result.errors),
                        result.errors)


class TestEmptyIndicesSelectNothing(unittest.TestCase):
    """An ABSENT `indices` means "select by range"; a PRESENT but empty one means the
    user picked nothing. Testing for truthiness conflated them, so clearing the feature
    picker in the UI fell through to the range branch and cut every hole in the part."""

    def setUp(self):
        job = build_job()
        with redirect_stdout(io.StringIO()):
            self.features = tooling.survey_part(job, job.parts[0])

    def test_empty_indices_select_nothing(self):
        self.assertEqual(len(tooling.selected_hole_keys(self.features, {'indices': []})), 0)
        self.assertEqual(len(tooling.selected_pocket_keys(self.features, {'indices': []})), 0)

    def test_absent_indices_still_select_everything(self):
        self.assertEqual(len(tooling.selected_hole_keys(self.features, {})), 5)
        self.assertEqual(len(tooling.selected_pocket_keys(self.features, {})), 1)

    def test_listed_indices_select_those(self):
        self.assertEqual(len(tooling.selected_hole_keys(self.features, {'indices': [0, 2]})), 2)


class TestTwoAndAHalfDIsRefused(unittest.TestCase):
    def test_a_multilayer_dxf_is_refused(self):
        """load_dxf keeps only the shallowest layer and overwrites the stated thickness
        from the layer depths, so a multi-tool job would machine one layer of a stepped
        part at the wrong thickness. The browser hides 2.5D; the route can be posted to
        directly, so the server needs its own guard."""
        result = generate(build_job(parts=[PartOps(
            dxf_path=make_layered_dxf(), name='stepped', operations=covering_ops())]))
        self.assertFalse(result.success)
        self.assertTrue(any('2.5D' in e for e in result.errors), result.errors)


class TestFixturingPause(unittest.TestCase):
    PAUSE = TeamConfig({'machining': {
        'z_reference': {'tool_change_height': 2.0},
        'fixturing': {'pause_after_holes': True, 'pause_before_perimeter': False},
    }})

    def test_pause_after_holes_is_the_default(self):
        self.assertTrue(TeamConfig().pause_after_holes)

    def test_a_profile_only_job_does_not_claim_there_are_fastening_holes(self):
        job = build_job(config=self.PAUSE, parts=[PartOps(
            dxf_path=make_bare_dxf(), name='P',
            operations=[Operation('perimeter', 2)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        self.assertNotIn('PAUSE FOR FIXTURING', result.gcode)

    def test_pause_fires_after_holes_and_before_pockets(self):
        result = generate(build_job(config=self.PAUSE, parts=[PartOps(
            dxf_path=make_plate_dxf(), name='P', operations=covering_ops())]))
        self.assertTrue(result.success, result.errors)
        self.assertIn('Install fasteners', result.gcode)
        lines = result.gcode.splitlines()
        last_hole = max(i for i, line in enumerate(lines)
                        if line.startswith('(===== ') and 'HOLES -' in line)
        pause = next(i for i, line in enumerate(lines) if 'PAUSE FOR FIXTURING' in line)
        first_later_cut = min(i for i, line in enumerate(lines)
                              if line.startswith('(===== ')
                              and ('POCKETS -' in line or 'PERIMETER -' in line))
        self.assertLess(last_hole, pause)
        self.assertLess(pause, first_later_cut)

    def test_pause_uses_the_roomy_manual_access_height(self):
        result = generate(build_job(config=self.PAUSE))
        pause = result.gcode.split('( === PAUSE FOR FIXTURING === )', 1)[1]
        self.assertIn('G0 Z2.0000  ; Safe Z clearance', pause.split('M0', 1)[0])

    def test_an_explicit_false_disables_the_default_pause(self):
        cfg = TeamConfig({'machining': {'fixturing': {
            'pause_after_holes': False, 'pause_before_perimeter': False,
        }}})
        result = generate(build_job(config=cfg))
        self.assertNotIn('PAUSE FOR FIXTURING', result.gcode)

    def test_every_parts_holes_precede_the_shared_pause(self):
        dxf = make_plate_dxf()
        result = generate(build_job(config=self.PAUSE, parts=[
            PartOps(dxf_path=dxf, name='A', place_x=0, operations=covering_ops()),
            PartOps(dxf_path=dxf, name='B', place_x=7, operations=covering_ops()),
        ]))
        self.assertTrue(result.success, result.errors)
        lines = result.gcode.splitlines()
        last_hole = max(i for i, line in enumerate(lines)
                        if line.startswith('(===== ') and 'HOLES -' in line)
        pause = next(i for i, line in enumerate(lines) if 'PAUSE FOR FIXTURING' in line)
        first_pocket = min(i for i, line in enumerate(lines)
                           if line.startswith('(===== ') and 'POCKETS -' in line)
        self.assertLess(last_hole, pause)
        self.assertLess(pause, first_pocket)

    def test_the_split_does_not_reorder_a_parts_own_operations(self):
        parts = [PartOps(dxf_path='x.dxf', name='p', operations=[
            Operation('holes', 1), Operation('pockets', 2),
            Operation('perimeter', 2), Operation('chamfer', 3,
                                                 scope={'targets': ['perimeter']})])]
        order = tooling.order_operations(parts, split_after_holes=True)
        self.assertEqual([op for _, op in order], [0, 1, 2, 3])


class TestFeedsMachineResolution(unittest.TestCase):
    def test_machine_id_reaches_the_feeds_model(self):
        """feeds_machine defaulted to a truthy value, making the machine_id fallback
        unreachable - so a team on an Avid got Omio's 150 IPM ceiling, not its own 400."""
        omio, _ = tooling.compute_tool_feeds(Tool(1, 't', 0.25, 1), 'plywood',
                                             'omio_x8', 'perimeter')
        avid, _ = tooling.compute_tool_feeds(Tool(1, 't', 0.25, 1), 'plywood',
                                             'avid_pro2424', 'perimeter')
        self.assertNotEqual(omio['feed_xy'], avid['feed_xy'])

        job = build_job(machine_id='avid_pro2424', tools=[Tool(1, 't', 0.25, 1)],
                        parts=[PartOps(dxf_path=make_bare_dxf(), name='P',
                                       operations=[Operation('perimeter', 1)])])
        feeds, _ = tooling.compute_tool_feeds(job.tools[0], job.material, job.machine_id,
                                              'perimeter', feeds_machine=job.feeds_machine)
        self.assertEqual(feeds['feed_xy'], avid['feed_xy'])

    def test_an_unknown_machine_falls_back_to_the_default(self):
        feeds, _ = tooling.compute_tool_feeds(Tool(1, 't', 0.25, 1), 'plywood',
                                              'some_custom_router', 'perimeter')
        default, _ = tooling.compute_tool_feeds(Tool(1, 't', 0.25, 1), 'plywood',
                                                tooling.DEFAULT_FEEDS_MACHINE, 'perimeter')
        self.assertEqual(feeds['feed_xy'], default['feed_xy'])


class TestHeaderReflectsTheProgram(unittest.TestCase):
    def test_a_chamfer_only_job_reports_its_real_zmin(self):
        """A chamfer never touches cut_depth, so reading that for ZMIN reported whatever
        the through-cut default happened to be rather than where the tool goes."""
        result = generate(build_job(thickness=1.0, parts=[PartOps(
            dxf_path=make_bare_dxf(), name='P',
            operations=[Operation('chamfer', 3,
                                  scope={'targets': ['perimeter'], 'width': 0.20})])]))
        self.assertTrue(result.success, result.errors)
        lines = result.gcode.splitlines()
        declared = float([l for l in lines if l.startswith('(ZMIN:')][0]
                         .split(':')[1].strip().strip('")'))
        cuts = [float(t[1:]) for l in lines for t in l.split()
                if t.startswith('Z') and l.startswith(('G1', 'G2', 'G3'))]
        self.assertAlmostEqual(declared, min(cuts), places=4)

    def test_a_tool_that_cut_nothing_is_not_listed(self):
        """Listing it would send the operator hunting for a cutter the program never
        pauses for."""
        result = generate(build_job(parts=[PartOps(
            dxf_path=make_bare_dxf(), name='P',
            operations=[Operation('pockets', 1, 'this plate has none'),
                        Operation('perimeter', 2)])]))
        self.assertTrue(result.success, result.errors)
        self.assertEqual(len(result.stats['tools']), 1)
        self.assertIn('T2', result.stats['tools'][0])


def make_hole_dxf(diameter, count=1):
    """A 6x4 plate carrying `count` holes of one diameter and nothing else."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True)
    for i in range(count):
        msp.add_circle((1 + i * 1.2, 1), diameter / 2.0)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


def drill_job(hole_diameter, drill_diameter, material='plywood', count=1, **scope):
    return build_job(
        material=material,
        tools=[Tool(1, 'twist drill', drill_diameter, 2, type='drill')],
        parts=[PartOps(dxf_path=make_hole_dxf(hole_diameter, count), name='plate',
                       operations=[Operation('holes', 1, 'Drill', scope=scope)])])


class TestDrillingIsNotMilling(unittest.TestCase):
    """A twist drill has no side cutting edges. The only motion it may make under load is
    straight down its own axis - so a drilling operation must never reach the milling
    generator, which enters helically and feeds sideways to open a bore."""

    def _drill(self, hole=0.196, drill=0.196, **kw):
        return generate(drill_job(hole, drill, **kw))

    @staticmethod
    def _lateral_cuts(gcode):
        """Feed moves with any X or Y component - what a drill must never emit."""
        return [l for l in gcode.splitlines()
                if l.startswith(('G1', 'G2', 'G3')) and ('X' in l or 'Y' in l)]

    def test_a_drill_emits_no_lateral_cutting_moves(self):
        """The reported bug: with the drill smaller than the hole this emitted helical
        entry plus a dozen lateral moves - end-mill toolpaths on a twist bit."""
        result = self._drill(hole=0.196, drill=0.196)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(self._lateral_cuts(result.gcode), [])

    def test_a_drill_pecks_rather_than_plunging_in_one_go(self):
        result = self._drill()
        self.assertIn('Peck 1 of', result.gcode)
        self.assertIn('Retract to clear chips', result.gcode)
        self.assertNotIn('Helical', result.gcode)

    def test_no_lateral_moves_for_any_in_tolerance_size(self):
        for hole, drill in ((0.196, 0.196), (0.196, 0.1935), (0.196, 0.2031),
                            (0.257, 0.250), (0.250, 0.2500)):
            with self.subTest(hole=hole, drill=drill):
                result = self._drill(hole=hole, drill=drill)
                self.assertTrue(result.success, result.errors)
                self.assertEqual(self._lateral_cuts(result.gcode), [])

    def test_a_through_hole_goes_past_the_stock_by_the_point_length(self):
        """A drill's tip reaches depth before its full diameter does. Without the extra
        the exit side of the hole is a cone, not an opening."""
        result = self._drill(hole=0.25, drill=0.25)
        self.assertTrue(result.success, result.errors)
        point = FRCPostProcessor.drill_point_length(0.25, 118.0)
        depths = [float(t[1:]) for l in result.gcode.splitlines() for t in l.split()
                  if l.startswith('G1') and t.startswith('Z')]
        self.assertAlmostEqual(min(depths), -0.008 - point, places=4)

    def test_point_length_matches_the_geometry(self):
        # 118 deg included -> 59 deg half angle -> length = (D/2)/tan(59)
        self.assertAlmostEqual(FRCPostProcessor.drill_point_length(0.25, 118.0),
                               0.125 / math.tan(math.radians(59)), places=6)
        # A flatter 135 deg split point reaches depth sooner.
        self.assertLess(FRCPostProcessor.drill_point_length(0.25, 135.0),
                        FRCPostProcessor.drill_point_length(0.25, 118.0))

    def test_drill_feeds_come_from_the_drilling_model_not_the_milling_one(self):
        tool = Tool(1, 'd', 0.25, 2, type='drill')
        drill_feeds, _ = tooling.compute_tool_feeds(tool, 'plywood', 'omio_x8', 'holes')
        mill = Tool(2, 'm', 0.25, 2)
        mill_feeds, _ = tooling.compute_tool_feeds(mill, 'plywood', 'omio_x8', 'holes')
        self.assertEqual(drill_feeds['operation'], 'drill')
        self.assertLess(drill_feeds['peck_feed'], mill_feeds['peck_feed'])

    def test_a_drill_cannot_be_used_for_milling_operations(self):
        for op_type in ('pockets', 'perimeter', 'interior'):
            with self.subTest(op_type=op_type):
                message = tooling._check_tool_suits_operation(
                    Operation(op_type, 1), Tool(1, 'd', 0.25, 2, type='drill'))
                self.assertIsNotNone(message)
                self.assertIn('drill', message)

    def test_the_header_does_not_promise_no_straight_plunges(self):
        """Drilling IS a straight plunge; a header claiming otherwise is worse than one
        that says nothing, because the operator reads it."""
        result = self._drill()
        self.assertNotIn('No straight plunges', result.gcode)
        self.assertIn('straight axial plunge', result.gcode)

    def test_a_mixed_job_describes_both_entry_styles(self):
        job = build_job(
            tools=[Tool(1, '#10 drill', 0.1935, 2, type='drill'),
                   Tool(2, '1/4 endmill', 0.25, 2)],
            parts=[PartOps(dxf_path=make_plate_dxf(), name='plate', operations=[
                Operation('holes', 1, 'Drill', scope={'max_diameter': 0.3}),
                Operation('holes', 2, 'Bore', scope={'min_diameter': 0.3}),
                Operation('pockets', 2), Operation('perimeter', 2)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        self.assertIn('helical entry', result.gcode)
        self.assertIn('straight axial plunge', result.gcode)


class TestDrillSizeSnapping(unittest.TestCase):
    """A twist drill makes exactly one size of hole - its own. A drawing asking for 0.196
    and a crib holding a #10 have to meet somewhere."""

    def test_a_near_size_hole_is_drilled_at_the_tool_size(self):
        result = generate(drill_job(0.196, 0.1935))
        self.assertTrue(result.success, result.errors)
        self.assertTrue(any('will be drilled at 0.1935' in w for w in result.warnings),
                        result.warnings)

    def test_a_hole_slightly_smaller_than_the_drill_still_snaps(self):
        """The survey gate is an end-mill rule (cutter must fit inside the hole). A drill
        does not cut a hole, it is the hole, so a 13/64 against a 0.196 drawing is a
        stocking difference, not an error."""
        result = generate(drill_job(0.196, 0.2031))
        self.assertTrue(result.success, result.errors)
        self.assertTrue(any('will be drilled at 0.2031' in w for w in result.warnings),
                        result.warnings)

    def test_an_exact_match_says_nothing(self):
        result = generate(drill_job(0.196, 0.196))
        self.assertTrue(result.success, result.errors)
        self.assertFalse([w for w in result.warnings if 'will be drilled at' in w])

    def test_a_real_mismatch_is_refused_with_the_numbers(self):
        result = generate(drill_job(0.500, 0.196))
        self.assertFalse(result.success)
        self.assertTrue(any('0.5000' in e and '0.1960' in e for e in result.errors),
                        result.errors)

    def test_the_tolerance_is_configurable_per_operation(self):
        tight = generate(drill_job(0.196, 0.1935, size_tolerance=0.001))
        self.assertFalse(tight.success)
        loose = generate(drill_job(0.196, 0.1935, size_tolerance=0.020))
        self.assertTrue(loose.success, loose.errors)

    def test_every_snapped_hole_is_drilled(self):
        result = generate(drill_job(0.196, 0.1935, count=4))
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.gcode.count('Rapid over hole centre'), 4)
        self.assertTrue(any('4 hole(s)' in w for w in result.warnings), result.warnings)


class TestHostileInputIsRefusedNotCrashed(unittest.TestCase):
    """Ordinary bad input - a blank tool row, a field the UI left null, a hand-written
    job file with a typo - used to escape as TypeError/AttributeError and surface as an
    HTTP 500 with a traceback. All of it is a ToolingError now, which the routes render
    as a 400 the user can act on."""

    GOOD_TOOLS = [{'slot': 1, 'name': 'a', 'diameter': 0.125, 'flutes': 1}]
    GOOD_PART = {'file_index': 0, 'name': 'p', 'operations': [
        {'op_type': 'holes', 'tool_slot': 1}, {'op_type': 'perimeter', 'tool_slot': 1}]}

    def _spec(self, **over):
        base = {'material': 'plywood', 'thickness': 0.25,
                'tools': self.GOOD_TOOLS, 'parts': [self.GOOD_PART]}
        base.update(over)
        return base

    def _refuses(self, spec):
        with self.assertRaises(ToolingError):
            tooling.job_from_dict(spec, {0: make_bare_dxf()})

    def test_wrong_types_are_refused(self):
        for label, spec in (
            ('tools not a list', self._spec(tools=5)),
            ('parts not a list', self._spec(parts=5)),
            ('part not an object', self._spec(parts=[5])),
            ('operations not a list', self._spec(parts=[dict(self.GOOD_PART, operations=7)])),
            ('operation not an object', self._spec(parts=[dict(self.GOOD_PART, operations=[7])])),
            ('job not an object', [1, 2, 3]),
        ):
            with self.subTest(label):
                self._refuses(spec)

    def test_non_numbers_are_refused(self):
        for label, spec in (
            ('thickness null', self._spec(thickness=None)),
            ('thickness zero', self._spec(thickness=0)),
            ('thickness negative', self._spec(thickness=-1)),
            ('thickness NaN', self._spec(thickness=float('nan'))),
            ('thickness text', self._spec(thickness='abc')),
            ('tab_spacing null', self._spec(tab_spacing=None)),
            ('place_x NaN', self._spec(parts=[dict(self.GOOD_PART, place_x=float('nan'))])),
            ('rotation text', self._spec(parts=[dict(self.GOOD_PART, rotation='left')])),
        ):
            with self.subTest(label):
                self._refuses(spec)

    def test_a_blank_tool_row_is_refused_with_a_readable_message(self):
        with self.assertRaises(ToolingError) as ctx:
            tooling.job_from_dict(self._spec(tools=[{}]), {0: make_bare_dxf()})
        self.assertIn('blank row', str(ctx.exception))

    def test_bad_scope_shapes_are_refused(self):
        for scope in (5, [], '', {'indices': 'abc'}, {'indices': [None]},
                      {'indices': ['x']}):
            with self.subTest(scope=scope):
                with self.assertRaises(ToolingError):
                    Operation.from_dict({'op_type': 'holes', 'tool_slot': 1, 'scope': scope})

    def test_unknown_drill_purpose_is_refused(self):
        """A misspelled tap operation must not silently become a clearance hole."""
        with self.assertRaisesRegex(ToolingError, 'purpose'):
            Operation.from_dict({'op_type': 'holes', 'tool_slot': 1,
                                 'scope': {'purpose': 'tpa'}})

    def test_fractional_integer_fields_are_not_silently_truncated(self):
        for field, value in (('slot', 1.9), ('flutes', 2.5)):
            with self.subTest(field=field):
                tool = {'slot': 1, 'name': 'a', 'diameter': 0.125, 'flutes': 1}
                tool[field] = value
                with self.assertRaises(ToolingError):
                    Tool.from_dict(tool)
        with self.assertRaises(ToolingError):
            Operation.from_dict({'op_type': 'holes', 'tool_slot': 1.9})
        self._refuses(self._spec(parts=[dict(self.GOOD_PART, file_index=0.5)]))

    def test_text_booleans_do_not_mirror_or_add_machining(self):
        """Python considers the string ``false`` true, opposite to its JSON meaning."""
        self._refuses(self._spec(parts=[dict(self.GOOD_PART, mirror='false')]))
        self._refuses(self._spec(engrave='false'))

    def test_job_size_is_capped(self):
        dxf = make_bare_dxf()
        many = [PartOps(dxf_path=dxf, name=f'p{i}',
                        operations=[Operation('perimeter', 1)])
                for i in range(tooling.MAX_PARTS_PER_JOB + 1)]
        with self.assertRaises(ToolingError) as ctx:
            build_job(tools=[Tool(1, 't', 0.25, 1)], parts=many)
        self.assertIn('limit', str(ctx.exception))

        ops = [Operation('perimeter', 1)] * (tooling.MAX_OPERATIONS_PER_JOB + 1)
        with self.assertRaises(ToolingError) as ctx:
            build_job(tools=[Tool(1, 't', 0.25, 1)],
                      parts=[PartOps(dxf_path=dxf, name='p', operations=ops)])
        self.assertIn('operations', str(ctx.exception))


class TestFilenameSafety(unittest.TestCase):
    def test_the_client_timestamp_cannot_escape_the_output_directory(self):
        """The browser supplies the timestamp so the filename matches the operator's
        clock. It used to reach the path with only '-', ' ' and ':' stripped."""
        for hostile in ('/../../ESCAPED', '..\\..\\win', '/etc/passwd', 'a/b/c'):
            with self.subTest(hostile):
                name = build_output_filename('job', hostile)
                self.assertNotIn('/', name)
                self.assertNotIn('\\', name)
                self.assertNotIn('..', name)
                self.assertTrue(name.endswith('.nc'))

    def test_a_normal_timestamp_still_shows_through(self):
        self.assertEqual(build_output_filename('job', '2026-08-20 12:00:00'),
                         'job_20260820_120000.nc')

    def test_an_unusable_timestamp_falls_back_to_server_time(self):
        name = build_output_filename('job', '////')
        self.assertRegex(name, r'^job_\d{8}_\d{6}\.nc$')


class TestNamesInComments(unittest.TestCase):
    def test_a_display_name_cannot_break_the_comment_rules(self):
        """An Onshape/Google display name reads like 'Trent Fox (Mentor) Jose' - a nested
        paren and a non-ASCII byte, on line 3 of every program the hosted app makes."""
        pp = FRCPostProcessor(0.25, 0.157)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset('plywood')
        pp.user_name = 'Trent Fox (Mentor) José'
        pp.team_name = 'Team (6238) – Penguins'
        pp.holes, pp.pockets, pp.perimeter = [], [], None
        header = pp._generate_gcode_header('2026-08-20 12:00:00')
        for line in header:
            line.encode('ascii')
            depth = maxdepth = 0
            for ch in line.split(';')[0]:
                if ch == '(':
                    depth += 1
                    maxdepth = max(maxdepth, depth)
                elif ch == ')':
                    depth -= 1
            self.assertLessEqual(maxdepth, 1, line)


class TestDrillSizeIndex(unittest.TestCase):
    """CAD nearly always draws a hole at a real drill size, so the lookup should be
    exact for the sizes an FRC team actually uses."""

    def test_common_frc_hole_sizes_resolve_exactly(self):
        for hole, label in ((0.1935, '#10'), (0.1960, '#9'), (0.2010, '#7'),
                            (0.1660, '#19'), (0.1495, '#25'), (0.1360, '#29'),
                            (0.1562, '5/32 in'), (0.1875, '3/16 in'), (0.2500, '1/4 in'),
                            (0.2656, '17/64 in'), (0.3750, '3/8 in')):
            with self.subTest(hole=hole):
                match = drill_sizes.nearest_drill(hole)
                self.assertIsNotNone(match, hole)
                self.assertEqual(match.label, label)
                self.assertAlmostEqual(match.diameter, hole, places=4)

    def test_a_bore_has_no_drill(self):
        """A 1.125 bearing bore is bored with an end mill, not drilled."""
        self.assertIsNone(drill_sizes.nearest_drill(1.125))
        self.assertIn('end mill', drill_sizes.describe_suggestion(1.125))

    def test_it_rounds_up_not_to_nearest(self):
        """A hole drilled undersize will not pass the fastener it was drawn for, so a
        drill just over the hole beats a nearer one just under it."""
        hole = 0.1900                       # nearer #11 (.1910) than #13 (.1850)
        match = drill_sizes.nearest_drill(hole)
        self.assertGreaterEqual(match.diameter, hole)

    def test_a_slightly_undersize_drill_is_only_a_last_resort(self):
        under = drill_sizes.nearest_drill(0.2295)     # #1 is .2280, 15/64 is .2344
        self.assertIsNotNone(under)
        self.assertGreaterEqual(under.diameter, 0.2295 - drill_sizes.UNDERSIZE_TOLERANCE)

    def test_suggest_drills_groups_and_counts(self):
        result = drill_sizes.suggest_drills([0.1935, 0.1935, 0.1935, 0.25, 1.125])
        matched = {m['drill']['label']: m['count'] for m in result['matched']}
        self.assertEqual(matched.get('#10'), 3)
        self.assertEqual(matched.get('1/4 in'), 1)
        self.assertEqual(result['unmatched'], [1.125])

    def test_every_index_entry_is_self_consistent(self):
        for size in drill_sizes.DRILL_INDEX:
            self.assertGreater(size.diameter, 0)
            self.assertIn(size.series, ('fractional', 'number', 'letter'))
            self.assertTrue(size.label)


class TestDrillAwareSuggestions(unittest.TestCase):
    """The reported bug: a 5/32 drill was suggested for 0.1935 holes, because
    the suggester sorted drills in with the end mills and assigned by diameter
    RANGE - which a drill cannot honour, since it makes exactly one size."""

    def _suggest(self, hole_diameters, tools):
        dxf = make_hole_dxf(hole_diameters[0], count=len(hole_diameters))
        job = build_job(tools=tools, parts=[PartOps(
            dxf_path=dxf, name='p', operations=[Operation('perimeter', tools[-1].slot)])])
        with redirect_stdout(io.StringIO()):
            features = tooling.survey_part(job, job.parts[0])
            plan = tooling.suggest_tooling(features, available=tools, mill_diameter=0.25)
            return features, plan['operations']

    def test_a_drill_is_never_given_a_hole_it_cannot_make(self):
        tools = [Tool(1, '5/32 Drillbit', 0.1562, 2, type='drill'),
                 Tool(2, '#10 Drillbit', 0.1935, 2, type='drill'),
                 Tool(3, '1/4 endmill', 0.25, 2)]
        features, ops = self._suggest([0.1935] * 4, tools)
        by_slot = {t.slot: t for t in tools}
        for op in ops:
            if op.op_type != 'holes':
                continue
            tool = by_slot[op.tool_slot]
            if tool.type != 'drill':
                continue
            for hole in features['holes']:
                if tooling.selected_hole_keys(features, op.scope) & {hole['key']}:
                    self.assertLessEqual(
                        abs(hole['diameter'] - tool.diameter),
                        tooling.DEFAULT_DRILL_SIZE_TOLERANCE,
                        f"{tool.name} was given a {hole['diameter']} hole")

    def test_the_suggested_plan_actually_generates(self):
        """A suggestion that fails validation the moment it is used is worse than none."""
        tools = [Tool(1, '5/32 Drillbit', 0.1562, 2, type='drill'),
                 Tool(2, '#10 Drillbit', 0.1935, 2, type='drill'),
                 Tool(3, '1/4 endmill', 0.25, 2)]
        dxf = make_hole_dxf(0.1935, count=4)
        job = build_job(tools=tools, parts=[PartOps(
            dxf_path=dxf, name='p', operations=[Operation('perimeter', 3)])])
        with redirect_stdout(io.StringIO()):
            features = tooling.survey_part(job, job.parts[0])
            plan = tooling.suggest_tooling(features, available=tools, mill_diameter=0.25)
        result = generate(build_job(tools=tools + plan['tools'], parts=[
            PartOps(dxf_path=dxf, name='p', operations=plan['operations'])]))
        self.assertTrue(result.success, result.errors)

    def test_the_error_names_the_drill_that_would_work(self):
        result = generate(build_job(
            tools=[Tool(1, '5/32 Drillbit', 0.1562, 2, type='drill'),
                   Tool(2, '1/4 endmill', 0.25, 2)],
            parts=[PartOps(dxf_path=make_hole_dxf(0.1935, 4), name='p', operations=[
                Operation('holes', 1, 'Small holes'), Operation('perimeter', 2)])]))
        self.assertFalse(result.success)
        self.assertTrue(any('#10' in e for e in result.errors), result.errors)


class TestDrillPurpose(unittest.TestCase):
    """The same drawn diameter wants opposite drills depending on what the hole is for.
    0.190 as a 10-32 CLEARANCE hole wants a #10 (0.1935, over); the same 0.190 to be
    TAPPED 10-32 wants a #21 (0.1590, well under); as a SPOT it wants whatever is in the
    spindle."""

    def _job(self, hole, drill_diameter, purpose, count=3):
        return build_job(
            material='aluminum',
            tools=[Tool(1, 'drill', drill_diameter, 2, type='drill'),
                   Tool(2, 'endmill', 0.25, 2)],
            parts=[PartOps(dxf_path=make_hole_dxf(hole, count), name='p', operations=[
                Operation('holes', 1, 'Drill', scope={'purpose': purpose}),
                Operation('perimeter', 2)])])

    def test_tap_accepts_the_deliberately_undersize_drill(self):
        result = generate(self._job(0.190, 0.159, 'tap'))
        self.assertTrue(result.success, result.errors)
        self.assertTrue(any('TAP DRILL' in w for w in result.warnings), result.warnings)

    def test_the_same_drill_is_refused_as_a_clearance_hole(self):
        result = generate(self._job(0.190, 0.159, 'clearance'))
        self.assertFalse(result.success)

    def test_tap_refuses_a_drill_that_is_not_the_tap_size(self):
        result = generate(self._job(0.190, 0.1935, 'tap'))   # a clearance drill
        self.assertFalse(result.success)
        self.assertTrue(any('tap' in e.lower() for e in result.errors), result.errors)

    def test_an_ambiguous_nominal_is_flagged_not_guessed(self):
        """10-24 and 10-32 are both 0.190 nominal but need different tap drills."""
        result = generate(self._job(0.190, 0.159, 'tap'))
        self.assertTrue(any('share this nominal' in w for w in result.warnings),
                        result.warnings)

    def test_a_non_thread_size_cannot_be_tapped(self):
        result = generate(self._job(0.3000, 0.159, 'tap'))    # not a thread nominal
        self.assertFalse(result.success)
        self.assertTrue(any('not a thread size' in e for e in result.errors), result.errors)

    def test_spot_accepts_any_tool_for_any_hole(self):
        """A centre drill only marks a location, so it is not held to the hole's size."""
        result = generate(self._job(0.1935, 0.125, 'spot'))
        self.assertTrue(result.success, result.errors)
        self.assertIn('CENTRE DRILLING', result.gcode)

    def test_spot_is_shallow_and_never_goes_through(self):
        result = generate(self._job(0.1935, 0.125, 'spot'))
        lines = result.gcode.splitlines()
        start = next(i for i, l in enumerate(lines) if 'CENTRE DRILLING' in l)
        end = next(i for i, l in enumerate(lines[start + 1:], start + 1)
                   if l.startswith('(===== '))
        depths = [float(t[1:]) for line in lines[start:end] for t in line.split()
                  if t.startswith('Z') and line.startswith('G1')]
        self.assertTrue(depths)
        self.assertGreater(min(depths), 0.0)          # never past the material top
        self.assertLess(min(depths), 0.25)            # but it does cut in

    def test_spot_says_the_holes_are_not_finished(self):
        result = generate(self._job(0.1935, 0.125, 'spot'))
        self.assertTrue(any('still have to be drilled' in w for w in result.warnings),
                        result.warnings)

    def test_spot_depth_is_overridable(self):
        job = build_job(
            tools=[Tool(1, 'centre', 0.125, 2, type='drill'), Tool(2, 'em', 0.25, 2)],
            parts=[PartOps(dxf_path=make_hole_dxf(0.1935, 2), name='p', operations=[
                Operation('holes', 1, 'Spot',
                          scope={'purpose': 'spot', 'spot_depth': 0.015}),
                Operation('perimeter', 2)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        self.assertIn('0.0150 in deep', result.gcode)

    def test_tap_drill_table_is_internally_consistent(self):
        for thread, spec in drill_sizes.TAP_DRILLS.items():
            with self.subTest(thread=thread):
                self.assertLess(spec['tap'], spec['nominal'],
                                f'{thread}: a tap drill must be undersize')
                self.assertGreater(spec['clearance'], spec['nominal'],
                                   f'{thread}: a clearance drill must be oversize')


class TestDrillToleranceOverride(unittest.TestCase):
    """The size check is the user's to widen - it is their machine and their part - but a
    big substitution is always reported with what it will actually cost them."""

    def _job(self, scope=None, job_tolerance=None):
        fields = {'drill_size_tolerance': job_tolerance} if job_tolerance else {}
        return build_job(
            material='aluminum',
            tools=[Tool(1, '5/32', 0.15625, 2, type='drill'), Tool(2, 'em', 0.25, 2)],
            parts=[PartOps(dxf_path=make_hole_dxf(0.1935, 4), name='p', operations=[
                Operation('holes', 1, 'Holes', scope=scope or {}),
                Operation('perimeter', 2)])],
            **fields)

    def test_the_default_refuses_a_0_037_substitution(self):
        result = generate(self._job())
        self.assertFalse(result.success)

    def test_the_error_offers_the_tap_route_when_the_tool_is_a_tap_drill(self):
        """5/32 is 0.0028 from the #21, the 10-32 tap drill. A hole drawn at a clearance
        size with a much smaller drill assigned is usually a tapped hole, not a mistake
        about size."""
        result = generate(self._job())
        self.assertTrue(any('tap drill for' in e for e in result.errors), result.errors)

    def test_the_error_says_how_to_override(self):
        result = generate(self._job())
        self.assertTrue(any('size_tolerance' in e for e in result.errors), result.errors)

    def test_a_job_level_tolerance_is_honoured(self):
        result = generate(self._job(job_tolerance=0.05))
        self.assertTrue(result.success, result.errors)

    def test_an_operation_tolerance_overrides_the_job(self):
        result = generate(self._job(scope={'size_tolerance': 0.05}, job_tolerance=0.001))
        self.assertTrue(result.success, result.errors)

    def test_a_big_substitution_names_its_consequence(self):
        result = generate(self._job(job_tolerance=0.05))
        self.assertTrue(any('UNDERSIZE' in w and 'will not pass through' in w
                            for w in result.warnings), result.warnings)

    def test_a_small_substitution_stays_quiet_about_consequences(self):
        job = build_job(
            tools=[Tool(1, '#10', 0.1935, 2, type='drill'), Tool(2, 'em', 0.25, 2)],
            parts=[PartOps(dxf_path=make_hole_dxf(0.1960, 4), name='p', operations=[
                Operation('holes', 1, 'Holes'), Operation('perimeter', 2)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        self.assertFalse([w for w in result.warnings if 'UNDERSIZE' in w])

    def test_tap_mode_accepts_a_hole_drawn_at_the_clearance_size(self):
        """CAD libraries routinely draw every #10 hole at 0.1935 whether it is tapped or
        not, so matching only the thread nominal made ordinary drawings untappable."""
        result = generate(self._job(scope={'purpose': 'tap'}))
        self.assertTrue(result.success, result.errors)
        self.assertTrue(any('TAP DRILL' in w for w in result.warnings), result.warnings)


class TestPeckCyclesAreExpanded(unittest.TestCase):
    """Drilling is written as explicit moves, not a G83 canned cycle. G81-G89 are not in
    GRBL 1.1 - which docs/ASSUMPTIONS.md lists as a target controller - and every consumer
    of the G-code (cycle time, 3D preview, heightmap simulator) only understands
    G0/G1/G2/G3, so a canned cycle was invisible to all of them at once."""

    def _drilled(self, holes=6):
        return generate(build_job(
            material='aluminum',
            tools=[Tool(1, '#10 drill', 0.1935, 2, type='drill'), Tool(2, 'em', 0.25, 2)],
            parts=[PartOps(dxf_path=make_hole_dxf(0.1935, holes), name='p', operations=[
                Operation('holes', 1, 'Drill'), Operation('perimeter', 2)])]))

    def test_no_canned_cycles_are_emitted(self):
        result = self._drilled()
        self.assertTrue(result.success, result.errors)
        for line in result.gcode.splitlines():
            code = line.split(';')[0].split('(')[0]
            for word in code.split():
                self.assertFalse(word.startswith('G8'),
                                 f'canned cycle word {word} is not supported on GRBL')

    def test_drilling_is_counted_in_the_cycle_time(self):
        """A G83 matched none of G0/G1/G2/G3, so the estimator scored it as zero seconds
        and a plate full of holes under-reported by the whole drilling operation."""
        drilled = self._drilled(holes=12)
        bare = generate(build_job(
            material='aluminum', tools=[Tool(2, 'em', 0.25, 2)],
            parts=[PartOps(dxf_path=make_bare_dxf(), name='p',
                           operations=[Operation('perimeter', 2)])]))
        self.assertGreater(drilled.stats['cycle_time_seconds'],
                           bare.stats['cycle_time_seconds'])
        self.assertGreater(drilled.stats['cutting_time'], '0')

    def test_every_drilling_move_is_visible_to_the_preview(self):
        """The viewer matches /^(G[0-3])/, so the moves have to be plain G0/G1."""
        result = self._drilled()
        lines = result.gcode.splitlines()
        start = next(i for i, l in enumerate(lines) if '===== DRILLING' in l)
        # The section ends at the next block header OR the tool change, whichever comes
        # first - the tool-change block legitimately contains M5/M0/S words that are not
        # motion and are not this test's business.
        end = next(i for i, l in enumerate(lines[start + 1:], start + 1)
                   if l.startswith('(===== ') or 'TOOL CHANGE' in l)
        motion = [l for l in lines[start + 1:end]
                  if l and not l.startswith('(') and not l.startswith(';')]
        self.assertTrue(motion)
        for line in motion:
            self.assertRegex(line, r'^G[0-3]\b', line)

    def test_the_pecks_reach_the_full_depth_and_only_go_downward(self):
        result = self._drilled(holes=1)
        cuts = [float(t[1:]) for l in result.gcode.splitlines() for t in l.split()
                if l.startswith('G1') and t.startswith('Z')]
        drill_cuts = [z for z in cuts if z < 0.25]
        self.assertTrue(drill_cuts)
        expected = -0.008 - FRCPostProcessor.drill_point_length(0.1935, 118.0)
        # Compared at the precision the G-code is actually written to (4 dp).
        self.assertAlmostEqual(min(drill_cuts), expected, places=4)

    def test_each_peck_retracts_to_clear_chips(self):
        result = self._drilled(holes=1)
        self.assertIn('Retract to clear chips', result.gcode)
        self.assertIn('Rapid back to just above the last peck', result.gcode)

    def test_the_heightmap_simulator_sees_the_holes(self):
        import gcode_sim
        result = self._drilled(holes=3)
        moves = gcode_sim.parse_moves(result.gcode)
        axial = [m for m in moves
                 if m[0] == 'feed' and abs(m[1] - m[4]) < 1e-9
                 and abs(m[2] - m[5]) < 1e-9 and m[6] < m[3]]
        self.assertTrue(axial, 'simulator sees no axial drilling moves')
        self.assertLess(min(m[6] for m in axial), 0.0)   # through the stock


def make_mixed_dxf():
    """A realistic plate: two drillable hole sizes, one bore too big to drill, a pocket."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (8, 0), (8, 5), (0, 5)], close=True)
    for xy in [(1, 1), (7, 1), (1, 4), (7, 4)]:
        msp.add_circle(xy, 0.1935 / 2)
    for xy in [(2, 2.5), (6, 2.5)]:
        msp.add_circle(xy, 0.257 / 2)
    msp.add_circle((4, 2.5), 1.125 / 2)
    msp.add_lwpolyline([(3, 3.6), (5, 3.6), (5, 4.4), (3, 4.4)], close=True)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


class TestWholePlanSuggestion(unittest.TestCase):
    """`default_operations` can only assign work to tools that already exist, which puts
    the user in the wrong order: asked to plan a part with tools they have not chosen,
    and told what it needs afterwards. `suggest_tooling` starts from the geometry."""

    def _survey(self, dxf=None):
        job = build_job(tools=[Tool(1, 'seed', 0.157, 1)], parts=[PartOps(
            dxf_path=dxf or make_mixed_dxf(), name='p',
            operations=[Operation('perimeter', 1)])])
        with redirect_stdout(io.StringIO()):
            return tooling.survey_part(job, job.parts[0])

    def test_it_proposes_tools_as_well_as_operations(self):
        plan = tooling.suggest_tooling(self._survey(), mill_diameter=0.25)
        self.assertTrue(plan['tools'])
        self.assertTrue(plan['operations'])

    def test_each_hole_size_gets_the_right_drill(self):
        plan = tooling.suggest_tooling(self._survey(), mill_diameter=0.25)
        drills = {t.diameter for t in plan['tools'] if t.type == 'drill'}
        self.assertIn(0.1935, drills)       # #10
        self.assertIn(0.2570, drills)       # F

    def test_a_bore_too_big_to_drill_goes_to_the_end_mill(self):
        plan = tooling.suggest_tooling(self._survey(), mill_diameter=0.25)
        drills = {round(t.diameter, 4) for t in plan['tools'] if t.type == 'drill'}
        self.assertNotIn(1.125, drills)
        self.assertTrue(any('large' in o.label.lower() for o in plan['operations']))
        self.assertTrue(any('bored with the end mill' in n for n in plan['notes']))

    def test_it_proposes_a_profile_and_pocket_pass(self):
        plan = tooling.suggest_tooling(self._survey(), mill_diameter=0.25)
        kinds = {o.op_type for o in plan['operations']}
        self.assertIn('perimeter', kinds)
        self.assertIn('pockets', kinds)

    def test_the_chamfer_is_opt_in(self):
        features = self._survey()
        without = tooling.suggest_tooling(features, mill_diameter=0.25)
        with_chamfer = tooling.suggest_tooling(features, mill_diameter=0.25,
                                               include_chamfer=True)
        self.assertNotIn('chamfer', {o.op_type for o in without['operations']})
        self.assertIn('chamfer', {o.op_type for o in with_chamfer['operations']})
        self.assertTrue(any(t.type == 'vbit' for t in with_chamfer['tools']))

    def test_the_suggested_plan_generates_with_no_edits(self):
        """The whole point: a plan the user has to fix before it works is not a plan."""
        dxf = make_mixed_dxf()
        features = self._survey(dxf)
        plan = tooling.suggest_tooling(features, mill_diameter=0.25)
        result = generate(build_job(
            material='aluminum', tools=plan['tools'],
            parts=[PartOps(dxf_path=dxf, name='p', operations=plan['operations'])]))
        self.assertTrue(result.success, result.errors)

    def test_it_covers_every_feature(self):
        """A suggestion that leaves a hole uncut would fail the coverage check."""
        dxf = make_mixed_dxf()
        features = self._survey(dxf)
        plan = tooling.suggest_tooling(features, mill_diameter=0.25)
        errors, _ = tooling._validate_feature_coverage(
            PartOps(dxf_path=dxf, name='p', operations=plan['operations']), features)
        self.assertEqual(errors, [])

    def test_a_bare_plate_gets_just_a_profile(self):
        features = self._survey(make_bare_dxf())
        plan = tooling.suggest_tooling(features, mill_diameter=0.25)
        self.assertEqual([o.op_type for o in plan['operations']], ['perimeter'])
        self.assertEqual(len(plan['tools']), 1)

    def test_the_route_returns_the_plan(self):
        from frc_cam_gui_app import app
        app.config['TESTING'] = True
        with open(make_mixed_dxf(), 'rb') as fh:
            data = fh.read()
        response = app.test_client().post('/part-features', data={
            'file': (io.BytesIO(data), 'p.dxf'),
            'thickness': '0.25', 'material': 'aluminum', 'mill_diameter': '0.25',
            'tools': json.dumps([{'slot': 1, 'name': 's', 'diameter': 0.157, 'flutes': 1}]),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200)
        plan = response.get_json()['suggested_plan']
        self.assertTrue(plan['tools'])
        self.assertTrue(plan['operations'])


class TestGeneratedProgram(unittest.TestCase):
    """End-to-end structure of a real multi-tool program."""

    @classmethod
    def setUpClass(cls):
        cls.result = generate(build_job())
        assert cls.result.success, cls.result.errors
        cls.lines = cls.result.gcode.splitlines()

    def test_pauses_are_exactly_tool_changes_plus_the_fixturing_stop(self):
        # Match the block header, not the header comment that mentions tool changes.
        changes = [l for l in self.lines if '=== TOOL CHANGE' in l]
        pauses = [l for l in self.lines if l.startswith('M0')]
        self.assertEqual(len(changes), self.result.stats['tool_changes'])
        fixtures = [l for l in self.lines if '=== PAUSE FOR FIXTURING' in l]
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(len(pauses), len(changes) + len(fixtures))

    def test_tool_change_stops_the_spindle_and_restarts_it(self):
        text = self.result.gcode
        change_at = text.index('=== TOOL CHANGE')
        next_change = text.find('=== TOOL CHANGE', change_at + 1)
        block = text[change_at:next_change if next_change >= 0 else len(text)]
        self.assertIn('M5', block)                     # spindle off before hands go in
        self.assertIn('M0', block)                     # wait for CYCLE START
        self.assertIn('Re-zero G54 Z', block)          # the new tool has a new length
        self.assertIn('Do NOT change the X or Y zero', block)
        self.assertIn('M3', block.split('M0', 1)[1])   # spindle back on after the pause

    def test_each_tool_change_has_a_complete_resume_checkpoint(self):
        checkpoints = [l for l in self.lines if '=== RESUME CHECKPOINT' in l]
        self.assertEqual(len(checkpoints), self.result.stats['tool_changes'])
        self.assertEqual(len(checkpoints), len(set(checkpoints)))
        for checkpoint in checkpoints:
            after = self.result.gcode.split(checkpoint, 1)[1][:600]
            self.assertIn('G90 G94 G91.1 G40 G49 G17', after)
            self.assertIn('G20', after)
            self.assertIn('G92.1', after)
            self.assertIn('G54', after)
            self.assertIn('Safe Z before resumed XY motion', after)
            self.assertIn('M3', after)

    def test_standalone_resume_files_are_safe_tail_programs(self):
        programs = build_resume_programs(self.result.gcode, self.result.filename)
        self.assertEqual(len(programs), self.result.stats['tool_changes'])
        for program in programs:
            gcode = program['gcode']
            self.assertTrue(program['filename'].endswith(
                f"_RESUME_{program['checkpoint']}.nc"))
            self.assertLess(gcode.index('M0  ; Confirm standalone resume setup'),
                            gcode.index('=== RESUME CHECKPOINT'))
            self.assertIn('Reference or home the machine', gcode)
            self.assertIn('Verify G54 X and Y', gcode)
            self.assertIn('M30', gcode)

    def test_standalone_resume_turns_configured_coolant_off_before_pause(self):
        source = '\n'.join([
            '(Material: 6061 Aluminum)',
            'M7  ; Air on',
            '( === RESUME CHECKPOINT TC01 - test tool === )',
            'G90 G94 G91.1 G40 G49 G17',
            'G92.1',
            'G54',
            'M30',
        ])
        setup = build_resume_programs(source, 'test.nc')[0]['gcode'].split(
            'M0  ; Confirm standalone resume setup', 1)[0]
        self.assertLess(setup.index('M9  ; Keep coolant off during resume setup'),
                        setup.index('M5  ; Keep spindle stopped during resume setup'))

    def test_configured_tool_change_height_creates_wrench_clearance(self):
        cfg = TeamConfig({'machining': {'z_reference': {'tool_change_height': 2.0}}})
        result = generate(build_job(config=cfg))
        self.assertTrue(result.success, result.errors)
        for block in result.gcode.split('( === TOOL CHANGE')[1:]:
            before_pause = block.split('M0', 1)[0]
            self.assertIn('G0 Z2.0000  ; Safe Z clearance', before_pause)

    def test_aluminum_rechecks_the_new_tool_at_every_change_and_resume(self):
        result = generate(build_job(material='6063', machine_id='omio_x8'))
        self.assertTrue(result.success, result.errors)
        changes = result.gcode.split('( === TOOL CHANGE')[1:]
        self.assertTrue(changes)
        for block in changes:
            before_pause = block.split('M0', 1)[0]
            self.assertIn('Clean collet, minimize stickout, and verify low runout',
                          before_pause)
            self.assertIn('continuous directed air and a clear chip escape path',
                          before_pause)
            self.assertIn('lubricant or MQL is ready for 6063', before_pause)
        for program in build_resume_programs(result.gcode, result.filename):
            setup = program['gcode'].split('M0  ; Confirm standalone resume setup', 1)[0]
            self.assertIn('clean collet, low runout, continuous directed air', setup)
            self.assertIn('lubricant or MQL is ready for 6063', setup)

    def test_no_automatic_tool_change_codes(self):
        """These routers have no changer and no tool-length table; a T/M6 or G43 would be
        either ignored or actively wrong."""
        for line in self.lines:
            code = line.split(';')[0].split('(')[0]
            self.assertNotIn('M6', code)
            self.assertNotIn('G43', code)

    def test_header_lists_every_tool_used(self):
        header = '\n'.join(self.lines[:60])
        self.assertIn('MANUAL TOOL CHANGES REQUIRED', header)
        for tool in ('T1', 'T2', 'T3'):
            self.assertIn(tool, header)

    def test_tabs_are_removed_only_at_the_very_end(self):
        """Tab removal frees the part; anything cut afterwards would be cut on a loose
        part. Here a chamfer follows the profile, so removal must be held back."""
        removal = max(i for i, l in enumerate(self.lines) if 'TAB REMOVAL' in l)
        chamfer = max(i for i, l in enumerate(self.lines) if 'CHAMFER' in l)
        self.assertGreater(removal, chamfer)

    def test_program_ends_cleanly(self):
        tail = '\n'.join(self.lines[-8:])
        self.assertIn('M5', tail)
        self.assertIn('M30', tail)

    def test_comments_obey_the_controller_rules(self):
        """No nested parentheses, no square brackets inside comments, pure ASCII - the
        same rules the single-tool output is held to (see CLAUDE.md)."""
        for n, line in enumerate(self.lines, 1):
            line.encode('ascii')                       # raises if any character is not ASCII
            depth = maxdepth = 0
            for ch in line.split(';')[0]:
                if ch == '(':
                    depth += 1
                    maxdepth = max(maxdepth, depth)
                elif ch == ')':
                    depth -= 1
            self.assertLessEqual(maxdepth, 1, f"line {n} nests comments: {line}")
            in_comment = False
            for ch in line:
                if ch == '(':
                    in_comment = True
                elif ch == ')':
                    in_comment = False
                elif in_comment:
                    self.assertNotIn(ch, '[]', f"line {n} brackets a comment: {line}")

    def test_header_zmin_covers_the_deepest_cut_in_the_program(self):
        """The header is written by the first operation's post-processor, so a job that
        starts shallow must not advertise that shallow depth as the program's ZMIN."""
        job = build_job(parts=[PartOps(
            dxf_path=make_plate_dxf(), name='plate',
            operations=[Operation('pockets', 2, 'Shallow first', depth=0.05),
                        Operation('holes', 1, scope={'max_diameter': 0.4}),
                        Operation('holes', 2, scope={'min_diameter': 0.4}),
                        Operation('perimeter', 2)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        zmin_line = [l for l in result.gcode.splitlines() if l.startswith('(ZMIN:')][0]
        declared = float(zmin_line.split(':')[1].strip().strip('")'))
        cut_depths = [float(t[1:]) for line in result.gcode.splitlines()
                      for t in line.split()
                      if t.startswith('Z') and line.startswith(('G1', 'G2', 'G3'))]
        self.assertLessEqual(declared, min(cut_depths) + 1e-6)
        self.assertLess(declared, 0.05)                 # not the first operation's depth

    def test_stats_describe_the_program(self):
        self.assertEqual(self.result.stats['num_tools'], 3)
        self.assertEqual(self.result.stats['num_parts'], 1)
        self.assertTrue(self.result.stats['excludes_tool_change_time'])
        self.assertGreater(self.result.stats['cycle_time_seconds'], 0)


class TestPartialDepth(unittest.TestCase):
    def test_depth_leaves_a_floor(self):
        job = build_job(parts=[PartOps(
            dxf_path=make_plate_dxf(), name='plate',
            operations=[Operation('holes', 1, scope={'max_diameter': 0.4}),
                        Operation('holes', 2, scope={'min_diameter': 0.4}),
                        Operation('pockets', 2, 'Lightening', depth=0.125)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        body = result.gcode.split('LIGHTENING', 1)[1]
        depths = [float(t[1:]) for line in body.splitlines() for t in line.split()
                  if t.startswith('Z') and line.startswith(('G1', 'G2', 'G3'))]
        self.assertAlmostEqual(min(depths), 0.125, places=4)   # 0.25 stock - 0.125 deep

    def test_depth_past_the_stock_warns_and_cuts_through(self):
        job = build_job(parts=[PartOps(
            dxf_path=make_plate_dxf(), name='plate',
            operations=[Operation('holes', 1, scope={'max_diameter': 0.4}),
                        Operation('holes', 2, scope={'min_diameter': 0.4}),
                        Operation('pockets', 2, 'Too deep', depth=0.9)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        self.assertTrue(any('cutting through instead' in w for w in result.warnings),
                        result.warnings)


class TestEmptyOperations(unittest.TestCase):
    def test_an_operation_that_matches_nothing_still_warns(self):
        """Its body is dropped from the program, but silently dropping the warning with it
        would hide a mistyped size range."""
        job = build_job(parts=[PartOps(
            dxf_path=make_bare_dxf(), name='plate',
            operations=[Operation('pockets', 2, 'This plate has none'),
                        Operation('perimeter', 2)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        self.assertTrue(any('no features matched' in w for w in result.warnings),
                        result.warnings)

    def test_a_job_where_nothing_cuts_fails_with_an_explanation(self):
        job = build_job(parts=[PartOps(
            dxf_path=make_bare_dxf(), name='plate',
            operations=[Operation('pockets', 2, 'This plate has none')])])
        result = generate(job)
        self.assertFalse(result.success)
        self.assertTrue(any('scopes match features' in e for e in result.errors),
                        result.errors)


class TestMultiPartMultiTool(unittest.TestCase):
    def setUp(self):
        dxf = make_plate_dxf()
        self.job = build_job(parts=[
            PartOps(dxf_path=dxf, name='a', place_x=0, place_y=0, operations=standard_ops()),
            PartOps(dxf_path=dxf, name='b', place_x=7, place_y=0, operations=standard_ops()),
        ])
        self.result = generate(self.job)
        self.assertTrue(self.result.success, self.result.errors)

    def test_two_parts_still_cost_only_one_change_per_tool(self):
        # T1 -> T2 -> T3 -> back to T2 for the held-back tab removals.
        self.assertEqual(self.result.stats['tool_changes'], 3)
        self.assertEqual(self.result.stats['num_parts'], 2)

    def test_both_parts_tabs_survive_until_every_profile_is_cut(self):
        lines = self.result.gcode.splitlines()
        last_perimeter = max(i for i, l in enumerate(lines)
                             if 'PERIMETER WITH TABS' in l)
        first_removal = min(i for i, l in enumerate(lines) if 'TAB REMOVAL' in l)
        self.assertGreater(first_removal, last_perimeter)

    def test_each_part_is_labelled_in_the_program(self):
        self.assertIn('- a -', self.result.gcode)
        self.assertIn('- b -', self.result.gcode)


class TestSanitizeComment(unittest.TestCase):
    """Tool and part names come from students and from CAD, and land in G-code comments."""

    def test_strips_parens_and_brackets(self):
        self.assertNotIn('(', sanitize_comment('endmill (carbide)'))
        self.assertNotIn('[', sanitize_comment('endmill [spare]'))

    def test_transliterates_then_drops_non_ascii(self):
        out = sanitize_comment('45° chamfer → “finish” – café')
        out.encode('ascii')
        self.assertIn('deg', out)

    def test_falls_back_when_nothing_survives(self):
        self.assertEqual(sanitize_comment('()', 'tool'), 'tool')
        self.assertEqual(sanitize_comment('', 'tool'), 'tool')


class TestJobFromDict(unittest.TestCase):
    def test_round_trips_the_wire_format(self):
        dxf = make_plate_dxf()
        spec = {
            'material': 'plywood', 'thickness': 0.25, 'name': 'wire',
            'tools': [{'slot': 1, 'name': 'small', 'diameter': 0.125, 'flutes': 1},
                      {'slot': 2, 'name': 'big', 'diameter': 0.25, 'flutes': 1}],
            'parts': [{'file_index': 0, 'name': 'plate', 'operations': [
                {'op_type': 'holes', 'tool_slot': 1, 'scope': {'max_diameter': 0.4}},
                {'op_type': 'holes', 'tool_slot': 2, 'scope': {'min_diameter': 0.4}},
                {'op_type': 'perimeter', 'tool_slot': 2},
            ]}],
        }
        job = tooling.job_from_dict(spec, {0: dxf})
        self.assertEqual(len(job.parts[0].operations), 3)
        self.assertEqual(job.parts[0].operations[0].scope['max_diameter'], 0.4)

    def test_missing_dxf_is_reported(self):
        with self.assertRaises(ToolingError):
            tooling.job_from_dict({'parts': [{'file_index': 3}],
                                   'tools': [{'slot': 1, 'name': 'a', 'diameter': 0.25}]}, {})


class TestSuggestionReusesLoadedTools(unittest.TestCase):
    """One suggestion path, and it works with what is already in the spindle rack.
    Previously a second, tool-constrained suggester auto-filled the operation list, so a
    part surveyed with only a default end mill silently got a plan that milled every
    hole while the better answer sat behind a button."""

    def _features(self, dxf=None):
        job = build_job(tools=[Tool(1, 'seed', 0.157, 1)], parts=[PartOps(
            dxf_path=dxf or make_mixed_dxf(), name='p',
            operations=[Operation('perimeter', 1)])])
        with redirect_stdout(io.StringIO()):
            return tooling.survey_part(job, job.parts[0])

    def test_an_end_mill_already_loaded_is_reused(self):
        loaded = [Tool(1, 'my 1/4 endmill', 0.25, 2)]
        plan = tooling.suggest_tooling(self._features(), available=loaded)
        self.assertNotIn('endmill', [t.type for t in plan['tools']])
        self.assertIn(loaded[0], plan['reused'])
        milling = [o for o in plan['operations'] if o.op_type in ('pockets', 'perimeter')]
        self.assertTrue(milling)
        for op in milling:
            self.assertEqual(op.tool_slot, 1)

    def test_a_drill_already_loaded_is_reused(self):
        loaded = [Tool(1, '#10 drill', 0.1935, 2, type='drill'),
                  Tool(2, 'endmill', 0.25, 2)]
        plan = tooling.suggest_tooling(self._features(), available=loaded)
        proposed = {round(t.diameter, 4) for t in plan['tools']}
        self.assertNotIn(0.1935, proposed)          # already have it
        self.assertIn(0.2570, proposed)             # the F drill is still missing

    def test_new_tools_never_collide_with_loaded_slots(self):
        loaded = [Tool(1, 'a', 0.125, 1), Tool(2, 'b', 0.25, 2), Tool(5, 'c', 0.5, 2)]
        plan = tooling.suggest_tooling(self._features(), available=loaded)
        taken = {t.slot for t in loaded}
        for tool in plan['tools']:
            self.assertNotIn(tool.slot, taken, 'proposed a slot already in use')
            taken.add(tool.slot)

    def test_with_nothing_loaded_it_proposes_the_whole_set(self):
        plan = tooling.suggest_tooling(self._features(), available=[])
        kinds = [t.type for t in plan['tools']]
        self.assertIn('drill', kinds)
        self.assertIn('endmill', kinds)

    def test_a_reused_plan_still_generates(self):
        dxf = make_mixed_dxf()
        loaded = [Tool(1, 'my endmill', 0.25, 2)]
        plan = tooling.suggest_tooling(self._features(dxf), available=loaded)
        result = generate(build_job(
            material='aluminum', tools=loaded + plan['tools'],
            parts=[PartOps(dxf_path=dxf, name='p', operations=plan['operations'])]))
        self.assertTrue(result.success, result.errors)


class TestSuggestionCoverage(unittest.TestCase):
    def _plan(self, available=None):
        job = build_job()
        with redirect_stdout(io.StringIO()):
            features = tooling.survey_part(job, job.parts[0])
        return features, tooling.suggest_tooling(
            features, available=available if available is not None else job.tools,
            mill_diameter=0.25)

    def test_every_hole_is_claimed_exactly_once(self):
        features, plan = self._plan()
        claimed = []
        for op in plan['operations']:
            if op.op_type in ('holes', 'interior'):
                claimed.extend(tooling.selected_hole_keys(features, op.scope))
        self.assertEqual(len(claimed), len(set(claimed)))          # nothing cut twice
        self.assertEqual(set(claimed), {h['key'] for h in features['holes']})

    def test_the_profile_runs_last(self):
        """The part stays anchored for as long as possible."""
        _, plan = self._plan()
        self.assertEqual(plan['operations'][-1].op_type, 'perimeter')


# -------------------------------------------------------------------- routes

class TestMultiToolRoutes(unittest.TestCase):
    def setUp(self):
        from frc_cam_gui_app import app
        app.config['TESTING'] = True
        self.client = app.test_client()
        path = make_plate_dxf()
        with open(path, 'rb') as fh:
            self.dxf = fh.read()
        os.remove(path)
        self.tools = [
            {'slot': 1, 'name': 'small', 'diameter': 0.125, 'flutes': 1, 'type': 'endmill'},
            {'slot': 2, 'name': 'big', 'diameter': 0.250, 'flutes': 1, 'type': 'endmill'},
        ]

    def test_presets_route(self):
        r = self.client.get('/api/tooling/presets')
        self.assertEqual(r.status_code, 200)
        self.assertIn('4mm_1f', r.get_json()['tools'])

    def test_part_features_reports_the_geometry(self):
        r = self.client.post('/part-features', data={
            'file': (io.BytesIO(self.dxf), 'plate.dxf'),
            'thickness': '0.25', 'material': 'plywood',
            'tools': json.dumps(self.tools),
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['features']['hole_sizes'], [0.196, 0.75])
        self.assertTrue(body['suggested_plan']['operations'])
        # Internal identity keys must not leak into the API surface.
        self.assertNotIn('key', body['features']['holes'][0])

    def test_part_features_rejects_a_non_dxf(self):
        r = self.client.post('/part-features', data={
            'file': (io.BytesIO(b'nope'), 'notes.txt'),
            'tools': json.dumps(self.tools),
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def _post_job(self, operations, parts=None):
        parts = parts or [{'file_index': 0, 'name': 'plate', 'place_x': 0, 'place_y': 0,
                           'rotation': 0, 'operations': operations}]
        job = {'material': 'plywood', 'thickness': 0.25, 'tab_spacing': 6.0,
               'name': 'routetest', 'tools': self.tools, 'parts': parts}
        return self.client.post('/process-multitool', data={
            'file_0': (io.BytesIO(self.dxf), 'plate.dxf'),
            'job': json.dumps(job), 'timestamp': '2026-08-20 12:00:00',
        }, content_type='multipart/form-data')

    def test_generates_a_program(self):
        r = self._post_job([
            {'op_type': 'holes', 'tool_slot': 1, 'scope': {'max_diameter': 0.4}},
            {'op_type': 'holes', 'tool_slot': 2, 'scope': {'min_diameter': 0.4}},
            {'op_type': 'pockets', 'tool_slot': 2},
            {'op_type': 'perimeter', 'tool_slot': 2},
        ])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['tool_changes'], 1)
        self.assertIn('M30', body['gcode'])
        self.assertEqual(len(body['tools']), 2)
        self.assertEqual(len(body['restart_files']), 1)
        resume = body['restart_files'][0]
        self.assertEqual(resume['checkpoint'], 'TC01')
        downloaded = self.client.get('/download/' + resume['filename'])
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn(b'PENGUINCAM STANDALONE RESUME PROGRAM', downloaded.data)
        self.assertIn(b'G90 G94 G91.1 G40 G49 G17', downloaded.data)
        self.assertIn(b'G92.1', downloaded.data)
        bundle = self.client.get('/download/' + body['restart_bundle']['filename'])
        self.assertEqual(bundle.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(bundle.data)) as archive:
            names = archive.namelist()
            self.assertIn(body['filename_display'], names)
            self.assertIn(resume['filename_display'], names)

    def test_reports_plan_errors_as_part_errors(self):
        r = self._post_job([{'op_type': 'holes', 'tool_slot': 2,
                             'scope': {'min_diameter': 0.4}},
                            {'op_type': 'perimeter', 'tool_slot': 2}])
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertFalse(body['success'])
        self.assertTrue(body['part_errors'])

    def test_rejects_a_malformed_job(self):
        r = self.client.post('/process-multitool', data={
            'file_0': (io.BytesIO(self.dxf), 'plate.dxf'),
            'job': 'not json',
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def test_overlapping_parts_are_rejected_using_the_widest_tool(self):
        """The layout gap is checked against the largest cutter in the job, since that is
        the one that reaches furthest into a neighbouring part."""
        ops = [{'op_type': 'holes', 'tool_slot': 1, 'scope': {'max_diameter': 0.4}},
               {'op_type': 'holes', 'tool_slot': 2, 'scope': {'min_diameter': 0.4}},
               {'op_type': 'perimeter', 'tool_slot': 2}]
        parts = [{'file_index': 0, 'name': 'a', 'place_x': 0, 'place_y': 0,
                  'rotation': 0, 'operations': ops},
                 {'file_index': 0, 'name': 'b', 'place_x': 3, 'place_y': 0,
                  'rotation': 0, 'operations': ops}]
        r = self._post_job(ops, parts=parts)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.get_json()['part_errors'])


class TestLocalMode(unittest.TestCase):
    def setUp(self):
        import local_mode
        self.local_mode = local_mode
        self._saved = {k: os.environ.get(k) for k in
                       (local_mode.LOCAL_ENV_VAR, local_mode.CONFIG_ENV_VAR)}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_flag_parsing(self):
        for value, expected in (('1', True), ('true', True), ('YES', True),
                                ('0', False), ('', False), ('off', False)):
            os.environ[self.local_mode.LOCAL_ENV_VAR] = value
            self.assertEqual(self.local_mode.is_local_mode(), expected, value)

    def test_a_team_suffixed_config_is_discovered(self):
        """Teams rename the file to say whose it is - PenguinCAM-config-2129.yaml. Before
        the pattern match, renaming it silently disabled discovery and the machine ran on
        built-in defaults with nothing on screen to say so."""
        import glob
        import os
        import tempfile
        original = os.getcwd()
        workdir = tempfile.mkdtemp()
        try:
            os.chdir(workdir)
            with open('PenguinCAM-config-2129.yaml', 'w', encoding='utf-8') as fh:
                fh.write('team:\n  number: 2129\n  name: "Ultraviolet"\n')
            found = self.local_mode.find_local_config_path()
            self.assertTrue(found.endswith('PenguinCAM-config-2129.yaml'), found)
            with self.assertLogs('penguincam', level='INFO'):
                config, _ = self.local_mode.load_local_team_config()
            self.assertEqual(config.team_number, 2129)
            self.assertEqual(config.team_name, 'Ultraviolet')
        finally:
            os.chdir(original)
            for leftover in glob.glob(os.path.join(workdir, '*')):
                os.remove(leftover)
            os.rmdir(workdir)

    def test_the_plain_filename_still_wins_over_a_suffixed_one(self):
        """Precedence has to be stable, or which team's feeds you get depends on
        directory listing order."""
        import glob
        import os
        import tempfile
        original = os.getcwd()
        workdir = tempfile.mkdtemp()
        try:
            os.chdir(workdir)
            for name, number in (('PenguinCAM-config.yaml', 1111),
                                 ('PenguinCAM-config-2129.yaml', 2129)):
                with open(name, 'w', encoding='utf-8') as fh:
                    fh.write(f'team:\n  number: {number}\n  name: "T"\n')
            self.assertTrue(self.local_mode.find_local_config_path()
                            .endswith('PenguinCAM-config.yaml'))
        finally:
            os.chdir(original)
            for leftover in glob.glob(os.path.join(workdir, '*')):
                os.remove(leftover)
            os.rmdir(workdir)

    def test_missing_explicit_config_is_reported_not_silently_ignored(self):
        """Falling back to defaults on a typo'd path would run the machine on someone
        else's feeds without saying so."""
        os.environ[self.local_mode.CONFIG_ENV_VAR] = os.path.join(
            tempfile.gettempdir(), 'definitely-not-here.yaml')
        with self.assertLogs('penguincam', level='INFO') as logged:
            self.assertEqual(self.local_mode.find_local_config_path(), '')
        self.assertIn('does not exist', '\n'.join(logged.output))

    def test_loads_a_config_file(self):
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False,
                                         encoding='utf-8') as fh:
            fh.write('team:\n  number: 9999\n  name: Test Team\n')
            path = fh.name
        try:
            os.environ[self.local_mode.CONFIG_ENV_VAR] = path
            with self.assertLogs('penguincam', level='INFO'):
                config, found = self.local_mode.load_local_team_config()
            self.assertEqual(found, path)
            self.assertEqual(config.team_number, 9999)
        finally:
            os.remove(path)

    def test_malformed_config_falls_back_to_defaults(self):
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False,
                                         encoding='utf-8') as fh:
            fh.write('team: [unclosed\n')
            path = fh.name
        try:
            os.environ[self.local_mode.CONFIG_ENV_VAR] = path
            with self.assertLogs('penguincam', level='INFO') as logged:
                config, found = self.local_mode.load_local_team_config()
            self.assertIsNone(config)
            self.assertIn('using built-in defaults', '\n'.join(logged.output))
        finally:
            os.remove(path)


if __name__ == '__main__':
    unittest.main()


class TestSpindleSpeedFollowsTheOperation(unittest.TestCase):
    """One tool, several operations, several derived RPMs - and only the first was ever
    commanded.

    The feeds model quotes a different spindle speed per operation (a slot, a pocket and
    a profile are not the same cut), and the section header prints the number it derived.
    But S is only emitted at a tool change, so the pockets body announced 12000 RPM and
    fed F30 while the spindle was still turning 9320 - 29% over the chipload the aluminum
    guard had validated. Reversed, the same gap lands in the rubbing regime.
    """

    def _job(self):
        return build_job(
            material='aluminum', thickness=0.25,
            tools=[Tool(1, '1/8 in 1-flute endmill', 0.125, 1)],
            parts=[PartOps(dxf_path=make_plate_dxf(), name='plate', operations=[
                Operation('holes', 1), Operation('pockets', 1),
                Operation('perimeter', 1)])])

    def test_each_body_runs_at_the_rpm_it_announces(self):
        result = generate(self._job())
        self.assertTrue(result.success, result.errors)

        commanded = None
        for line in result.gcode.splitlines():
            code = line.split('(')[0].split(';')[0].strip()
            spoken = re.match(r'^S(\d+)\b', code)
            if spoken:
                commanded = int(spoken.group(1))
                continue
            announced = re.search(r'\(Tool [\d.]+ in diameter, feed [\d.]+ ipm, '
                                  r'spindle (\d+) rpm\)', line)
            if announced:
                self.assertEqual(
                    commanded, int(announced.group(1)),
                    f"the section claims {announced.group(1)} RPM but the spindle was "
                    f"last commanded {commanded}: {line}")

    def test_a_changed_speed_is_given_time_to_settle(self):
        result = generate(self._job())
        lines = result.gcode.splitlines()
        for i, line in enumerate(lines):
            if re.match(r'^S\d+\s*$', line.split(';')[0].strip()):
                self.assertTrue(
                    any(l.strip().startswith('G4 P') for l in lines[i:i + 3]),
                    f'no dwell after the spindle change at line {i + 1}')

    def test_one_speed_for_the_whole_job_emits_no_extra_s_words(self):
        """Nothing changed means nothing to re-issue - no noise in the common case."""
        doc = ezdxf.new('R2010')
        doc.modelspace().add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        job = build_job(
            tools=[Tool(1, '1/8 in 1-flute endmill', 0.125, 1)],
            parts=[PartOps(dxf_path=path, name='plate',
                           operations=[Operation('perimeter', 1)])])
        result = generate(job)
        self.assertTrue(result.success, result.errors)
        speeds = [l for l in result.gcode.splitlines()
                  if re.match(r'^S\d+', l.split(';')[0].strip())]
        self.assertEqual(len(speeds), 1, speeds)


class TestTapDrillTolerance(unittest.TestCase):
    """A tap drill is not a size you round to.

    Tap acceptance shared the clearance-snap tolerance, and at +/-0.010 a 10-32 accepted
    #25 (0.1495) through #19 (0.1660) - five drill sizes. The wrong end strips the
    threads, the other end breaks the tap. drill_sizes.tap_drill_for itself works to
    0.002; acceptance now matches it, and the job-level drill_size_tolerance - which is
    legitimately widened by shops that stock fractional drills only - cannot widen tap
    acceptance past 0.003.
    """

    #: 10-32: nominal 0.1900, tap drill #21 = 0.1590.
    TEN_THIRTYTWO = 0.1900

    def _plan(self, drill, tolerance=None):
        scope = {'purpose': 'tap'}
        if tolerance is not None:
            scope['size_tolerance'] = tolerance
        job = drill_job(self.TEN_THIRTYTWO, drill, **scope)
        with redirect_stdout(io.StringIO()):
            return tooling.generate_multitool_job(job, timestamp='2026-08-20 12:00:00')

    def test_the_right_tap_drills_are_accepted(self):
        # 0.190 is the nominal of BOTH 10-24 and 10-32, so #25 and #21 are each
        # correct for one of them; the note says which and flags the ambiguity.
        for drill, name in ((0.1590, '#21'), (0.1495, '#25')):
            with self.subTest(drill=name):
                result = self._plan(drill)
                self.assertTrue(result.success, result.errors)
                self.assertTrue(any('TAP DRILL' in w for w in result.warnings),
                                result.warnings)

    def test_a_drill_that_is_no_thread_s_tap_drill_is_refused(self):
        # Both sit inside the old +/-0.010 window around #21 or #25 and are neither.
        for drill, name in ((0.1660, '#19'), (0.1540, '#23')):
            with self.subTest(drill=name):
                result = self._plan(drill)
                self.assertFalse(result.success,
                                 f'{name} was accepted as a 10-32/10-24 tap drill')
                self.assertTrue(any('tap' in e.lower() for e in result.errors),
                                result.errors)

    def test_a_widened_job_tolerance_cannot_widen_tap_acceptance(self):
        """0.010 is a legitimate CLEARANCE tolerance. It is not a tap tolerance."""
        result = self._plan(0.1660, tolerance=0.010)
        self.assertFalse(result.success, 'a widened tolerance let #19 through')

    def test_the_tap_tolerance_constant_is_tight(self):
        self.assertLessEqual(tooling.TAP_DRILL_TOLERANCE, 0.002)
        self.assertLessEqual(tooling.MAX_TAP_DRILL_TOLERANCE, 0.003)

    def test_clearance_holes_keep_their_wider_tolerance(self):
        """The snap tolerance exists for a reason and this must not narrow it."""
        job = drill_job(0.1960, 0.1935, purpose='clearance', size_tolerance=0.010)
        with redirect_stdout(io.StringIO()):
            result = tooling.generate_multitool_job(job, timestamp='2026-08-20 12:00:00')
        self.assertTrue(result.success, result.errors)


class TestSpotDrillCoverage(unittest.TestCase):
    """A spot drill does not make the hole - it marks where the hole goes.

    Coverage counted a spot op as having "cut" a hole, so a plan of just
    [spot, perimeter] passed in silence and shipped a plate of dimples. And because it
    counted, spot + drill on the same holes - the documented workflow - was rejected as a
    double claim that "would be cut twice".
    """

    HOLE = 0.1935

    def _job(self, ops, tools=None):
        return build_job(
            tools=tools or [Tool(1, 'centre drill', 0.125, 2, type='drill'),
                            Tool(2, '#10 drill', self.HOLE, 2, type='drill'),
                            Tool(3, '1/4 endmill', 0.25, 2)],
            parts=[PartOps(dxf_path=make_hole_dxf(self.HOLE, 2), name='plate',
                           operations=ops)])

    def test_spot_then_drill_is_legal(self):
        result = generate(self._job([
            Operation('holes', 1, 'Spot', scope={'purpose': 'spot'}),
            Operation('holes', 2, 'Drill'),
            Operation('perimeter', 3)]))
        self.assertTrue(result.success, result.errors)
        self.assertIn('CENTRE DRILLING', result.gcode)
        self.assertIn('DRILLING', result.gcode)

    def test_spot_alone_warns_that_nothing_drilled_them(self):
        result = generate(self._job([
            Operation('holes', 1, 'Spot', scope={'purpose': 'spot'}),
            Operation('perimeter', 3)]))
        self.assertTrue(result.success, result.errors)
        joined = ' '.join(result.warnings).lower()
        self.assertIn('spot', joined)
        self.assertIn('drill press', joined, result.warnings)

    def test_a_hole_no_operation_touches_is_still_an_error(self):
        """Two holes; both ops take only the first. Nothing claims the second at
        all, spot included, so it is missing rather than merely undrilled."""
        result = generate(self._job([
            Operation('holes', 1, 'Spot',
                      scope={'purpose': 'spot', 'indices': [0]}),
            Operation('holes', 2, 'Drill', scope={'indices': [0]}),
            Operation('perimeter', 3)]))
        self.assertFalse(result.success)
        self.assertTrue(any('not cut by any operation' in e for e in result.errors),
                        result.errors)

    def test_two_real_drill_ops_on_one_hole_is_still_an_error(self):
        result = generate(self._job([
            Operation('holes', 2, 'Drill'),
            Operation('holes', 2, 'Drill again'),
            Operation('perimeter', 3)]))
        self.assertFalse(result.success)
        self.assertTrue(any('more than one' in e for e in result.errors), result.errors)

    def test_two_spot_ops_on_one_hole_is_not_a_double_claim(self):
        """Spotting twice is pointless but harmless; it is not "cut twice"."""
        result = generate(self._job([
            Operation('holes', 1, 'Spot', scope={'purpose': 'spot'}),
            Operation('holes', 1, 'Spot again', scope={'purpose': 'spot'}),
            Operation('holes', 2, 'Drill'),
            Operation('perimeter', 3)]))
        self.assertTrue(result.success, result.errors)


class TestProfileOrderWithTabsLeftIn(unittest.TestCase):
    """`tabs_enabled=True, remove_tabs=False` means the machine cuts tabs and LEAVES
    them: the deferral in generate_operation is gated on remove_tabs, so no removal pass
    is ever emitted and the part stays anchored until someone cuts it out by hand.

    The order check refused that plan anyway, telling the operator the part was "cut free
    and left loose on the table" - which is the opposite of what happens.
    """

    LEAVE_TABS = TeamConfig(
        {'machining': {'tabs': {'enabled': True, 'remove_tabs': False}}})
    NO_TABS = TeamConfig({'machining': {'tabs': {'enabled': False}}})

    def _job(self, config, parts=None):
        return build_job(config=config, parts=parts or [
            PartOps(dxf_path=make_plate_dxf(), name='plate', operations=[
                Operation('holes', 1, scope={'max_diameter': 0.4}),
                Operation('holes', 2, scope={'min_diameter': 0.4}),
                Operation('pockets', 2), Operation('perimeter', 2),
                Operation('chamfer', 3,
                          scope={'targets': ['perimeter'], 'width': 0.02})])])

    def test_leaving_the_tabs_in_allows_work_after_the_profile(self):
        self.assertEqual(tooling._validate_profile_order(self._job(self.LEAVE_TABS)), [])

    def test_and_the_job_actually_generates(self):
        result = generate(self._job(self.LEAVE_TABS))
        self.assertTrue(result.success, result.errors)
        self.assertIn('CHAMFER', result.gcode)
        self.assertNotIn('TAB REMOVAL', result.gcode)

    def test_two_parts_are_fine_too_when_the_tabs_stay_in(self):
        dxf = make_plate_dxf()
        parts = [PartOps(dxf_path=dxf, name=f'p{i}', operations=[
            Operation('holes', 1, scope={'max_diameter': 0.4}),
            Operation('holes', 2, scope={'min_diameter': 0.4}),
            Operation('pockets', 2), Operation('perimeter', 2)]) for i in (1, 2)]
        self.assertEqual(
            tooling._validate_profile_order(self._job(self.LEAVE_TABS, parts)), [])

    def test_tabs_off_is_still_refused(self):
        """No tabs at all, and the profile really does free the part."""
        errors = tooling._validate_profile_order(self._job(self.NO_TABS))
        self.assertTrue(errors)
        self.assertIn('loose', ' '.join(errors))


class TestSmallestToolUsesTheJobTolerance(unittest.TestCase):
    """The survey loads with the smallest hole any tool could make, and a drill counts
    for slightly less than its diameter because a hole a few thou UNDER the drill is a
    stocking difference plan_drilled_holes resolves by snapping. That allowance read the
    DEFAULT tolerance rather than the job's, so a job that had narrowed the tolerance
    still surveyed as though it were wide - and one that widened it got no benefit.
    """

    def _job(self, tolerance):
        return build_job(
            tools=[Tool(1, '#10 drill', 0.1935, 2, type='drill'),
                   Tool(2, '1/4 endmill', 0.25, 2)],
            drill_size_tolerance=tolerance,
            parts=[PartOps(dxf_path=make_hole_dxf(0.1935), name='p', operations=[
                Operation('holes', 1), Operation('perimeter', 2)])])

    def test_a_narrowed_tolerance_narrows_the_allowance(self):
        self.assertAlmostEqual(self._job(0.001).smallest_tool_diameter,
                               0.1935 - 0.001, places=6)

    def test_a_widened_tolerance_widens_it(self):
        self.assertAlmostEqual(self._job(0.020).smallest_tool_diameter,
                               0.1935 - 0.020, places=6)

    def test_the_default_is_unchanged(self):
        self.assertAlmostEqual(
            self._job(tooling.DEFAULT_DRILL_SIZE_TOLERANCE).smallest_tool_diameter,
            0.1935 - tooling.DEFAULT_DRILL_SIZE_TOLERANCE, places=6)

    def test_an_end_mill_gets_no_allowance(self):
        job = build_job(
            tools=[Tool(1, '1/4 endmill', 0.25, 2)], drill_size_tolerance=0.020,
            parts=[PartOps(dxf_path=make_hole_dxf(0.5), name='p', operations=[
                Operation('holes', 1), Operation('perimeter', 1)])])
        self.assertAlmostEqual(job.smallest_tool_diameter, 0.25, places=6)


class TestFluteLengthWarning(unittest.TestCase):
    """A cutter's flute length is the depth it can actually reach. Nothing knew it, so a
    program could ask a stub-length bit for a 0.5" cut and only the shank found out.

    Warning, not refusal: PenguinCAM cannot see how far the tool sticks out of the
    collet, and a shop that grinds its own reach knows better than the program does.
    """

    def _job(self, thickness, flute_length=None, diameter=0.125):
        tool = Tool(1, 'endmill', diameter, 1, flute_length=flute_length)
        doc = ezdxf.new('R2010')
        doc.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        return build_job(thickness=thickness, tools=[tool],
                         parts=[PartOps(dxf_path=path, name='plate',
                                        operations=[Operation('perimeter', 1)])])

    def test_a_cut_deeper_than_the_flutes_warns(self):
        result = generate(self._job(0.5, flute_length=0.25))
        self.assertTrue(result.success, result.errors)
        joined = ' '.join(result.warnings).lower()
        self.assertIn('flute', joined)
        self.assertIn('0.25', ' '.join(result.warnings))
        self.assertIn('flute length', result.gcode.lower())

    def test_a_cut_inside_the_flutes_stays_quiet(self):
        result = generate(self._job(0.25, flute_length=1.0))
        self.assertTrue(result.success, result.errors)
        self.assertFalse([w for w in result.warnings if 'flute length' in w.lower()],
                         result.warnings)

    def test_without_a_stated_length_four_diameters_is_assumed(self):
        """A 1/8" bit cutting 0.75" deep is six diameters; something is worth saying."""
        result = generate(self._job(0.75))
        self.assertTrue(result.success, result.errors)
        joined = ' '.join(result.warnings).lower()
        self.assertIn('flute', joined)
        self.assertIn('4x', joined.replace(' ', ''))

    def test_a_shallow_cut_with_no_stated_length_stays_quiet(self):
        result = generate(self._job(0.25))
        self.assertFalse([w for w in result.warnings if 'flute' in w.lower()],
                         result.warnings)

    def test_the_field_is_validated_like_every_other_number(self):
        for bad in (0, -0.5, float('nan'), 'deep'):
            with self.subTest(bad=bad):
                with self.assertRaises(ToolingError):
                    Tool(1, 'endmill', 0.125, 1, flute_length=bad)

    def test_it_round_trips_through_a_dict(self):
        tool = Tool.from_dict({'slot': 1, 'name': 'em', 'diameter': 0.125,
                               'flutes': 1, 'flute_length': 0.75})
        self.assertAlmostEqual(tool.flute_length, 0.75)
        self.assertAlmostEqual(Tool.from_dict(tool.to_dict()).flute_length, 0.75)

    def test_a_saved_bit_can_carry_one(self):
        cfg = TeamConfig({'tools': [
            {'name': '1/8 stub', 'diameter': '1/8"', 'flutes': 1,
             'flute_length': '3/8"'}]})
        saved = cfg.saved_tools
        self.assertEqual(len(saved), 1, saved)
        self.assertAlmostEqual(saved[0]['flute_length'], 0.375)
