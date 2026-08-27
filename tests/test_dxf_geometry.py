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


if __name__ == '__main__':
    unittest.main()
