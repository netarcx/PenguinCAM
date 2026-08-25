"""Dry run: the same program, raised clear of the work, with the spindle off.

The point of a dry run is to be TRUSTWORTHY in two opposite directions:

  1. It must not cut. Not "cut shallower" - not touch the material at all, with the
     spindle never started, so a wrong origin or a clamp in the path is discovered by
     watching rather than by listening to a cutter break.
  2. It must be the SAME program otherwise. A dry run that traced a different path
     would prove nothing about the one you are about to run.

So the central assertion mirrors the Z-datum tests: every Z word raised by exactly the
lift, every other character identical.
"""
import io
import math
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

from frc_cam_postprocessor import FRCPostProcessor
from team_config import TeamConfig
import tooling

Z_WORD = re.compile(r'(?<![A-Za-z])Z(-?\d+\.?\d*)')
LIFT = 2.0


def _plate(path):
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True)
    msp.add_circle((2, 3), 0.3)
    msp.add_lwpolyline([(0.5, 0.5), (2.0, 0.5), (2.0, 2.0), (0.5, 2.0)], close=True)
    doc.saveas(path)
    return path


class DryRunFrameTest(unittest.TestCase):
    def test_the_lift_moves_all_three_anchors(self):
        pp = FRCPostProcessor(0.25, 0.25, config=TeamConfig())
        before = (pp.material_top, pp.retract_height, pp.cut_depth, pp._safe_z())
        pp.set_dry_run(LIFT)
        after = (pp.material_top, pp.retract_height, pp.cut_depth, pp._safe_z())
        for was, now in zip(before, after):
            self.assertAlmostEqual(now - was, LIFT, places=9)

    def test_the_lift_is_charged_once_even_with_a_configured_ceiling(self):
        """z_shift is measured from material_top, so it already carries the lift -
        adding it again in _safe_z charged a 2 inch lift as 4."""
        cfg = TeamConfig({'machining': {'z_reference': {'safe_height': 1.5}}})
        pp = FRCPostProcessor(0.25, 0.25, config=cfg)
        before = pp._safe_z()
        pp.set_dry_run(LIFT)
        self.assertAlmostEqual(pp._safe_z() - before, LIFT, places=9)

    def test_a_negative_or_absurd_lift_is_refused(self):
        pp = FRCPostProcessor(0.25, 0.25)
        for bad in (-1.0, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                pp.set_dry_run(bad)

    def test_is_dry_run_is_false_by_default(self):
        self.assertFalse(FRCPostProcessor(0.25, 0.25).is_dry_run)


class DryRunProgramTest(unittest.TestCase):
    THICK = 0.25

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='dryrun_')
        cls.dxf = _plate(os.path.join(cls.tmp, 'plate.dxf'))
        cls.real = cls._program(cls, 0)
        cls.dry = cls._program(cls, LIFT)

    def _program(self, lift):
        pp = FRCPostProcessor(self.THICK, 0.25, config=TeamConfig())
        pp.apply_material_preset('plywood')
        if lift:
            pp.set_dry_run(lift)
        pp.load_dxf(self.dxf)
        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        result = pp.generate_gcode()
        assert result.success, result.errors
        return result.gcode

    def test_same_motion_raised(self):
        """Every Z word exactly `LIFT` higher; every other character identical."""
        # Compare motion only: the dry run adds a banner, and the spindle block differs
        # by design. Match the spindle START precisely - a bare 'M3' substring also
        # matches M30, which quietly dropped the program end from one side of this
        # comparison and made the two lists differ by a line that was never the point.
        spindle = re.compile(r'^(S\d+\s+M3|M5\s+; DRY RUN)')
        def motion(gcode):
            out = []
            for line in gcode.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('('):
                    continue
                if spindle.match(stripped) or stripped.startswith('G4 P'):
                    continue
                out.append(line)
            return out
        real_moves, dry_moves = motion(self.real), motion(self.dry)
        self.assertEqual(len(real_moves), len(dry_moves),
                         'the dry run is not the same program')
        compared = 0
        for n, (r, d) in enumerate(zip(real_moves, dry_moves), start=1):
            self.assertEqual(Z_WORD.sub('Z*', r), Z_WORD.sub('Z*', d),
                             f'motion line {n} differs in something other than Z')
            for a, b in zip(Z_WORD.findall(r), Z_WORD.findall(d)):
                compared += 1
                self.assertAlmostEqual(float(b) - float(a), LIFT, delta=1.01e-4,
                                       msg=f'line {n}: Z{a} vs Z{b}')
        self.assertGreater(compared, 20, 'suspiciously few Z moves compared')

    def test_nothing_reaches_the_material(self):
        """The whole point: the lowest move is clear above the top of the stock."""
        deepest = min(float(z) for z in Z_WORD.findall(self.dry))
        self.assertGreater(deepest, self.THICK,
                           'a dry run move dipped to or below the top of the stock')

    def test_the_spindle_is_never_started(self):
        self.assertIsNone(re.search(r'(?m)^S\d+\s+M3', self.dry),
                          'the dry run starts the spindle')
        self.assertIn('M5', self.dry)
        self.assertRegex(self.real, r'(?m)^S\d+\s+M3')   # the real one still does

    def test_the_operator_is_told_in_the_header(self):
        self.assertIn('DRY RUN - THIS PROGRAM DOES NOT CUT ANYTHING', self.dry)
        self.assertNotIn('DRY RUN', self.real)

    def test_the_header_obeys_the_g_code_rules(self):
        """The banner is generated text going into a comment, which is exactly where
        this project's ASCII and nested-paren rules get broken."""
        self.assertTrue(all(ord(c) < 128 for c in self.dry), 'non-ASCII in the program')
        self.assertIsNone(re.search(r'\([^)]*\(', self.dry), 'a nested or unclosed comment')
        self.assertIsNone(re.search(r'\([^)]*[\[\]]', self.dry), 'brackets inside a comment')

    def test_the_file_says_it_is_a_dry_run(self):
        pp = FRCPostProcessor(self.THICK, 0.25, config=TeamConfig())
        pp.apply_material_preset('plywood')
        pp.set_dry_run(LIFT)
        pp.load_dxf(self.dxf)
        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        result = pp.generate_gcode(suggested_filename='bracket',
                                   timestamp='20260825_120000')
        self.assertIn('_DRYRUN', result.filename)


class DryRunMultiToolTest(unittest.TestCase):
    THICK = 0.25

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dryrun_mt_')
        self.dxf = _plate(os.path.join(self.tmp, 'plate.dxf'))

    def _job(self, lift):
        spec = {
            'material': 'plywood', 'thickness': self.THICK, 'name': 'plate',
            'tools': [{'slot': 1, 'name': '1/4 endmill', 'diameter': 0.25, 'flutes': 2}],
            'parts': [{'file_index': 0, 'name': 'p', 'operations': [
                {'op_type': 'holes', 'tool_slot': 1},
                {'op_type': 'pockets', 'tool_slot': 1},
                {'op_type': 'perimeter', 'tool_slot': 1}]}],
        }
        if lift:
            spec['dry_run_lift'] = lift
        job = tooling.job_from_dict(spec, {0: self.dxf}, config=TeamConfig())
        result = tooling.generate_multitool_job(job)
        self.assertTrue(result.success, msg=str(result.errors))
        return result

    def test_a_multi_tool_job_lifts_and_marks_itself(self):
        real, dry = self._job(0), self._job(LIFT)
        real_z = [float(z) for z in Z_WORD.findall(real.gcode)]
        dry_z = [float(z) for z in Z_WORD.findall(dry.gcode)]
        self.assertEqual(len(real_z), len(dry_z))
        for a, b in zip(real_z, dry_z):
            self.assertAlmostEqual(b - a, LIFT, delta=1.01e-4)
        self.assertIn('_DRYRUN', dry.filename)
        self.assertNotIn('_DRYRUN', real.filename)

    def test_the_tool_change_pause_does_not_spin_up_either(self):
        """Every restart-after-pause block starts the spindle again; a dry run must not."""
        spec_gcode = self._job(LIFT).gcode
        self.assertIsNone(re.search(r'(?m)^S\d+\s+M3', spec_gcode))


if __name__ == '__main__':
    unittest.main()
