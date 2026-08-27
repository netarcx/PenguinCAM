"""Regression test for the circular-ring clearing gouge (Class A toolpath bug).

The ring spiral used to reposition to angle 0 with a straight G1 after each
Archimedean spiral, cutting a CHORD across the ring interior that gouged the
central island (found on the Turntable part: a 3.76" x 0.07"-deep slot across
solid keep-material). This asserts no cutting move in the ring path comes closer
to the ring center than the inner radius.
"""
import math
import re
import unittest

from shapely.geometry import Point

from frc_cam_postprocessor import FRCPostProcessor
from gcode_sim import parse_moves


def _pt_seg_dist(px, py, ax, ay, bx, by):
    """Distance from point (px,py) to segment (ax,ay)-(bx,by)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class RingClearingTest(unittest.TestCase):

    def test_ring_does_not_gouge_the_island(self):
        pp = FRCPostProcessor(0.25, 0.157, units='inch')
        pp.apply_material_preset('plywood')

        cx, cy = 5.0, 5.0
        outer_r, inner_r = 2.0, 1.0   # a wide ring -> big spirals -> big chord if buggy
        ring = Point(cx, cy).buffer(outer_r).difference(Point(cx, cy).buffer(inner_r))

        gcode = pp._generate_circular_ring_gcode(ring, ring, cx, cy, outer_r, inner_r)

        # Prefix a safe-Z rapid so the initial XY positioning move is above stock
        # (and thus excluded as non-cutting).
        text = "G20\nG0 Z1.0\n" + "\n".join(gcode)
        moves = parse_moves(text)

        mt = pp.material_top
        worst = inner_r
        for kind, x0, y0, z0, x1, y1, z1 in moves:
            if kind != 'feed':
                continue
            if z0 >= mt - 1e-9 and z1 >= mt - 1e-9:
                continue  # not cutting
            worst = min(worst, _pt_seg_dist(cx, cy, x0, y0, x1, y1))

        # No cutting move should dip meaningfully inside the inner wall.
        self.assertGreaterEqual(
            worst, inner_r - 0.02,
            f"ring cutting move gouged to r={worst:.4f} (inner wall r={inner_r})")

    def test_ring_still_clears_the_band(self):
        # Sanity: the ring path still reaches both walls (didn't get gutted).
        pp = FRCPostProcessor(0.25, 0.157, units='inch')
        pp.apply_material_preset('plywood')
        cx, cy = 5.0, 5.0
        outer_r, inner_r = 2.0, 1.0
        ring = Point(cx, cy).buffer(outer_r).difference(Point(cx, cy).buffer(inner_r))
        gcode = "\n".join(pp._generate_circular_ring_gcode(ring, ring, cx, cy, outer_r, inner_r))
        self.assertIn('Circular ring spiral clearing', gcode)
        self.assertIn('Outer cleanup circle', gcode)
        self.assertIn('Inner cleanup circle', gcode)



class RectangularIslandTest(unittest.TestCase):
    """The island-aware pocket path linked its contour rings with a bare feed move.

    Only CIRCULAR rings were diverted to the spiral clearer; a rectangular island left
    the tool feeding in a straight line at full depth from wherever it happened to be to
    the next ring's start - straight across the island if the geometry said so. The safe
    pattern (retract, rapid over, ramp back down) already existed for the plain pocket
    path; it now covers this one too.
    """

    ISLAND = [(1.5, 1.0), (2.5, 1.0), (2.5, 2.0), (1.5, 2.0)]

    def _pocket(self, tool=0.25):
        import io
        from contextlib import redirect_stdout
        from shapely.geometry import Polygon

        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(0.25, tool, units='inch')
            pp.apply_material_preset('plywood')
            pp.material_top = 0.25
            pp.cut_depth = -0.008
            poly = Polygon([(0, 0), (4, 0), (4, 3), (0, 3)], [self.ISLAND])
            gcode = pp._generate_pocket_gcode_from_polygon(poly)
        return pp, gcode

    def test_no_cutting_move_crosses_the_island(self):
        from shapely.geometry import LineString, Polygon

        pp, gcode = self._pocket()
        keep = Polygon(self.ISLAND).buffer(pp.tool_radius - 1e-3)
        text = "G20\nG0 Z1.0\n" + "\n".join(gcode)
        offenders = []
        for kind, x0, y0, z0, x1, y1, z1 in parse_moves(text):
            if kind != 'feed':
                continue
            if z0 >= pp.material_top - 1e-9 and z1 >= pp.material_top - 1e-9:
                continue                    # above the stock, not a cut
            if keep.intersects(LineString([(x0, y0), (x1, y1)])):
                offenders.append(((x0, y0), (x1, y1), min(z0, z1)))
        self.assertFalse(
            offenders,
            f"{len(offenders)} cutting move(s) go through the island, first "
            f"{offenders[0] if offenders else ''}")

    def test_it_still_clears_both_sides_of_the_island(self):
        pp, gcode = self._pocket()
        text = "\n".join(gcode)
        self.assertIn('Contour pass', text)
        xs = [x for _, x, _, _, _, _, _ in
              [(k, a, b, c, d, e, f) for k, a, b, c, d, e, f
               in parse_moves("G20\nG0 Z1.0\n" + text)]]
        self.assertTrue(any(x < 1.5 for x in xs), 'nothing cut left of the island')
        self.assertTrue(any(x > 2.5 for x in xs), 'nothing cut right of the island')


class NarrowPocketHelixTest(unittest.TestCase):
    """The helical entry bores a hole the width of the tool plus twice the helix radius.

    In a slot barely wider than the cutter there is no room for that, and the plain
    pocket path took the preset radius regardless: on a 0.20"-wide slot with a 0.157"
    tool it swept 0.157/2 + 0.0589 = 0.137 from centre against a 0.10 half-width, cutting
    0.037" into each wall before the toolpath proper began. The island-aware sibling has
    clamped this for a while; this is the same clamp on the plain path.
    """

    SLOT = [(1.0, 1.0), (3.0, 1.0), (3.0, 1.20), (1.0, 1.20)]   # 2.0 x 0.20

    def _entry_moves(self, tool=0.157, material='plywood'):
        import io
        import re
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(0.25, tool, units='inch')
            pp.apply_material_preset(material)
            gcode = pp._generate_pocket_gcode(self.SLOT)
        arcs = []
        x = y = None
        for line in gcode:
            code = line.split('(')[0].split(';')[0].strip()
            mx = re.search(r'\bX(-?[\d.]+)', code)
            my = re.search(r'\bY(-?[\d.]+)', code)
            if mx:
                x = float(mx.group(1))
            if my:
                y = float(my.group(1))
            mi = re.search(r'\bI(-?[\d.]+)', code)
            mj = re.search(r'\bJ(-?[\d.]+)', code)
            if code.startswith(('G2', 'G3')) and mi and mj and x is not None:
                arcs.append((x + float(mi.group(1)), y + float(mj.group(1)),
                             math.hypot(float(mi.group(1)), float(mj.group(1)))))
        return pp, gcode, arcs

    def test_the_entry_bore_stays_inside_the_slot(self):
        pp, gcode, arcs = self._entry_moves()
        self.assertTrue(arcs, 'no helical entry arcs found')
        half_width = 0.20 / 2.0
        for cx, cy, radius in arcs:
            swept = radius + pp.tool_radius
            self.assertLessEqual(
                swept, half_width + 1e-6,
                f"the entry bore sweeps {swept:.4f} from centre in a slot only "
                f"{half_width:.4f} half-wide - it cuts into both walls")

    def test_it_still_enters_helically_when_there_is_room(self):
        pp, gcode, arcs = self._entry_moves(tool=0.125)
        text = '\n'.join(gcode)
        self.assertIn('helical entry', text)
        self.assertTrue(arcs)
        self.assertGreater(max(r for _, _, r in arcs), 0.0)

    def test_a_wide_pocket_keeps_the_preset_radius(self):
        import io
        from contextlib import redirect_stdout
        import re

        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(0.25, 0.157, units='inch')
            pp.apply_material_preset('plywood')
            gcode = pp._generate_pocket_gcode([(0, 0), (3, 0), (3, 3), (0, 3)])
        radii = [abs(float(m.group(1))) for line in gcode
                 for m in [re.search(r'\bI(-?[\d.]+)', line.split(';')[0])]
                 if m and line.split(';')[0].strip().startswith(('G2', 'G3'))]
        self.assertTrue(radii)
        expected = pp.tool_radius * pp.helix_radius_multiplier
        self.assertAlmostEqual(max(radii), expected, places=4)



class HoleAtToolDiameterTest(unittest.TestCase):
    """A team config with min_millable_multiplier at 1.0 lets a hole barely wider than
    the cutter take the MILLING path, and the milling path has nowhere to go.

    At a toolpath radius of 0.000025 the helical entry emitted `G3 ... I-0.0000 J0` -
    a zero-radius arc, which GRBL answers with error:33 - 7137 times. And a hole the tool
    is genuinely too big for was dropped with nothing but a G-code comment, so the
    program reported success with the feature missing.
    """

    PERMISSIVE = None   # set in setUp; TeamConfig import is local to this module

    def setUp(self):
        import io
        from contextlib import redirect_stdout
        from team_config import TeamConfig
        self.cfg = TeamConfig({'machining': {'holes': {'min_millable_multiplier': 1.0}}})
        self._io, self._redirect = io, redirect_stdout

    def _hole(self, diameter, tool=0.157):
        with self._redirect(self._io.StringIO()):
            pp = FRCPostProcessor(0.25, tool, config=self.cfg)
            pp.apply_material_preset('plywood')
            pp.circles = [{'center': (1.0, 1.0), 'diameter': diameter,
                           'radius': diameter / 2}]
            pp.polylines = [[(0, 0), (4, 0), (4, 3), (0, 3)]]
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            needs_peck = pp.holes[0]['needs_peck_drill'] if pp.holes else False
            gcode = pp._generate_hole_gcode(1.0, 1.0, diameter,
                                            needs_peck_drill=needs_peck)
        return pp, gcode

    def test_no_zero_radius_arc_is_ever_emitted(self):
        for diameter in (0.15701, 0.15705, 0.1571, 0.1575, 0.158):
            with self.subTest(diameter=diameter):
                pp, gcode = self._hole(diameter)
                for line in gcode:
                    for word in ('I', 'J'):
                        m = re.search(rf'\b{word}(-?[\d.]+)', line.split(';')[0])
                        if m and line.split(';')[0].strip().startswith(('G2', 'G3')):
                            self.assertNotAlmostEqual(
                                abs(float(m.group(1))), 0.0, places=4,
                                msg=f'zero-radius arc: {line}')

    def test_a_hair_of_stock_is_pecked_not_milled(self):
        pp, gcode = self._hole(0.15705)
        text = '\n'.join(gcode)
        self.assertIn('Peck', text)
        self.assertNotIn('Helical pass', text)

    def test_a_hole_the_tool_cannot_make_fails_the_program(self):
        """Not a comment in a 'successful' program.

        classify_holes catches this for a DXF, but the multilayer and tube paths reach
        the hole generator by other routes, so the generator has to refuse for itself.
        """
        with self._redirect(self._io.StringIO()):
            pp = FRCPostProcessor(0.25, 0.25, config=self.cfg)
            pp.apply_material_preset('plywood')
            gcode = pp._generate_hole_gcode(1.0, 1.0, 0.157, needs_peck_drill=False)
        self.assertTrue(pp.errors, 'the dropped hole was only a comment')
        joined = ' '.join(pp.errors)
        self.assertIn('0.157', joined)
        self.assertIn('0.250', joined)

    def test_an_ordinary_hole_is_still_milled(self):
        pp, gcode = self._hole(0.5)
        text = '\n'.join(gcode)
        self.assertIn('Helical pass', text)
        self.assertFalse(pp.errors, pp.errors)


if __name__ == '__main__':
    unittest.main()
