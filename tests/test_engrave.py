"""Engraved part names: telling one bracket from eleven others once they are off the machine.

The feature is small; the ways it can go wrong are not. An engraving is a light cut in
the middle of a face, so the failure modes are: cutting it after the part is free (it
chatters and snaps a tab), cutting it with a tool too fat to write (an unreadable
smear), cutting it off the edge of the part (a gouge in the neighbour), and doing any of
those silently. Each has a test.
"""
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import stroke_font
from frc_cam_postprocessor import FRCPostProcessor
from team_config import TeamConfig


def _plate(path, width=4.0, height=3.0):
    doc = ezdxf.new('R2010')
    doc.modelspace().add_lwpolyline(
        [(0, 0), (width, 0), (width, height), (0, height)], close=True)
    doc.saveas(path)
    return path


def _build(dxf, tool=0.0625, text='GEARBOX-L', height=0.18, size=(4.0, 3.0)):
    with redirect_stdout(io.StringIO()):
        pp = FRCPostProcessor(0.25, tool, config=TeamConfig())
        pp.apply_material_preset('plywood')
        pp.engrave = {'text': text, 'height': height, 'depth': 0.01}
        pp.load_dxf(dxf)
        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        return pp, pp.generate_gcode()


class StrokeFontTest(unittest.TestCase):
    def test_every_character_a_part_name_can_hold_is_drawable(self):
        """sanitize_comment leaves letters, digits and a few marks; each needs a glyph,
        or a label silently reads as a different part number."""
        for char in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.#/+ ':
            self.assertIn(char, stroke_font.GLYPHS, f'no glyph for {char!r}')

    def test_an_unknown_character_still_marks_the_part(self):
        strokes, _ = stroke_font.text_strokes('§')
        self.assertTrue(strokes, 'an unmappable character vanished silently')

    def test_width_scales_with_height(self):
        narrow = stroke_font.text_width('ABC', height=0.1)
        wide = stroke_font.text_width('ABC', height=0.2)
        self.assertAlmostEqual(wide, narrow * 2, places=6)

    def test_strokes_stay_inside_the_nominal_box(self):
        strokes, width = stroke_font.text_strokes('BRACKET-2', height=0.25)
        for stroke in strokes:
            for x, y in stroke:
                self.assertGreaterEqual(x, -1e-9)
                self.assertLessEqual(x, width + 1e-9)
                self.assertGreaterEqual(y, -1e-9)
                self.assertLessEqual(y, 0.25 + 1e-9)


class EngraveToolpathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='engrave_')
        self.dxf = _plate(os.path.join(self.tmp, 'plate.dxf'))

    def test_the_name_is_cut_into_the_part(self):
        pp, result = _build(self.dxf)
        self.assertTrue(result.success, msg=str(result.errors))
        self.assertIn('(===== ENGRAVE PART NAME =====)', result.gcode)
        self.assertIn('(Text: GEARBOX-L)', result.gcode)

    def test_it_runs_before_the_profile(self):
        """After the profile the part hangs on tabs, and a light chattery label cut is
        exactly what breaks one."""
        _, result = _build(self.dxf)
        gcode = result.gcode
        self.assertLess(gcode.index('ENGRAVE PART NAME'), gcode.index('PERIMETER'),
                        'the name is engraved after the part is cut free')

    def test_every_engraved_move_lands_on_the_part(self):
        """A label that runs off the part is a gouge in whatever is beside it."""
        _, result = _build(self.dxf)
        inside = False
        moves = 0
        for line in result.gcode.splitlines():
            if 'ENGRAVE PART NAME' in line:
                inside = True
                continue
            if inside and line.startswith('(====='):
                break
            if inside:
                x = re.search(r'X(-?[\d.]+)', line)
                y = re.search(r'Y(-?[\d.]+)', line)
                if x and y:
                    moves += 1
                    self.assertGreater(float(x.group(1)), 0.0)
                    self.assertLess(float(x.group(1)), 4.0)
                    self.assertGreater(float(y.group(1)), 0.0)
                    self.assertLess(float(y.group(1)), 3.0)
        self.assertGreater(moves, 20, 'suspiciously few engraved moves')

    def test_it_cuts_shallow_and_never_through(self):
        pp, result = _build(self.dxf)
        inside = False
        for line in result.gcode.splitlines():
            if 'ENGRAVE PART NAME' in line:
                inside = True
                continue
            if inside and line.startswith('(====='):
                break
            if inside:
                z = re.search(r'(?<![A-Za-z])Z(-?[\d.]+)', line)
                if z:
                    self.assertGreater(float(z.group(1)), pp.cut_depth,
                                       'an engraving move went to the through depth')

    def test_a_tool_too_fat_to_write_is_refused_out_loud(self):
        """Not silently: an operator who ticked the box is expecting a label."""
        _, result = _build(self.dxf, tool=0.25)
        self.assertNotIn('ENGRAVE PART NAME', result.gcode)
        self.assertTrue(any('legible' in w for w in result.warnings),
                        f'no warning explaining the skip: {result.warnings}')

    def test_a_part_too_small_is_refused_out_loud(self):
        tiny = _plate(os.path.join(self.tmp, 'tiny.dxf'), width=0.4, height=0.4)
        _, result = _build(tiny, tool=0.0625)
        self.assertNotIn('ENGRAVE PART NAME', result.gcode)
        self.assertTrue(result.warnings)

    def test_the_name_shrinks_to_fit_rather_than_overflowing(self):
        small = _plate(os.path.join(self.tmp, 'small.dxf'), width=1.2, height=1.0)
        _, result = _build(small, tool=0.03125, text='A-VERY-LONG-PART-NAME')
        if 'ENGRAVE PART NAME' in result.gcode:
            cap = float(re.search(r'Cap height ([\d.]+) in', result.gcode).group(1))
            self.assertLess(cap, 0.18, 'the name did not shrink to fit')

    def test_the_program_still_obeys_the_g_code_rules(self):
        """Part names are user text going into a comment - the exact place this
        project's ASCII and nested-paren rules get broken."""
        _, result = _build(self.dxf, text='Bracket (left) [v2]')
        self.assertTrue(all(ord(c) < 128 for c in result.gcode))
        self.assertIsNone(re.search(r'\([^)]*\(', result.gcode))
        self.assertIsNone(re.search(r'\([^)]*[\[\]]', result.gcode))

    def test_phases_carry_the_engraving_for_multi_part_jobs(self):
        """Multi-part jobs build phases directly and never call generate_gcode, so an
        engraving hooked only into generate_gcode silently did nothing there."""
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(0.25, 0.0625, config=TeamConfig())
            pp.apply_material_preset('plywood')
            pp.engrave = {'text': 'GEARBOX-R', 'height': 0.18, 'depth': 0.01}
            pp.load_dxf(self.dxf)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            phases = pp.generate_part_phases()
        self.assertTrue(any('ENGRAVE PART NAME' in l for l in phases['interior']))


if __name__ == '__main__':
    unittest.main()
