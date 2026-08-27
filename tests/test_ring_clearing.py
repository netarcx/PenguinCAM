"""Regression test for the circular-ring clearing gouge (Class A toolpath bug).

The ring spiral used to reposition to angle 0 with a straight G1 after each
Archimedean spiral, cutting a CHORD across the ring interior that gouged the
central island (found on the Turntable part: a 3.76" x 0.07"-deep slot across
solid keep-material). This asserts no cutting move in the ring path comes closer
to the ring center than the inner radius.
"""
import math
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


if __name__ == '__main__':
    unittest.main()


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
