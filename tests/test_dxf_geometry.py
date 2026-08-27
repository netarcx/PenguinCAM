"""Reading the DXF: what the drawing says versus what PenguinCAM machines.

Every test here is about a DIFFERENCE between the two that the operator was never told
about - a curve read as a straight line, a gap welded shut, a boundary dropped. A
refusal is fine and a warning is fine; machining a different part in silence is not.
"""

import io
import math
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import dxf_geometry
from frc_cam_postprocessor import FRCPostProcessor


def bulged_slot(path, length=1.0, width=0.25, layer=None):
    """A 1.0 x 0.25 slot with true semicircular ends, drawn the way Fusion, SolidWorks,
    QCAD and LibreCAD draw one: a four-vertex LWPOLYLINE whose end vertices carry
    bulge=1 (a half turn). Overall length is `length` + `width`."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    attribs = {'layer': layer} if layer else {}
    if layer and layer not in doc.layers:
        doc.layers.new(name=layer)
    msp.add_lwpolyline(
        [(0.0, 0.0, 0.0), (length, 0.0, 1.0), (length, width, 0.0), (0.0, width, 1.0)],
        format='xyb', close=True, dxfattribs=attribs)
    doc.saveas(path)
    return path


def bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


class TestPolylineBulges(unittest.TestCase):
    """A polyline vertex carries a `bulge` - the tangent of a quarter of the arc's
    included angle to the next vertex. Reading only x and y discards it, so a slot with
    semicircular ends loaded as a plain RECTANGLE and was machined as one. Onshape
    exports LINE/ARC entities and never showed the bug; every other CAD tool does.
    """

    def _loaded(self, **kwargs):
        path = tempfile.mktemp(suffix='.dxf')
        bulged_slot(path, **kwargs)
        try:
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(0.25, 0.125)
                pp.apply_material_preset('plywood')
                pp.load_dxf(path)
            return pp
        finally:
            os.remove(path)

    def test_the_slot_is_as_long_as_it_was_drawn(self):
        pp = self._loaded()
        self.assertTrue(pp.polylines, 'the slot did not load at all')
        x0, y0, x1, y1 = bbox(pp.polylines[0])
        self.assertAlmostEqual(x1 - x0, 1.25, places=3,
                               msg='the bulged ends were dropped; the slot loaded as a '
                                   'plain rectangle 1.00 long')
        self.assertAlmostEqual(y1 - y0, 0.25, places=3)

    def test_the_arcs_are_actually_curved(self):
        pp = self._loaded()
        points = pp.polylines[0]
        self.assertGreater(len(points), 20,
                           f'only {len(points)} vertices - the ends are not arcs')
        # Every point on an end cap sits on its circle, to within the chord tolerance.
        centre = (1.0, 0.125)
        on_cap = [p for p in points if p[0] > 1.0 + 1e-9]
        self.assertGreater(len(on_cap), 5, 'no points beyond the straight section')
        for p in on_cap:
            self.assertAlmostEqual(math.hypot(p[0] - centre[0], p[1] - centre[1]),
                                   0.125, delta=dxf_geometry.CHORD_TOLERANCE * 2)

    def test_a_bulge_free_polyline_is_unchanged(self):
        """The fast path stays fast, and its vertices stay exactly as drawn."""
        doc = ezdxf.new('R2010')
        doc.modelspace().add_lwpolyline([(0, 0), (2, 0), (2, 1), (0, 1)], close=True)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        try:
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(0.25, 0.125)
                pp.apply_material_preset('plywood')
                pp.load_dxf(path)
            self.assertEqual([(round(x, 6), round(y, 6)) for x, y in pp.polylines[0]],
                             [(0, 0), (2, 0), (2, 1), (0, 1)])
        finally:
            os.remove(path)

    def test_the_helper_flattens_a_bare_entity(self):
        doc = ezdxf.new('R2010')
        entity = doc.modelspace().add_lwpolyline(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 1.0), (1.0, 0.25, 0.0), (0.0, 0.25, 1.0)],
            format='xyb', close=True)
        points = dxf_geometry.polyline_points(entity)
        x0, _, x1, _ = bbox(points)
        self.assertAlmostEqual(x1 - x0, 1.25, places=3)



class TestHatchBulges(unittest.TestCase):
    """The 2.5D reader takes solid regions from HATCH boundary paths, whose vertices
    carry bulges exactly as an LWPOLYLINE's do."""

    def test_a_bulged_hatch_boundary_keeps_its_arcs(self):
        doc = ezdxf.new('R2010')
        hatch = doc.modelspace().add_hatch()
        hatch.paths.add_polyline_path(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 1.0), (1.0, 0.25, 0.0), (0.0, 0.25, 1.0)],
            is_closed=True)
        points = dxf_geometry.hatch_path_points(hatch.paths[0])
        x0, _, x1, _ = bbox(points)
        self.assertAlmostEqual(x1 - x0, 1.25, places=3)
        self.assertGreater(len(points), 20)

    def test_a_plain_hatch_boundary_is_unchanged(self):
        doc = ezdxf.new('R2010')
        hatch = doc.modelspace().add_hatch()
        hatch.paths.add_polyline_path([(0, 0), (2, 0), (2, 1), (0, 1)], is_closed=True)
        points = dxf_geometry.hatch_path_points(hatch.paths[0])
        self.assertEqual([(round(x, 6), round(y, 6)) for x, y in points],
                         [(0, 0), (2, 0), (2, 1), (0, 1)])



def comment_faults(gcode):
    """Every CLAUDE.md comment rule broken by this program, as (line number, why)."""
    faults = []
    for number, line in enumerate(gcode.splitlines(), 1):
        try:
            line.encode('ascii')
        except UnicodeEncodeError:
            faults.append((number, f'non-ASCII: {line[:60]}'))
        depth = deepest = 0
        for char in line.split(';')[0]:
            if char == '(':
                depth += 1
                deepest = max(deepest, depth)
            elif char == ')':
                depth -= 1
        if deepest > 1:
            faults.append((number, f'nested comment: {line[:60]}'))
        inside = False
        for char in line:
            if char == '(':
                inside = True
            elif char == ')':
                inside = False
            elif inside and char in '[]':
                faults.append((number, f'bracket in comment: {line[:60]}'))
                break
    return faults


class TestHostileTextInHeaders(unittest.TestCase):
    """Names and timestamps reach the header straight from a Google account, an Onshape
    session and a form field. The plate header learned to sanitise them; the tube header
    did not, and neither did the coolant name or the timestamp anywhere.
    """

    HOSTILE_USER = 'Trent (Coach) Fox José'
    HOSTILE_TIME = '2026-08-27 (12:00) [utc]—'
    HOSTILE_COOLANT = 'Air (comp) [shop]'

    def _config(self):
        from team_config import TeamConfig
        return TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'M', 'machine': {'coolant': self.HOSTILE_COOLANT}}}})

    def test_a_tube_program_survives_it(self):
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(0.0625, 0.157, config=self._config())
            pp.apply_material_preset('aluminum_tube')
            pp.tube_height = 1.0
            pp.load_tube_pattern(2.0, 12.0, mode='lightening')
            pp.user_name = self.HOSTILE_USER
            result = pp.generate_tube_pattern_gcode(
                tube_height=1.0, square_end=True, cut_to_length=False,
                tube_width=2.0, tube_length=12.0, timestamp=self.HOSTILE_TIME)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(comment_faults(result.gcode), [])

    def test_a_tube_facing_program_survives_it(self):
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(0.0625, 0.157, config=self._config())
            pp.apply_material_preset('aluminum_tube')
            pp.user_name = self.HOSTILE_USER
            result = pp.generate_tube_facing_gcode(tube_size='1x1',
                                                   timestamp=self.HOSTILE_TIME)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(comment_faults(result.gcode), [])

    def test_a_plate_program_survives_it(self):
        doc = ezdxf.new('R2010')
        doc.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        try:
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(0.25, 0.157, config=self._config())
                pp.apply_material_preset('plywood')
                pp.user_name = self.HOSTILE_USER
                pp.load_dxf(path)
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                result = pp.generate_gcode(timestamp=self.HOSTILE_TIME)
            self.assertTrue(result.success, result.errors)
            self.assertEqual(comment_faults(result.gcode), [])
        finally:
            os.remove(path)

    def test_a_park_position_is_formatted_not_repr(self):
        """An unformatted YAML float emits X1e-05, which GRBL rejects outright."""
        from team_config import TeamConfig
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'M', 'machine': {'park_position': {'x': 0.00001, 'y': -0.5,
                                                       'z': -0.25}}}}})
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(0.25, 0.157, config=cfg)
            pp.apply_material_preset('plywood')
            park = pp._park_gcode()
        self.assertTrue(park)
        for line in park:
            self.assertNotIn('e-', line)
            self.assertNotIn('e+', line)



class TestCircleClassification(unittest.TestCase):
    """`_path_as_circle` turns a tessellated boundary back into a hole so the hole
    classifier can pick peck / helical / contour by size. At 0.97 circularity it also
    accepted stadiums up to about 1.3:1 - a 0.20 x 0.26 adjustment slot in a 2.5D or
    STEP part was machined as a 0.235 round hole at its centroid.
    """

    @staticmethod
    def circle_coords(cx, cy, r, n=64):
        return [(cx + r * math.cos(2 * math.pi * i / n),
                 cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]

    @staticmethod
    def stadium_coords(length, width, n=48):
        """A slot: two straight sides and two semicircular ends, overall `length` long."""
        r = width / 2.0
        straight = length - width
        points = []
        for i in range(n // 2 + 1):                       # right cap
            a = -math.pi / 2 + math.pi * i / (n // 2)
            points.append((straight / 2 + r * math.cos(a), r * math.sin(a)))
        for i in range(n // 2 + 1):                       # left cap
            a = math.pi / 2 + math.pi * i / (n // 2)
            points.append((-straight / 2 + r * math.cos(a), r * math.sin(a)))
        return points

    def setUp(self):
        with redirect_stdout(io.StringIO()):
            self.pp = FRCPostProcessor(0.25, 0.125)

    def test_a_tessellated_circle_is_still_a_hole(self):
        for radius in (0.1, 0.375, 1.0):
            with self.subTest(radius=radius):
                found = self.pp._path_as_circle(self.circle_coords(1.0, 1.0, radius))
                self.assertIsNotNone(found, 'a real circle stopped being recognised')
                # Tessellation loses a hair of area; the tolerance is the chord one.
                self.assertAlmostEqual(found['diameter'], radius * 2, delta=0.005)

    def test_a_short_slot_is_not_a_hole(self):
        """0.20 x 0.26: only 1.3:1, and it used to pass."""
        self.assertIsNone(self.pp._path_as_circle(self.stadium_coords(0.26, 0.20)))

    def test_longer_slots_are_not_holes_either(self):
        for length, width in ((0.30, 0.20), (0.5, 0.25), (1.25, 0.25)):
            with self.subTest(length=length, width=width):
                self.assertIsNone(
                    self.pp._path_as_circle(self.stadium_coords(length, width)))

    def test_polygons_are_not_holes(self):
        for sides in (8, 12, 16):
            with self.subTest(sides=sides):
                # A regular polygon IS close to a circle at high side counts; the point
                # of the radius test is that it must stay inside the same tolerance.
                coords = self.circle_coords(0, 0, 0.5, n=sides)
                found = self.pp._path_as_circle(coords)
                if found is not None:
                    self.assertAlmostEqual(found['diameter'], 1.0, delta=0.05)



def gapped_outline(path, gap, size=(4.0, 3.0), pocket=True):
    """A rectangle drawn as four LINEs with `gap` missing from one side, plus an inner
    closed pocket. This is how a CAD export loses an outer profile: one edge short."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    w, h = size
    msp.add_line((0, 0), (w, 0))
    msp.add_line((w, 0), (w, h))
    msp.add_line((w, h), (gap, h))          # short by `gap`
    msp.add_line((0, h), (0, 0))
    if pocket:
        msp.add_lwpolyline([(1, 1), (3, 1), (3, 2), (1, 2)], close=True)
    doc.saveas(path)
    return path


class TestOpenOutlines(unittest.TestCase):
    """An outer profile that does not close is not a cosmetic problem.

    The chain was dropped in silence, the biggest remaining closed loop - the POCKET -
    was promoted to perimeter, and the program profiled through the middle of the part
    with tabs. And a gap under the 0.1" closing tolerance was welded shut with no word
    either, quietly moving an edge.
    """

    def _load(self, gap, **kwargs):
        path = tempfile.mktemp(suffix='.dxf')
        gapped_outline(path, gap, **kwargs)
        try:
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(0.25, 0.157)
                pp.apply_material_preset('plywood')
                pp.load_dxf(path)
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                result = pp.generate_gcode(timestamp='2026-08-27 12:00')
            return pp, result
        finally:
            os.remove(path)

    def test_a_lost_outer_profile_is_a_hard_error(self):
        pp, result = self._load(0.15)
        self.assertFalse(result.success,
                         'the pocket was promoted to perimeter and a program shipped')
        joined = ' '.join(result.errors)
        self.assertIn('0.15', joined)
        self.assertTrue('close' in joined.lower() or 'gap' in joined.lower(), joined)

    def test_a_welded_gap_is_reported(self):
        pp, result = self._load(0.08)
        self.assertTrue(result.success, result.errors)
        joined = ' '.join(result.warnings)
        self.assertIn('0.08', joined)

    def test_a_tight_gap_stays_quiet(self):
        """CAD endpoints land microns apart. That is not news."""
        pp, result = self._load(0.005)
        self.assertTrue(result.success, result.errors)
        self.assertFalse([w for w in result.warnings if 'gap' in w.lower()],
                         result.warnings)

    def test_a_stray_open_chain_does_not_block_a_good_part(self):
        """An open chain SMALLER than the perimeter is a stray line, not a lost
        outline. Warn about it; do not refuse the part."""
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True)
        msp.add_line((1, 1), (2, 1))        # a stray, going nowhere
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        try:
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(0.25, 0.157)
                pp.apply_material_preset('plywood')
                pp.load_dxf(path)
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                result = pp.generate_gcode(timestamp='2026-08-27 12:00')
            self.assertTrue(result.success, result.errors)
        finally:
            os.remove(path)



class TestHeaderUnitsCrossCheck(unittest.TestCase):
    """$INSUNITS says what the drawing thinks its numbers mean. When it contradicts the
    units the job was set up in, one of the two is wrong by a factor of 25.4 - and
    nothing else in the pipeline would ever notice. The header is unreliable in the
    wild, so this warns; it never converts.
    """

    def _load(self, insunits, units='inch', thickness=0.25, tool=0.157):
        doc = ezdxf.new('R2010')
        doc.header['$INSUNITS'] = insunits
        doc.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True)
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        try:
            with redirect_stdout(io.StringIO()) as out:
                pp = FRCPostProcessor(thickness, tool, units=units)
                pp.apply_material_preset('plywood')
                pp.load_dxf(path)
            return pp, out.getvalue()
        finally:
            os.remove(path)

    def test_a_millimetre_drawing_in_an_inch_job_warns(self):
        pp, printed = self._load(4)                    # 4 = millimetres
        joined = ' '.join(pp.geometry_warnings)
        self.assertIn('25.4', joined, pp.geometry_warnings)
        self.assertIn('millimet', joined.lower())
        self.assertIn('inch', joined.lower())
        self.assertIn('25.4', printed)                 # and loudly, on the console

    def test_an_inch_drawing_in_a_millimetre_job_warns(self):
        pp, _ = self._load(1, units='mm', thickness=6.35, tool=4.0)
        joined = ' '.join(pp.geometry_warnings)
        self.assertIn('25.4', joined, pp.geometry_warnings)

    def test_agreement_is_silent(self):
        for insunits, units, thickness, tool in ((1, 'inch', 0.25, 0.157),
                                                 (4, 'mm', 6.35, 4.0)):
            with self.subTest(insunits=insunits):
                pp, _ = self._load(insunits, units, thickness, tool)
                self.assertFalse([w for w in pp.geometry_warnings if '25.4' in w],
                                 pp.geometry_warnings)

    def test_unitless_is_silent(self):
        """0 means "no units stated", which is most exports. Not news."""
        pp, _ = self._load(0)
        self.assertFalse([w for w in pp.geometry_warnings if '25.4' in w],
                         pp.geometry_warnings)

    def test_an_exotic_unit_is_silent(self):
        """Only inches and millimetres are worth cross-checking; a drawing in metres or
        feet is not something PenguinCAM can second-guess usefully."""
        pp, _ = self._load(6)                          # 6 = metres
        self.assertFalse([w for w in pp.geometry_warnings if '25.4' in w],
                         pp.geometry_warnings)


if __name__ == '__main__':
    unittest.main()
