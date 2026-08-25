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
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import feeds_speeds
from frc_cam_postprocessor import FRCPostProcessor


def _pp(tool, material='aluminum', units='inch', machine=None):
    with redirect_stdout(io.StringIO()):
        pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=tool, units=units)
        pp.apply_material_preset(material, machine)
        notes = pp.scale_feeds_to_tool()
    return pp, notes


class TestScaleFeedsToTool(unittest.TestCase):
    def test_reference_tool_is_untouched(self):
        # The presets ARE the tested numbers for the 4 mm tool; changing them for it
        # would silently rewrite feeds the team has cut with.
        pp, notes = _pp(0.157)
        self.assertEqual(notes, [])
        self.assertEqual(pp.feed_rate, 30.0)
        self.assertEqual(pp.max_slotting_depth, 0.06)
        self.assertIsNone(getattr(pp, 'feed_scale_note', None))

    def test_small_tool_is_derated_on_both_axes(self):
        pp, _ = _pp(0.125)
        expected_feed = 30.0 * (0.125 / 0.157) ** feeds_speeds.DIAMETER_EXPONENT
        # Floored to 0.1 IPM so the F words stay readable and never round back up.
        self.assertAlmostEqual(pp.feed_rate, int(expected_feed * 10) / 10, places=6)
        self.assertAlmostEqual(pp.max_slotting_depth, 0.06 * 0.125 / 0.157, places=6)
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
        self.assertEqual(pp.feed_rate, 30.0)
        self.assertEqual(pp.max_slotting_depth, 0.06)

    def test_mm_units_scale_consistently(self):
        pp_in, _ = _pp(0.125, units='inch')
        pp_mm, _ = _pp(0.125 * 25.4, units='mm')
        # Feeds are floored to 0.1 in their own units, so allow that quantization.
        self.assertAlmostEqual(pp_mm.feed_rate, pp_in.feed_rate * 25.4, delta=3.0)
        self.assertAlmostEqual(pp_mm.max_slotting_depth,
                               pp_in.max_slotting_depth * 25.4, places=3)

    def test_tiny_tool_warns_about_rubbing(self):
        # A 1 mm cutter derates to a chipload below aluminum's minimum: it will rub
        # and work-harden the wall. Nothing safe to raise automatically - warn.
        pp, notes = _pp(1.0 / 25.4)
        self.assertTrue(any('rub' in n for n in notes), notes)

    def test_without_a_preset_it_is_a_noop(self):
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.125)
        self.assertEqual(pp.scale_feeds_to_tool(), [])


class TestMaxPassDepth(unittest.TestCase):
    """The operator's depth-per-pass ceiling: more, shallower passes on request."""

    def test_clamps_down_and_says_so(self):
        pp, _ = _pp(0.157)
        pp.apply_max_pass_depth(0.05)
        self.assertEqual(pp.max_slotting_depth, 0.05)
        self.assertIn('limited to 0.050 in by operator', pp.feed_scale_note)

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
            # 0.258" total at the derated 0.06" preset = 5 passes; a 0.05" ceiling
            # tightens that to 6.
            self.assertEqual(passes(None), 5)
            self.assertEqual(passes(0.05), 6)
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
        response = self._post_job({'max_pass_depth': 0.05})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertIn('6 passes', body['gcode'])
        self.assertIn('limited to 0.050 in by operator', body['gcode'])

    def test_bad_ceiling_is_a_400(self):
        response = self._post_job({'max_pass_depth': -1})
        self.assertEqual(response.status_code, 400)


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
            # The tab material left standing is one perimeter pass-depth, so the
            # removal engages within the ceiling.
            self.assertLessEqual(pp._tab_material_top - pp.cut_depth, 0.03125 + 1e-9)
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
        self.assertIn('F25.5', result.gcode)          # scaled cutting feed in the moves
        self.assertNotIn('F30.0', result.gcode)       # the 4 mm feed must be gone
        self.assertIn('feed scaled to 25.5 ipm', result.gcode)   # header note

    def test_reference_tool_program_is_unchanged(self):
        result = self._generate(0.157)
        self.assertTrue(result.success, result.errors)
        self.assertIn('F30.0', result.gcode)
        self.assertNotIn('feed scaled', result.gcode)


if __name__ == '__main__':
    unittest.main()
