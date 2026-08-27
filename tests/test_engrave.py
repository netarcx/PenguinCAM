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
from frc_cam_postprocessor import FRCPostProcessor, sanitize_comment
from team_config import TeamConfig


def _plate(path, width=4.0, height=3.0):
    doc = ezdxf.new('R2010')
    doc.modelspace().add_lwpolyline(
        [(0, 0), (width, 0), (width, height), (0, height)], close=True)
    doc.saveas(path)
    return path


def _build(dxf, tool=0.0625, text='GEARBOX-L', height=0.18, size=(4.0, 3.0),
           anchor=None):
    with redirect_stdout(io.StringIO()):
        pp = FRCPostProcessor(0.25, tool, config=TeamConfig())
        pp.apply_material_preset('plywood')
        pp.engrave = {'text': text, 'height': height, 'depth': 0.01,
                      'anchor': anchor}
        pp.load_dxf(dxf)
        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        return pp, pp.generate_gcode()


class StrokeFontTest(unittest.TestCase):
    def test_the_characters_a_part_name_can_hold_are_drawable(self):
        """Derived from sanitize_comment, not hardcoded: a hardcoded list only ever
        proves the glyphs someone remembered to write down exist."""
        emitted = set()
        for code in range(32, 127):
            emitted.update(sanitize_comment(chr(code), fallback='').upper())
        missing = sorted(emitted - set(stroke_font.GLYPHS))
        # The stragglers are shell and markup punctuation, which no part name carries;
        # _engrave_body turns each into a visible dash AND warns, so a label is never
        # silently renamed. Anything else appearing here is a gap to fill.
        self.assertEqual(missing, ['<', '>', '\\', '^', '`', '{', '|', '}', '~'])

    def test_a_character_with_no_glyph_becomes_a_dash_and_says_so(self):
        strokes, _ = stroke_font.text_strokes('~')
        self.assertTrue(strokes, 'an unmappable character vanished silently')

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

    def test_a_selected_position_places_the_label_on_that_part(self):
        _, result = _build(self.dxf, text='P #17', anchor=(0.75, 0.65))
        self.assertIn('(Text: P #17)', result.gcode)
        section = result.gcode.split('ENGRAVE PART NAME', 1)[1].split('(=====', 1)[0]
        xs = [float(v) for v in re.findall(r'X(-?[\d.]+)', section)]
        ys = [float(v) for v in re.findall(r'Y(-?[\d.]+)', section)]
        self.assertTrue(xs and ys)
        self.assertAlmostEqual((min(xs) + max(xs)) / 2, 0.75, delta=0.02)
        self.assertAlmostEqual((min(ys) + max(ys)) / 2, 0.65, delta=0.02)

    def test_a_selected_position_cannot_put_the_name_on_the_stock(self):
        _, result = _build(self.dxf, text='BRACKET #17', anchor=(0.02, 0.02))
        self.assertNotIn('ENGRAVE PART NAME', result.gcode)
        self.assertTrue(any('selected label position' in warning
                            for warning in result.warnings), result.warnings)

    def test_it_runs_before_the_profile(self):
        """After the profile the part hangs on tabs, and a light chattery label cut is
        exactly what breaks one."""
        pp, result = _build(self.dxf)
        gcode = result.gcode
        self.assertLess(gcode.index('ENGRAVE PART NAME'), gcode.index('PERIMETER'),
                        'the name is engraved after the part is cut free')

    def test_every_engraved_move_lands_on_the_part(self):
        """A label that runs off the part is a gouge in whatever is beside it."""
        pp, result = _build(self.dxf)
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
                    # A tool RADIUS inside the outline, not merely inside it: the
                    # centreline is what the G-code says, and a cut whose centre is
                    # on the edge takes half a kerf out of the profile.
                    r = pp.tool_radius
                    self.assertGreater(float(x.group(1)), r)
                    self.assertLess(float(x.group(1)), 4.0 - r)
                    self.assertGreater(float(y.group(1)), r)
                    self.assertLess(float(y.group(1)), 3.0 - r)
        self.assertGreater(moves, 20, 'suspiciously few engraved moves')

    def test_it_cuts_shallow_and_never_through(self):
        pp, result = _build(self.dxf)
        inside = False
        zmoves = 0
        for line in result.gcode.splitlines():
            if 'ENGRAVE PART NAME' in line:
                inside = True
                continue
            if inside and line.startswith('(====='):
                break
            if inside:
                z = re.search(r'(?<![A-Za-z])Z(-?[\d.]+)', line)
                if z:
                    zmoves += 1
                    self.assertGreater(float(z.group(1)), pp.cut_depth,
                                       'an engraving move went to the through depth')
        # Without this the test passes when there is no engraving at all, which is
        # exactly the regression it exists to catch.
        self.assertGreater(zmoves, 0, 'no engraved Z moves to check')

    def test_a_fat_tool_writes_bigger_rather_than_writing_a_blob(self):
        """0.25" is the DEFAULT bit. Refusing it outright meant the feature never worked
        out of the box, and the old 1.2x gate let it through to cut a solid smear. A
        part with room gets taller letters instead."""
        pp, result = _build(self.dxf, tool=0.25)
        self.assertIn('ENGRAVE PART NAME', result.gcode)
        cap = float(re.search(r'Cap height ([\d.]+) in', result.gcode).group(1))
        self.assertGreaterEqual(
            cap, 0.25 * pp.ENGRAVE_MIN_HEIGHT_PER_TOOL - 1e-9,
            'the letters are too small for this cutter to keep their strokes apart')

    def test_a_tool_too_fat_for_the_part_is_refused_out_loud(self):
        """Not silently, and blaming the right thing: when the cutter is what will not
        fit, saying "no clear space" sends someone hunting for room they already have."""
        narrow = _plate(os.path.join(self.tmp, 'narrow.dxf'), width=1.2, height=0.5)
        _, result = _build(narrow, tool=0.25)
        self.assertNotIn('ENGRAVE PART NAME', result.gcode)
        self.assertTrue(any('cutter cannot write' in w for w in result.warnings),
                        f'no warning naming the tool: {result.warnings}')

    def test_a_part_too_small_is_refused_out_loud(self):
        tiny = _plate(os.path.join(self.tmp, 'tiny.dxf'), width=0.4, height=0.4)
        _, result = _build(tiny, tool=0.0625)
        self.assertNotIn('ENGRAVE PART NAME', result.gcode)
        self.assertTrue(result.warnings)

    def test_the_name_shrinks_to_fit_rather_than_overflowing(self):
        small = _plate(os.path.join(self.tmp, 'small.dxf'), width=1.2, height=1.0)
        _, result = _build(small, tool=0.03125, text='A-VERY-LONG-PART-NAME')
        # Asserted, not guarded by `if`: an `if` here made the whole test vacuous the
        # moment the engraving stopped being emitted.
        self.assertIn('ENGRAVE PART NAME', result.gcode)
        cap = float(re.search(r'Cap height ([\d.]+) in', result.gcode).group(1))
        self.assertLess(cap, 0.18, 'the name did not shrink to fit')

    def test_the_program_still_obeys_the_g_code_rules(self):
        """Part names are user text going into a comment - the exact place this
        project's ASCII and nested-paren rules get broken."""
        _, result = _build(self.dxf, text='Bracket (left) [v2]')
        # The whole program is clean when there is no engraving at all, so prove the
        # user text actually reached a comment before checking the rules hold.
        self.assertIn('(Text: ', result.gcode)
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


class MultiToolEngraveTest(unittest.TestCase):
    """Multi-tool jobs never called `generate_gcode`, so an engraving hooked only into
    the single-part routes was silently dropped - while the summary said "names
    engraved". Silence is the failure mode this whole feature has to avoid."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mtengrave_')
        self.dxf = os.path.join(self.tmp, 'p.dxf')
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (5, 0), (5, 4), (0, 4)], close=True)
        msp.add_circle((2.5, 2.0), 0.75)     # a bore the label must keep out of
        doc.saveas(self.dxf)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, engrave):
        import tooling
        job = tooling.MultiToolJob(
            material='plywood', thickness=0.25, engrave=engrave,
            tools=[tooling.Tool(1, '1/8 endmill', 0.125, 2)],
            parts=[tooling.PartOps(dxf_path=self.dxf, name='GEARBOX-L', operations=[
                tooling.Operation('holes', 1), tooling.Operation('perimeter', 1)])],
            config=TeamConfig())
        with redirect_stdout(io.StringIO()):
            return tooling.generate_multitool_job(job, timestamp='2026-01-01 00:00:00')

    def test_custom_number_and_position_reach_a_multi_tool_program(self):
        import tooling
        job = tooling.MultiToolJob(
            material='plywood', thickness=0.25, engrave=True,
            tools=[tooling.Tool(1, '1/8 endmill', 0.125, 2)],
            parts=[tooling.PartOps(
                dxf_path=self.dxf, name='GEARBOX-L', engrave_text='ARM #42',
                engrave_anchor=(1.0, 1.0),
                operations=[tooling.Operation('holes', 1),
                            tooling.Operation('perimeter', 1)])],
            config=TeamConfig())
        with redirect_stdout(io.StringIO()):
            result = tooling.generate_multitool_job(job, timestamp='2026-01-01 00:00:00')
        self.assertIn('(Text: ARM #42)', result.gcode)

    def test_the_name_is_cut_when_the_job_asks_for_it(self):
        off = self._run(False)
        on = self._run(True)
        self.assertTrue(on.success, msg=str(on.errors))
        self.assertNotIn('ENGRAVE PART NAME', off.gcode)
        self.assertIn('ENGRAVE PART NAME', on.gcode)
        self.assertIn('(Text: GEARBOX-L)', on.gcode)

    def test_it_is_cut_before_the_part_is_freed(self):
        gcode = self._run(True).gcode
        self.assertLess(gcode.index('ENGRAVE PART NAME'), gcode.index('PERIMETER'),
                        'the name was cut after the profile, on a part hanging on tabs')

    def test_it_sees_the_whole_part_not_one_operation_s_scope(self):
        """An operation narrows `pp.holes`/`pp.pockets` to its own scope - a perimeter
        operation clears them entirely. Built on that view, the label would be placed
        over a bore and machined away with the slug."""
        from shapely.geometry import Point, Polygon
        lines = self._run(True).gcode.splitlines()
        start = next(i for i, l in enumerate(lines) if 'ENGRAVE PART NAME' in l)
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith('(====='))
        pts, x, y = [], None, None
        for line in lines[start:end]:
            mx = re.search(r'X(-?[\d.]+)', line)
            my = re.search(r'Y(-?[\d.]+)', line)
            if mx:
                x = float(mx.group(1))
            if my:
                y = float(my.group(1))
            if mx and x is not None and y is not None:
                pts.append((x, y))
        self.assertGreater(len(pts), 20, 'suspiciously few engraved moves')
        radius = 0.0625
        bore = Point(2.5, 2.0).buffer(0.75 / 2 + radius)
        plate = Polygon([(0, 0), (5, 0), (5, 4), (0, 4)]).buffer(-radius)
        for p in pts:
            self.assertFalse(bore.intersects(Point(p)), f'{p} is over the bore')
            self.assertTrue(plate.contains(Point(p)), f'{p} is off the part')
