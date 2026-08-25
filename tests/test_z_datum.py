"""Where the operator zeros Z: the sacrifice board (the default) or the top of the stock.

The whole feature is one claim, and it is the claim these tests are built around:

    THE TWO DATUMS DESCRIBE THE SAME MOTION, RENUMBERED.

Not "similar" motion. The same program with every Z word shifted by exactly the stock
thickness, every X, Y, F and comment character-identical. If that holds, nothing about
the cut can change when a team switches datum - only the number the operator touches
off on. If it ever stops holding, some path is computing Z from zero instead of from
material_top, and a part will be cut a thickness out of position.

The rest checks the things a wrong datum would make dangerous: the header must say which
surface it means, every pause must ask for the right one, and a tube job - which is
zeroed to its jig, in a frame built by lifting the plate toolpath - must ignore the
setting entirely rather than shift by a wall thickness.
"""
import math
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

from frc_cam_postprocessor import (
    FRCPostProcessor, Z_DATUM_BOARD, Z_DATUM_STOCK_TOP, normalize_z_datum,
)
from team_config import TeamConfig
import tooling

#: Z words, but not the Z inside a word like "ZMIN" or a tool name.
Z_WORD = re.compile(r'(?<![A-Za-z])Z(-?\d+\.?\d*)')


def _build_program(dxf, thickness, datum, chamfer=None):
    """One plate, cut through, in whichever Z frame is asked for."""
    pp = FRCPostProcessor(material_thickness=thickness, tool_diameter=0.157,
                          config=TeamConfig(), z_datum=datum)
    pp.apply_material_preset('plywood')
    if chamfer is not None:
        pp.chamfer_pass = chamfer
    pp.load_dxf(dxf)
    pp.transform_coordinates('bottom-left', 0)
    pp.identify_perimeter_and_pockets()
    pp.classify_holes()
    result = pp.generate_gcode()
    if not result.success:
        raise AssertionError(f'generation failed: {result.errors}')
    return result.gcode


def _plate_dxf(path, size=4.0, hole_diameter=0.5, pocket=(1.0, 1.0, 2.0, 2.0)):
    """A plate with a hole and a pocket, so the comparison covers a profile with tabs,
    a bored hole and a cleared pocket - three different ways of arriving at a Z."""
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (size, 0), (size, size), (0, size)], close=True)
    if hole_diameter:
        msp.add_circle((size / 2, size * 0.75), hole_diameter / 2)
    if pocket:
        x0, y0, x1, y1 = pocket
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True)
    doc.saveas(path)
    return path


class ZDatumFrameTest(unittest.TestCase):
    """The three numbers that place a program on the Z axis."""

    THICK = 0.25

    def _pp(self, datum, thickness=None):
        return FRCPostProcessor(material_thickness=thickness or self.THICK,
                                tool_diameter=0.157, z_datum=datum)

    def test_board_datum_is_unchanged(self):
        """The default frame is the one every existing program was cut with."""
        pp = self._pp(Z_DATUM_BOARD)
        self.assertAlmostEqual(pp.material_top, self.THICK)
        self.assertAlmostEqual(pp.retract_height, self.THICK + pp.clearance_height)
        self.assertAlmostEqual(pp.cut_depth, -pp.sacrifice_board_depth)
        self.assertAlmostEqual(pp.z_shift, 0.0)

    def test_stock_top_datum_hangs_the_part_below_zero(self):
        pp = self._pp(Z_DATUM_STOCK_TOP)
        self.assertAlmostEqual(pp.material_top, 0.0)
        self.assertAlmostEqual(pp.retract_height, pp.clearance_height)
        self.assertAlmostEqual(pp.cut_depth, -(self.THICK + pp.sacrifice_board_depth))
        self.assertAlmostEqual(pp.z_shift, -self.THICK)

    def test_frames_differ_by_exactly_the_thickness(self):
        board, top = self._pp(Z_DATUM_BOARD), self._pp(Z_DATUM_STOCK_TOP)
        for attr in ('material_top', 'retract_height', 'cut_depth'):
            self.assertAlmostEqual(getattr(board, attr) - getattr(top, attr), self.THICK,
                                   msg=f'{attr} did not shift by the stock thickness')

    def test_safe_retract_is_the_same_height_off_the_table(self):
        """The configured ceiling is a height over the board, so it has to move with the
        datum - otherwise the stock-top program retracts a thickness higher than asked."""
        board, top = self._pp(Z_DATUM_BOARD), self._pp(Z_DATUM_STOCK_TOP)
        self.assertAlmostEqual(board._safe_z() - top._safe_z(), self.THICK)

    def test_deeper_sacrifice_cut_moves_the_bottom_not_the_top(self):
        pp = self._pp(Z_DATUM_STOCK_TOP)
        pp.sacrifice_board_depth = 0.05
        pp._apply_z_frame()
        self.assertAlmostEqual(pp.material_top, 0.0)
        self.assertAlmostEqual(pp.cut_depth, -(self.THICK + 0.05))

    def test_set_z_datum_switches_an_existing_post_processor(self):
        pp = self._pp(Z_DATUM_BOARD)
        pp.set_z_datum('stock-top')
        self.assertEqual(pp.z_datum, Z_DATUM_STOCK_TOP)
        self.assertAlmostEqual(pp.material_top, 0.0)

    def test_datum_spellings(self):
        for text in ('board', 'BOARD', 'sacrifice', 'sacrifice_board', 'spoilboard', 'bottom'):
            self.assertEqual(normalize_z_datum(text), Z_DATUM_BOARD, msg=text)
        for text in ('top', 'stock_top', 'stock-top', 'STOCK TOP', 'material_top'):
            self.assertEqual(normalize_z_datum(text), Z_DATUM_STOCK_TOP, msg=text)
        self.assertEqual(normalize_z_datum(None), Z_DATUM_BOARD)
        self.assertEqual(normalize_z_datum('', default=Z_DATUM_STOCK_TOP), Z_DATUM_STOCK_TOP)

    def test_an_unknown_datum_is_refused_not_guessed(self):
        """Falling back would zero the program a thickness away from the operator."""
        for text in ('middle', 'vise', 'z0', 'stock bottom'):
            with self.assertRaises(ValueError, msg=text):
                normalize_z_datum(text)


class ZDatumProgramTest(unittest.TestCase):
    """The same part, cut both ways, compared line for line."""

    THICK = 0.3125

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='zdatum_')
        cls.dxf = _plate_dxf(os.path.join(cls.tmp, 'plate.dxf'))
        cls.board = _build_program(cls.dxf, cls.THICK, Z_DATUM_BOARD)
        cls.top = _build_program(cls.dxf, cls.THICK, Z_DATUM_STOCK_TOP)

    def _program(self, datum, chamfer=None):
        return _build_program(self.dxf, self.THICK, datum, chamfer)

    def assertSameMotion(self, board_gcode, top_gcode, thickness):
        """Every Z word shifted by exactly `thickness`; everything else identical."""
        board_lines = board_gcode.splitlines()
        top_lines = top_gcode.splitlines()
        self.assertEqual(len(board_lines), len(top_lines),
                         'the two datums produced different numbers of lines')
        compared = 0
        for n, (b, t) in enumerate(zip(board_lines, top_lines), start=1):
            if b == t or b.lstrip().startswith('('):
                continue    # comments carry the datum's own words; checked separately
            self.assertEqual(Z_WORD.sub('Z*', b), Z_WORD.sub('Z*', t),
                             f'line {n} differs in something other than Z')
            for zb, zt in zip(Z_WORD.findall(b), Z_WORD.findall(t)):
                compared += 1
                self.assertAlmostEqual(
                    float(zb) - float(zt), thickness, delta=1.01e-4,
                    msg=f'line {n}: Z{zb} vs Z{zt} is not a {thickness:.4f}" shift')
        self.assertGreater(compared, 20, 'suspiciously few Z moves compared')

    def test_same_motion_renumbered(self):
        self.assertSameMotion(self.board, self.top, self.THICK)

    def test_same_motion_with_a_chamfer_pass(self):
        """The deburr pass computes its own depth from the top face, and runs behind a
        tool change - two more chances to reach for zero instead of the top face."""
        spec = {'width': 0.02, 'bit_diameter': 0.5, 'bit_angle': 90,
                'targets': ['perimeter', 'holes']}
        from frc_cam_postprocessor import parse_chamfer_spec
        chamfer = parse_chamfer_spec(spec)
        self.assertSameMotion(self._program(Z_DATUM_BOARD, chamfer),
                              self._program(Z_DATUM_STOCK_TOP, chamfer), self.THICK)

    def test_no_move_goes_below_the_through_cut(self):
        """The stock-top program is negative throughout; it must still stop at the same
        physical depth, not a thickness deeper."""
        deepest = min(float(z) for z in Z_WORD.findall(self.top))
        self.assertAlmostEqual(deepest, -(self.THICK + 0.008), places=3)

    def test_header_names_the_surface_to_zero_on(self):
        self.assertIn('Z=0 is at SACRIFICE BOARD surface', self.board)
        self.assertIn('Material top: Z=0.3125', self.board)
        self.assertIn('ZERO Z TO THE SACRIFICE BOARD SURFACE', self.board)

        self.assertIn('Z=0 is at TOP OF STOCK', self.top)
        self.assertIn('Material top: Z=0.0000', self.top)
        self.assertIn('ZERO Z TO THE TOP OF THE STOCK', self.top)

    def test_zmin_matches_the_deepest_move(self):
        """The header's ZMIN is what an operator checks against their travel limit."""
        for gcode in (self.board, self.top):
            declared = float(re.search(r'\(ZMIN: (-?[\d.]+)', gcode).group(1))
            deepest = min(float(z) for z in Z_WORD.findall(gcode))
            self.assertAlmostEqual(declared, deepest, places=4)

    def test_every_pause_asks_for_the_right_surface(self):
        """A pause that names the wrong surface is how a correct program still crashes."""
        spec = {'width': 0.02, 'bit_diameter': 0.5, 'bit_angle': 90, 'targets': ['perimeter']}
        from frc_cam_postprocessor import parse_chamfer_spec
        top = self._program(Z_DATUM_STOCK_TOP, parse_chamfer_spec(spec))
        self.assertIn('Re-zero Z to the top of the stock', top)
        self.assertNotIn('sacrifice board surface with the new tool', top)


class ZDatum25DTest(unittest.TestCase):
    """2.5D takes the stock thickness from the CAD layers, which means the Z frame is
    placed a second time, after construction. It has to land on the derived thickness,
    not on the placeholder the post-processor was built with."""

    def _layered_dxf(self, path):
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        for layer, shapes in (('Z_0p000', [('rect', 0, 0, 4, 4)]),
                              ('Z_0p125', [('circle', (2, 2), 0.5)]),
                              ('Z_0p375', [('rect', 0.5, 0.5, 1, 1)])):
            doc.layers.new(name=layer)
            for shape in shapes:
                if shape[0] == 'circle':
                    msp.add_circle(shape[1], shape[2], dxfattribs={'layer': layer})
                else:
                    _, x, y, w, h = shape
                    msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                                       close=True, dxfattribs={'layer': layer})
        doc.saveas(path)
        return path

    def test_layer_depths_move_into_the_program_frame(self):
        """The regression this test exists for: a 2.5D DXF carries its layer heights as
        Z coordinates measured from the sacrifice board, and they used to be written into
        the program verbatim. On the stock-top datum that put every pocket a full stock
        thickness too high - cutting air over the part, while the perimeter still cut
        through - so the two programs have to match move for move here too."""
        dxf = self._layered_dxf(os.path.join(tempfile.mkdtemp(prefix='zdatum_25d_'),
                                             'multi.dxf'))

        def program(datum):
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.157,
                                  config=TeamConfig(), z_datum=datum)
            pp.apply_material_preset('plywood')
            pp.load_dxf(dxf)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            result = pp.generate_gcode()
            self.assertTrue(result.success, msg=str(result.errors))
            return pp, result.gcode

        board_pp, board = program(Z_DATUM_BOARD)
        _, top = program(Z_DATUM_STOCK_TOP)
        thickness = board_pp.material_thickness

        ZDatumProgramTest.assertSameMotion(self, board, top, thickness)
        # And the layer comment must quote the Z the program actually uses, not the
        # number the DXF happened to store.
        self.assertIn('DEPTH: Z=-0.2500"', top)
        self.assertIn('DEPTH: Z=0.1250"', board)

    def test_frame_follows_the_thickness_derived_from_the_layers(self):
        dxf = self._layered_dxf(os.path.join(tempfile.mkdtemp(prefix='zdatum_25d_'),
                                             'multi.dxf'))
        built = {}
        for datum in (Z_DATUM_BOARD, Z_DATUM_STOCK_TOP):
            # Constructed with a placeholder thickness, exactly as the 2.5D route does.
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.157,
                                  config=TeamConfig(), z_datum=datum)
            pp.apply_material_preset('plywood')
            pp.load_dxf(dxf)
            built[datum] = pp

        derived = built[Z_DATUM_BOARD].material_thickness
        self.assertAlmostEqual(derived, 0.375, places=4,
                               msg='the layers should have set the thickness')
        for pp in built.values():
            self.assertAlmostEqual(pp.material_thickness, derived)
        self.assertAlmostEqual(built[Z_DATUM_BOARD].material_top, derived)
        self.assertAlmostEqual(built[Z_DATUM_STOCK_TOP].material_top, 0.0)
        for attr in ('material_top', 'retract_height', 'cut_depth'):
            self.assertAlmostEqual(
                getattr(built[Z_DATUM_BOARD], attr) - getattr(built[Z_DATUM_STOCK_TOP], attr),
                derived, places=4, msg=attr)


class ZDatumMultiToolTest(unittest.TestCase):
    """A multi-tool job re-zeros Z at every manual change, so it has to say which
    surface - and only once for the whole program, since one job has one zero."""

    THICK = 0.25

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='zdatum_mt_')
        self.dxf = _plate_dxf(os.path.join(self.tmp, 'plate.dxf'))

    def _job(self, datum):
        spec = {
            'material': 'plywood', 'thickness': self.THICK, 'z_datum': datum,
            'tools': [
                {'slot': 1, 'name': '1/8 endmill', 'diameter': 0.125, 'flutes': 1,
                 'type': 'endmill'},
                {'slot': 2, 'name': '1/4 endmill', 'diameter': 0.25, 'flutes': 2,
                 'type': 'endmill'},
            ],
            'parts': [{
                'file_index': 0, 'name': 'plate',
                'operations': [
                    {'op_type': 'holes', 'tool_slot': 1},
                    {'op_type': 'pockets', 'tool_slot': 1},
                    {'op_type': 'perimeter', 'tool_slot': 2},
                ],
            }],
        }
        job = tooling.job_from_dict(spec, {0: self.dxf}, config=TeamConfig())
        result = tooling.generate_multitool_job(job)
        self.assertTrue(result.success, msg=str(result.errors))
        return result.gcode

    def test_tool_change_names_the_stock_top(self):
        gcode = self._job('stock_top')
        self.assertIn('Re-zero Z to the top of the stock with the new tool', gcode)
        self.assertNotIn('Re-zero Z to the sacrifice board surface', gcode)

    def test_tool_change_defaults_to_the_board(self):
        self.assertIn('Re-zero Z to the sacrifice board surface with the new tool',
                      self._job(None))

    def test_multi_tool_job_shifts_by_the_thickness(self):
        board = [float(z) for z in Z_WORD.findall(self._job('board'))]
        top = [float(z) for z in Z_WORD.findall(self._job('stock_top'))]
        self.assertEqual(len(board), len(top))
        self.assertTrue(board, 'no Z moves found')
        for b, t in zip(board, top):
            self.assertAlmostEqual(b - t, self.THICK, delta=1.01e-4)

    def test_partial_depth_and_drilled_work_survive_the_datum(self):
        """The second regression this file exists for. "Is this cut through the stock?"
        used to be written as `cut_depth <= 0`, which is only the bottom face on the
        board datum. On the stock-top datum every cut is negative, so a 0.100" pocket in
        0.250" stock read as a through-cut and was contoured instead of cleared - leaving
        its floor uncut - and a blind drilled hole got the break-through allowance meant
        for a hole that exits the far side."""
        dxf = os.path.join(self.tmp, 'depths.dxf')
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (3, 0), (3, 3), (0, 3)], close=True)
        msp.add_lwpolyline([(0.5, 0.5), (2.5, 0.5), (2.5, 2.5), (0.5, 2.5)], close=True)
        for cx, cy in ((0.8, 0.8), (2.2, 2.2)):
            msp.add_circle((cx, cy), 0.1005)      # 0.201" -> a #7 drill
        doc.saveas(dxf)

        def program(datum, blind):
            ops = [{'op_type': 'holes', 'tool_slot': 2, 'name': 'Drill'},
                   {'op_type': 'pockets', 'tool_slot': 1, 'name': 'Relief', 'depth': 0.1},
                   {'op_type': 'perimeter', 'tool_slot': 1}]
            if blind:
                ops[0]['depth'] = 0.15
            spec = {'material': 'aluminum', 'thickness': self.THICK, 'z_datum': datum,
                    'tools': [{'slot': 1, 'name': '1/8 endmill', 'diameter': 0.125,
                               'flutes': 2},
                              {'slot': 2, 'name': '#7 drill', 'diameter': 0.201,
                               'flutes': 2, 'type': 'drill'}],
                    'parts': [{'file_index': 0, 'name': 'p', 'operations': ops}]}
            job = tooling.job_from_dict(spec, {0: dxf}, config=TeamConfig())
            result = tooling.generate_multitool_job(job)
            self.assertTrue(result.success, msg=str(result.errors))
            return result.gcode

        for blind in (False, True):
            ZDatumProgramTest.assertSameMotion(self, program(Z_DATUM_BOARD, blind),
                                               program(Z_DATUM_STOCK_TOP, blind),
                                               self.THICK)

    def test_through_cut_is_measured_from_the_bottom_face(self):
        for datum, bottom in ((Z_DATUM_BOARD, 0.0), (Z_DATUM_STOCK_TOP, -0.25)):
            pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.125,
                                  config=TeamConfig(), z_datum=datum)
            self.assertAlmostEqual(pp.stock_bottom, bottom)
            self.assertTrue(pp.is_through_cut(bottom), 'the bottom face is a through-cut')
            self.assertTrue(pp.is_through_cut(bottom - 0.01), 'past the bottom is through')
            self.assertFalse(pp.is_through_cut(bottom + 0.1),
                             'a partial-depth cut is not a through-cut')

    def test_an_unknown_datum_is_a_bad_request(self):
        with self.assertRaises(tooling.ToolingError):
            tooling.job_from_dict({'material': 'plywood', 'thickness': 0.25,
                                   'z_datum': 'somewhere', 'tools': [], 'parts': []},
                                  {0: self.dxf}, config=TeamConfig())


class ZDatumTubeTest(unittest.TestCase):
    """A tube is zeroed to the tube in its jig. Its Z frame is built by lifting the plate
    toolpath by (tube height - wall thickness), which only works from the board datum, so
    the setting has to be dropped here rather than applied."""

    def test_tube_pattern_ignores_the_stock_top_datum(self):
        tmp = tempfile.mkdtemp(prefix='zdatum_tube_')
        dxf = _plate_dxf(os.path.join(tmp, 'face.dxf'), size=2.0,
                         hole_diameter=0.25, pocket=None)

        def program(datum):
            pp = FRCPostProcessor(material_thickness=0.0625, tool_diameter=0.157,
                                  config=TeamConfig(), z_datum=datum)
            pp.apply_material_preset('aluminum_tube')
            pp.load_dxf(dxf)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            result = pp.generate_tube_pattern_gcode(
                tube_height=1.0, square_end=False, cut_to_length=False,
                tube_width=2.0, tube_length=2.0)
            self.assertTrue(result.success, msg=str(result.errors))
            return pp, result.gcode

        board_pp, board_gcode = program(Z_DATUM_BOARD)
        top_pp, top_gcode = program(Z_DATUM_STOCK_TOP)
        self.assertEqual(top_pp.z_datum, Z_DATUM_BOARD,
                         'the tube path must reset the datum, not honour it')
        self.assertEqual(board_gcode, top_gcode,
                         'a tube program changed when the sheet Z datum changed')


class ZDatumWizardPageTest(unittest.TestCase):
    """The control has to open on the surface the team config actually names. It used to
    open on the board whatever the config said - and then SEND board, so the wizard
    silently overrode the setting it was supposed to be displaying."""

    def _rendered(self, datum):
        import frc_cam_gui_app as gui
        saved = (gui._require_onshape_auth, gui._ensure_local_team_config,
                 gui._maybe_refresh_team_config)
        gui._require_onshape_auth = lambda: None
        gui._ensure_local_team_config = lambda *a, **k: None
        gui._maybe_refresh_team_config = lambda *a, **k: None
        try:
            with gui.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['team_config_data'] = (
                        {'machining': {'z_reference': {'datum': datum}}} if datum else {})
                html = client.get('/app').get_data(as_text=True)
        finally:
            (gui._require_onshape_auth, gui._ensure_local_team_config,
             gui._maybe_refresh_team_config) = saved
        checked = {}
        for value in ('board', 'stock_top'):
            tag = re.search(r'name="z_datum" value="%s"[^>]*>' % value, html, re.S)
            self.assertIsNotNone(tag, f'no {value} radio in the page')
            checked[value] = 'checked' in tag.group(0)
        return checked

    def test_opens_on_the_configured_datum(self):
        self.assertEqual(self._rendered('stock_top'), {'board': False, 'stock_top': True})
        self.assertEqual(self._rendered('sacrifice_board'), {'board': True, 'stock_top': False})

    def test_defaults_to_the_board_with_no_setting(self):
        self.assertEqual(self._rendered(None), {'board': True, 'stock_top': False})


if __name__ == '__main__':
    unittest.main()
