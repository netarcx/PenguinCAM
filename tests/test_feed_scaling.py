"""Feeds and depths scaled to the ACTUAL tool in the single-tool flows.

The material presets are the feeds/speeds model frozen at its 4 mm single-flute
reference tool. Before scale_feeds_to_tool, a 1/8 in end mill in aluminum ran at the
4 mm tool's 55 IPM into a 0.2 in deep full-width slot - over its scaled chipload and
its scaled depth at once, which is how small end mills snap.

The contract under test:
  - the 4 mm reference tool reproduces the tested preset EXACTLY (no output change);
  - smaller tools are derated by the model's diameter exponent (feed) and their
    diameter (depth per pass), and the program header says so;
  - nothing is ever scaled UP - a big cutter keeps the tested preset;
  - the derate reaches the actual G-code, not just the attributes.
"""
import io
import math
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import feeds_speeds
from frc_cam_postprocessor import FRCPostProcessor
from team_config import CONFIG_TEMPLATE, TeamConfig


def _pp(tool, material='aluminum', units='inch', machine=None, flutes=1, config=None):
    with redirect_stdout(io.StringIO()):
        pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=tool, units=units,
                              tool_flutes=flutes, config=config)
        pp.apply_material_preset(material, machine)
        notes = pp.scale_feeds_to_tool()
    return pp, notes


class TestScaleFeedsToTool(unittest.TestCase):
    def test_reference_tool_keeps_base_feed_and_depth(self):
        # The proven 4 mm base feed/depth stay fixed. The corner-only floor may rise so
        # those deliberately slower moves do not fall below minimum chipload.
        pp, notes = _pp(0.157)
        self.assertEqual(pp.feed_rate, 24.0)
        self.assertEqual(pp.max_slotting_depth, 0.04)
        self.assertTrue(any('spindle reduced' in n for n in notes), notes)

    def test_small_tool_is_derated_on_both_axes(self):
        pp, _ = _pp(0.125)
        expected_feed = 24.0 * (0.125 / 0.157) ** feeds_speeds.DIAMETER_EXPONENT
        # Floored to 0.1 IPM so the F words stay readable and never round back up.
        self.assertAlmostEqual(pp.feed_rate, int(expected_feed * 10) / 10, places=6)
        self.assertAlmostEqual(pp.max_slotting_depth, 0.04 * 0.125 / 0.157, places=6)
        self.assertLess(pp.plunge_rate, 15.0)
        self.assertLess(pp.ramp_feed_rate, 19.0)
        self.assertIn('feed scaled', pp.feed_scale_note)

    def test_peck_depth_scales_with_the_tool(self):
        pp_ref, _ = _pp(0.157)
        pp_small, _ = _pp(0.125)
        self.assertAlmostEqual(pp_small.peck_drill_depth,
                               pp_ref.peck_drill_depth * 0.125 / 0.157, places=6)

    def test_large_tool_keeps_the_tested_preset(self):
        pp, notes = _pp(0.375, machine='omio_x8')
        self.assertEqual(pp.feed_rate, 24.0)
        self.assertEqual(pp.max_slotting_depth, 0.04)

    def test_mm_units_scale_consistently(self):
        pp_in, _ = _pp(0.125, units='inch')
        pp_mm, _ = _pp(0.125 * 25.4, units='mm')
        # Feeds are floored to 0.1 in their own units, so allow that quantization.
        self.assertAlmostEqual(pp_mm.feed_rate, pp_in.feed_rate * 25.4, delta=3.0)
        self.assertAlmostEqual(pp_mm.max_slotting_depth,
                               pp_in.max_slotting_depth * 25.4, places=3)

    def test_tiny_tool_lowers_rpm_instead_of_rubbing(self):
        # A 1 mm one-flute cutter can still make a chip above the Omio spindle floor.
        pp, notes = _pp(1.0 / 25.4)
        floor = feeds_speeds.MATERIALS['aluminum_6063']['chipload_min']
        self.assertGreaterEqual(
            pp.feed_rate * pp.corner_min_feed_scale / pp.spindle_speed, floor)
        self.assertTrue(any('spindle reduced' in n for n in notes), notes)

    def test_without_a_preset_it_is_a_noop(self):
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.125)
        self.assertEqual(pp.scale_feeds_to_tool(), [])

    def test_stale_team_config_cannot_restore_broken_aluminum_values(self):
        stale = TeamConfig({'materials': {'aluminum': {
            'feed_rate': 55.0, 'ramp_feed_rate': 35.0, 'plunge_rate': 25.0,
            'ramp_angle': 12.0, 'stepover_percentage': 0.5,
            'helix_radius_multiplier': 0.8, 'max_slotting_depth': 0.2,
        }}})
        pp, _ = _pp(0.157, config=stale)
        safety = feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX
        self.assertEqual(pp.feed_rate, safety['feed_rate'])
        self.assertEqual(pp.ramp_feed_rate, safety['ramp_feed_rate'])
        self.assertEqual(pp.plunge_rate, safety['plunge_rate'])
        self.assertEqual(pp.max_slotting_depth, safety['max_slotting_depth'])
        self.assertEqual(pp.ramp_angle, safety['ramp_angle'])
        self.assertIn('aluminum safety envelope', pp.feed_scale_note)

    def test_high_flute_aluminum_cutter_is_refused(self):
        with self.assertRaisesRegex(ValueError, '1- or 2-flute'):
            _pp(0.125, flutes=4)

    def test_two_flute_aluminum_cutter_is_explicit_and_allowed(self):
        pp, notes = _pp(0.125, flutes=2)
        self.assertEqual(pp.tool_flutes, 2)
        self.assertLess(pp.spindle_speed, 18000)
        self.assertGreaterEqual(pp.feed_rate / (pp.spindle_speed * 2),
                                feeds_speeds.MATERIALS['aluminum_6061']['chipload_min'])
        self.assertTrue(any('spindle reduced' in n for n in notes), notes)
        corner_feed = pp.feed_rate * pp.corner_min_feed_scale
        self.assertGreaterEqual(corner_feed / (pp.spindle_speed * 2),
                                feeds_speeds.MATERIALS['aluminum_6061']['chipload_min'])

    def test_one_flute_quarter_inch_is_machine_realistic(self):
        """The 2026-09-01 derate: a real 1F 1/4 in profile at 30 IPM / 0.049 in
        full-slot passes overloaded the Omio X8's axis motors. The envelope is now
        24 IPM / 0.04 in with corners at 0.75 x feed - which at 12000 RPM sits
        exactly on the chipload floor, so corner coordination no longer drags the
        spindle down and the 1F runs at a healthy S12000."""
        pp, notes = _pp(0.25, flutes=1)
        self.assertEqual(pp.spindle_speed, 12000)
        self.assertEqual(pp.feed_rate, 24.0)
        self.assertEqual(pp.max_slotting_depth, 0.04)
        minimum = feeds_speeds.MATERIALS['aluminum_6061']['chipload_min']
        corner_feed = pp.feed_rate * pp.corner_min_feed_scale
        self.assertGreaterEqual(corner_feed / pp.spindle_speed, minimum - 1e-9)
        self.assertGreaterEqual(pp.ramp_feed_rate / pp.spindle_speed, minimum - 1e-9)

    def test_two_flute_quarter_inch_keeps_chipload_and_says_so(self):
        """At the machine-realistic 24 IPM, a 2-flute 1/4 in cannot stay above the
        rubbing floor inside the spindle's smooth band - the chipload floor wins
        (rubbing snaps tools, a growl does not), and the notes steer the operator
        to the 1-flute cutter that runs at a healthy RPM."""
        pp, notes = _pp(0.25, flutes=2)
        minimum = feeds_speeds.MATERIALS['aluminum_6061']['chipload_min']
        self.assertGreaterEqual(pp.feed_rate / (pp.spindle_speed * 2), minimum)
        corner_feed = pp.feed_rate * pp.corner_min_feed_scale
        self.assertGreaterEqual(corner_feed / (pp.spindle_speed * 2), minimum - 1e-9)
        self.assertLess(pp.spindle_speed, feeds_speeds.milling_rpm_floor('omio_x8'))
        self.assertTrue(any('1-flute cutter runs healthier' in n for n in notes),
                        notes)

    def test_small_two_flute_keeps_chipload_over_spindle_comfort(self):
        """A 1/8 in 2F cannot make minimum chip in the smooth band at its scaled
        feed - there the chipload floor wins (rubbing snaps tools, a growl does
        not) and the old low-RPM protection stands, with a note steering the
        operator to a 1-flute cutter."""
        pp, notes = _pp(0.125, flutes=2)
        self.assertLess(pp.spindle_speed, feeds_speeds.milling_rpm_floor('omio_x8'))
        minimum = feeds_speeds.MATERIALS['aluminum_6061']['chipload_min']
        self.assertGreaterEqual(pp.feed_rate / (pp.spindle_speed * 2), minimum)
        self.assertTrue(any('1-flute cutter runs healthier' in n for n in notes),
                        notes)

    def test_generated_config_uses_the_same_aluminum_envelope(self):
        import yaml
        generated = yaml.safe_load(CONFIG_TEMPLATE)['materials']['aluminum']
        for key, ceiling in feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX.items():
            self.assertLessEqual(generated[key], ceiling, key)

    def test_every_6061_6063_spelling_gets_the_aluminum_guard(self):
        aliases = ('aluminum', 'aluminum_tube', '6061', '6061-T6',
                   'aluminum_6061', '6063', '6063 T5', 'aluminium-6063')
        for alias in aliases:
            with self.subTest(alias=alias):
                pp, _ = _pp(0.125, material=alias)
                self.assertLessEqual(pp.feed_rate, 30.0)
                self.assertIn(pp.material_id, ('aluminum_6061', 'aluminum_6063'))
                with self.assertRaisesRegex(ValueError, '1- or 2-flute'):
                    _pp(0.125, material=alias, flutes=4)

    def test_hostile_aluminum_config_is_replaced_by_safe_values(self):
        hostile = TeamConfig({'materials': {'aluminum': {
            'feed_rate': float('nan'), 'ramp_feed_rate': -2,
            'plunge_rate': 0, 'max_slotting_depth': -1,
            'spindle_speed': 50000, 'corner_min_feed_scale': -4,
        }}})
        pp, _ = _pp(0.157, config=hostile)
        self.assertTrue(math.isfinite(pp.feed_rate))
        self.assertGreater(pp.max_slotting_depth, 0)
        self.assertGreaterEqual(pp.spindle_speed, 6000)
        self.assertLessEqual(pp.spindle_speed, 24000)
        self.assertGreaterEqual(pp.corner_min_feed_scale, 0.6)

    def test_final_validator_rejects_post_scaling_overrides(self):
        pp, _ = _pp(0.125)
        pp.spindle_speed = 1000
        with self.assertRaisesRegex(ValueError, 'outside'):
            pp.validate_aluminum_cutting_parameters()
        pp, _ = _pp(0.125)
        pp.plunge_rate = 2000
        with self.assertRaisesRegex(ValueError, 'plunge'):
            pp.validate_aluminum_cutting_parameters()
        pp, _ = _pp(0.0625)
        pp.feed_rate = 30
        with self.assertRaisesRegex(ValueError, 'diameter-scaled'):
            pp.validate_aluminum_cutting_parameters()

    def test_aluminum_header_requires_operator_preflight(self):
        pp, _ = _pp(0.157, material='6063')
        pp.holes, pp.pockets, pp.perimeter = [], [], None
        header = '\n'.join(pp._generate_gcode_header('2026-08-25 12:00'))
        self.assertIn('REQUIRED ALUMINUM PREFLIGHT', header)
        self.assertIn('continuous manual air blast', header)
        self.assertIn('6063 requires proven aluminum-compatible lubricant', header)
        self.assertLess(header.index('M0  ; Confirm aluminum preflight'),
                        header.index('M3  ; Spindle on'))

    def test_tube_side_facing_uses_at_most_one_diameter_per_level(self):
        pp, _ = _pp(0.157, material='6063')
        passes = pp._calculate_tube_operation_passes(2.0)
        self.assertLessEqual(passes['roughing_depth_per_pass'], pp.tool_diameter + 1e-9)
        self.assertLessEqual(passes['finishing_depth_per_pass'], pp.tool_diameter + 1e-9)

    def test_tube_endmill_features_are_split_into_depth_levels(self):
        pp, _ = _pp(0.157, material='6063')
        pp.material_thickness = 0.125
        pp._apply_z_frame()
        pp.pockets = [[(0, 0), (1, 0), (1, 1), (0, 1)]]
        pp.holes = []
        gcode = '\n'.join(pp._generate_toolpath_gcode(skip_perimeter=True))
        # 0.125" wall + 0.008" overcut at the derated 0.04" max pass = 4 levels.
        self.assertIn('(Depth levels: 4 ', gcode)

    def test_generated_tube_drill_uses_drilling_model(self):
        pp, _ = _pp(0.201, material='6063')
        pp.tool_has_drill_point = True
        pp.apply_twist_drill_feeds()
        self.assertGreaterEqual(pp.spindle_speed, 6000)
        self.assertLessEqual(pp.spindle_speed, 24000)
        self.assertLessEqual(pp.plunge_rate, 15.0)
        self.assertAlmostEqual(pp.peck_drill_depth, pp.tool_diameter / 3.0)


class TestMaxPassDepth(unittest.TestCase):
    """The operator's depth-per-pass ceiling: more, shallower passes on request."""

    def test_clamps_down_and_says_so(self):
        pp, _ = _pp(0.157)
        pp.apply_max_pass_depth(0.03)
        self.assertEqual(pp.max_slotting_depth, 0.03)
        self.assertIn('limited to 0.030 in by operator', pp.feed_scale_note)

    def test_never_raises(self):
        # The automatic value is itself a safety ceiling; an operator setting above it
        # must be a no-op, not an override.
        pp, _ = _pp(0.125)
        automatic = pp.max_slotting_depth
        pp.apply_max_pass_depth(5.0)
        self.assertEqual(pp.max_slotting_depth, automatic)
        self.assertNotIn('operator', getattr(pp, 'feed_scale_note', '') or '')

    def test_rejects_nonsense(self):
        pp, _ = _pp(0.157)
        for bad in (0, -0.05, float('nan'), float('inf'), 'deep'):
            with self.assertRaises(ValueError):
                pp.apply_max_pass_depth(bad)

    def test_the_program_takes_more_passes(self):
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (3, 0), (3, 3), (0, 3)], close=True)
        dxf = tempfile.mktemp(suffix='.dxf')
        doc.saveas(dxf)
        try:
            def passes(ceiling):
                with redirect_stdout(io.StringIO()):
                    pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.157,
                                          units='inch')
                    pp.apply_material_preset('aluminum')
                    pp.scale_feeds_to_tool()
                    if ceiling:
                        pp.apply_max_pass_depth(ceiling)
                    pp.load_dxf(dxf)
                    pp.transform_coordinates('bottom-left', 0)
                    pp.identify_perimeter_and_pockets()
                    pp.classify_holes()
                    result = pp.generate_gcode(timestamp='2026-08-24 12:00:00')
                assert result.success, result.errors
                return result.gcode.count('===== PASS ')
            # 0.258" total at the derated 0.04" preset = 7 passes; a 0.03" ceiling
            # tightens that to 9.
            self.assertEqual(passes(None), 7)
            self.assertEqual(passes(0.03), 9)
        finally:
            os.remove(dxf)


class TestMaxPassDepthRoutes(unittest.TestCase):
    """The ceiling survives the trip from the browser form to the emitted program."""

    @classmethod
    def setUpClass(cls):
        from frc_cam_gui_app import app
        app.config['TESTING'] = True
        cls.client = app.test_client()
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (3, 0), (3, 3), (0, 3)], close=True)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        with open(path, 'rb') as fh:
            cls.dxf_bytes = fh.read()
        os.remove(path)

    def _post_job(self, extra):
        import json
        job = {'material': 'aluminum', 'tool_diameter': 0.157, 'thickness': 0.25,
               'tab_spacing': 6.0, 'stock': {'width': 10, 'height': 10},
               'name': 'passjob',
               'parts': [{'file_index': 0, 'name': 'plate',
                          'place_x': 0, 'place_y': 0, 'rotation': 0}]}
        job.update(extra)
        data = {'job': json.dumps(job), 'timestamp': '2026-08-24 12:00:00',
                'file_0': (io.BytesIO(self.dxf_bytes), 'plate.dxf')}
        return self.client.post('/process-job', data=data,
                                content_type='multipart/form-data')

    def test_job_route_honors_the_ceiling(self):
        response = self._post_job({'max_pass_depth': 0.03})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertIn('9 passes', body['gcode'])
        # Header comments wrap at the controller line limit; rejoin wrapped
        # comment lines before matching the full phrase.
        self.assertIn('limited to 0.030 in by operator',
                      body['gcode'].replace(')\n(', ' '))

    def test_bad_ceiling_is_a_400(self):
        response = self._post_job({'max_pass_depth': -1})
        self.assertEqual(response.status_code, 400)

    def test_job_route_refuses_high_flute_aluminum_cutter(self):
        response = self._post_job({'tool_flutes': 4})
        self.assertEqual(response.status_code, 400)
        self.assertIn('1- or 2-flute', response.get_json()['error'])


class TestTabRemovalRespectsDepthLimit(unittest.TestCase):
    """Tab removal was the one cut that ignored max_slotting_depth: it slotted the
    full tab height in a single move. On 0.125" aluminum the tabs WERE the full plate
    thickness, and a program whose profile obeyed a 0.031" ceiling still buried a
    1/8" cutter 0.133" deep at the tabs - a real bit broke there."""

    def _removal(self, limit, tab_top=None, thickness=0.25):
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(material_thickness=thickness, tool_diameter=0.157,
                                  units='inch')
            pp.apply_material_preset('plywood')
        pp.max_slotting_depth = limit
        if tab_top is not None:
            pp._tab_material_top = tab_top
        waypoints = [(0.0, 0.0), (0.25, 0.0)]
        return pp, pp._generate_tab_removal_gcode([(0, waypoints)])

    def test_thick_tabs_step_down(self):
        # No handoff from a contour pass: assume full tabs (0.15" tall). A 0.05"
        # limit must chew that in 3 passes, each plunging in the open kerf.
        pp, lines = self._removal(limit=0.05)
        text = '\n'.join(lines)
        self.assertIn('Each tab in 3 passes', text)
        plunges = [l for l in lines if 'Plunge in kerf' in l]
        self.assertEqual(len(plunges), 3)
        self.assertIn(f'Z{pp.cut_depth:.4f}', plunges[-1])   # last pass hits bottom
        # Each successive plunge is deeper, and no step exceeds the limit.
        zs = [float(p.split('Z')[1].split()[0].split('F')[0]) for p in plunges]
        prev = min(pp.material_top, pp.cut_depth + pp.tab_height)
        for z in zs:
            self.assertLessEqual(prev - z, 0.05 + 1e-9)
            prev = z

    def test_thin_tabs_stay_single_pass(self):
        # Tabs already thinned by the perimeter passes to one pass-depth: exactly the
        # historic single-pass output, byte for byte (golden fixtures depend on it).
        pp, lines = self._removal(limit=0.2, tab_top=0.03)
        text = '\n'.join(lines)
        self.assertNotIn('Each tab in', text)
        self.assertEqual(sum('Plunge in kerf' in l for l in lines), 1)

    def test_end_to_end_thin_aluminum_never_exceeds_the_ceiling(self):
        """The WCP-0543 scenario: 0.125" aluminum, 1/8" tool, 0.031" ceiling. Every
        cut in the program - tabs included - must engage 0.031" or less."""
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (2, 0), (2, 6), (0, 6)], close=True)
        dxf = tempfile.mktemp(suffix='.dxf')
        doc.saveas(dxf)
        try:
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(material_thickness=0.125, tool_diameter=0.125,
                                      units='inch')
                pp.apply_material_preset('aluminum')
                pp.scale_feeds_to_tool()
                pp.apply_max_pass_depth(0.03125)
                pp.load_dxf(dxf)
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                result = pp.generate_gcode(timestamp='2026-08-24 13:00:00')
            self.assertTrue(result.success, result.errors)
            # The tabs stand their full designed height - on stock this thin that is
            # the whole plate - so the removal pass is what has to respect the ceiling,
            # stepping down through them like every other cut.
            tab_top = pp._tab_material_top
            self.assertGreater(tab_top - pp.cut_depth, 0.03125,
                               'tabs were thinned; they hold a fraction of the part')
            lines = result.gcode.splitlines()
            start = next(i for i, l in enumerate(lines) if 'TAB REMOVAL PASS' in l)
            plunges = [float(re.search(r'Z(-?[\d.]+)', l).group(1))
                       for l in lines[start:] if 'Plunge in kerf' in l]
            self.assertTrue(plunges)
            previous = tab_top
            for z in plunges:
                if z > previous:            # next tab, back to the top
                    previous = tab_top
                self.assertLessEqual(previous - z, 0.03125 + 1e-9,
                                     f'tab removal engages {previous - z:.4f}"')
                previous = z
            self.assertAlmostEqual(min(plunges), pp.cut_depth, places=4)
        finally:
            os.remove(dxf)


class TestDerateReachesTheProgram(unittest.TestCase):
    """The scaled numbers must appear in the emitted moves and header, because the
    attributes being right means nothing if the G-code was generated before them."""

    @classmethod
    def setUpClass(cls):
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (3, 0), (3, 3), (0, 3)], close=True)
        cls.dxf = tempfile.mktemp(suffix='.dxf')
        doc.saveas(cls.dxf)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.dxf)

    def _generate(self, tool):
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=tool,
                                  units='inch')
            pp.apply_material_preset('aluminum')
            pp.scale_feeds_to_tool()
            pp.load_dxf(self.dxf)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            return pp.generate_gcode(timestamp='2026-08-24 12:00:00')

    def test_small_tool_program_carries_the_derated_feed(self):
        result = self._generate(0.125)
        self.assertTrue(result.success, result.errors)
        self.assertIn('F20.4', result.gcode)          # scaled cutting feed in the moves
        self.assertNotIn('F24.0', result.gcode)       # the 4 mm feed must be gone
        self.assertIn('feed scaled to 20.4 ipm', result.gcode)   # header note

    def test_program_header_states_flute_count(self):
        result = self._generate(0.157)
        self.assertIn('1-flute Flat End Mill', result.gcode)

    def test_reference_tool_program_is_unchanged(self):
        result = self._generate(0.157)
        self.assertTrue(result.success, result.errors)
        self.assertIn('F24.0', result.gcode)
        self.assertNotIn('feed scaled', result.gcode)



class TestChiploadCoordinationForEveryMaterial(unittest.TestCase):
    """RPM coordination was aluminum-only; the chipload REFUSAL applied to everything.

    So a config the shop had been cutting plywood with for years - 70 IPM, 18000 RPM, a
    two-flute cutter - stopped generating: 70 IPM at 18000 RPM on two flutes is 0.0019
    per tooth against plywood's 0.002 minimum. The fix for that is the one aluminum
    already had: drop the RPM until the chip is big enough. Refusal is for when even the
    spindle floor cannot get there.
    """

    def _pp(self, material, feed, flutes=2, tool=0.157, rpm=18000):
        cfg = TeamConfig({'version': 2, 'default_machine': 'omio_x8', 'machines': {
            'omio_x8': {'name': 'Omio', 'materials': {material: {
                'feed_rate': feed, 'ramp_feed_rate': 50.0, 'plunge_rate': 35.0,
                'spindle_speed': rpm}}}}})
        pp = FRCPostProcessor(0.25, tool, config=cfg, tool_flutes=flutes)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset(material, 'omio_x8')
            pp.scale_feeds_to_tool()
        return pp

    def _chipload(self, pp):
        return pp.feed_rate / (pp.spindle_speed * pp.tool_flutes)

    def test_two_flute_plywood_at_70_ipm_works_again(self):
        pp = self._pp('plywood', 70.0)
        minimum = feeds_speeds.MATERIALS['plywood']['chipload_min']
        self.assertGreaterEqual(self._chipload(pp), minimum - 1e-9)
        self.assertLess(pp.spindle_speed, 18000, 'the RPM was not coordinated down')
        self.assertGreaterEqual(pp.spindle_speed,
                                feeds_speeds.MACHINES['omio_x8']['rpm_min'])

    def test_the_feed_the_shop_tested_is_not_reduced(self):
        """Coordination lowers RPM, never the tested feed."""
        pp = self._pp('plywood', 70.0)
        self.assertAlmostEqual(pp.feed_rate, 70.0)

    def test_every_modelled_material_gets_coordinated(self):
        for material in ('plywood', 'polycarbonate', 'hdpe', 'srpp'):
            with self.subTest(material=material):
                pp = self._pp(material, 70.0)
                minimum = feeds_speeds.MATERIALS[material]['chipload_min']
                self.assertGreaterEqual(self._chipload(pp), minimum - 1e-9)

    def test_it_still_refuses_when_the_floor_cannot_get_there(self):
        """A four-flute cutter at 20 IPM asks 0.0008 per tooth at the 6000 RPM floor;
        no amount of coordinating reaches 0.002."""
        with self.assertRaises(ValueError) as caught:
            self._pp('plywood', 20.0, flutes=4)
        message = str(caught.exception).lower()
        self.assertIn('chip', message)

    def test_aluminum_keeps_its_corner_protected_ceiling(self):
        """Aluminum coordinates against the CORNER feed, not just the straight one, so
        a corner move cannot drop into the rubbing regime. That must not be relaxed."""
        pp = self._pp('aluminum', 30.0, flutes=2)
        minimum = feeds_speeds.MATERIALS['aluminum_6063']['chipload_min']
        corner_feed = pp.feed_rate * pp.corner_min_feed_scale
        self.assertGreaterEqual(corner_feed / (pp.spindle_speed * pp.tool_flutes),
                                minimum - 1e-9)

    def test_a_config_already_inside_the_model_is_untouched(self):
        pp = self._pp('plywood', 75.0, flutes=1)
        self.assertEqual(pp.spindle_speed, 18000)
        self.assertAlmostEqual(pp.feed_rate, 75.0)


if __name__ == '__main__':
    unittest.main()
