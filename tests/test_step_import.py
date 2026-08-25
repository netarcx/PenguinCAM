"""End-to-end tests for local STEP -> 2.5D depth-layer conversion."""

import base64
import io
import os
import tempfile
import unittest

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from frc_cam_postprocessor import FRCPostProcessor
from step_geometry import StepGeometryError, convert_step_to_multilayer_dxf


MM = 25.4


def _counterbored_plate(counterbore_from_top=True):
    plate = BRepPrimAPI_MakeBox(4 * MM, 2 * MM, 0.25 * MM).Shape()
    axis = gp_Dir(0, 0, 1)
    small = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(1 * MM, 1 * MM, 0), axis), 0.125 * MM, 0.25 * MM).Shape()
    large_z = 0.125 * MM if counterbore_from_top else 0
    large = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(1 * MM, 1 * MM, large_z), axis),
        0.25 * MM, 0.125 * MM).Shape()
    return BRepAlgoAPI_Cut(BRepAlgoAPI_Cut(plate, small).Shape(), large).Shape()


def _side_drilled_plate():
    plate = BRepPrimAPI_MakeBox(4 * MM, 2 * MM, 0.25 * MM).Shape()
    side_hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 1 * MM, 0.125 * MM), gp_Dir(1, 0, 0)),
        0.0625 * MM, 4 * MM).Shape()
    return BRepAlgoAPI_Cut(plate, side_hole).Shape()


def _step_bytes(shape):
    with tempfile.NamedTemporaryFile(suffix='.step', delete=False) as tmp:
        path = tmp.name
    try:
        writer = STEPControl_Writer()
        if writer.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone:
            raise AssertionError('Could not transfer test shape to STEP')
        if writer.Write(path) != IFSelect_RetDone:
            raise AssertionError('Could not write test STEP')
        with open(path, 'rb') as stream:
            return stream.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


class TestStepImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.top_step = _step_bytes(_counterbored_plate(True))
        cls.bottom_step = _step_bytes(_counterbored_plate(False))
        cls.side_hole_step = _step_bytes(_side_drilled_plate())

    def _convert(self, content):
        with tempfile.NamedTemporaryFile(suffix='.step', delete=False) as tmp:
            tmp.write(content)
            path = tmp.name
        try:
            return convert_step_to_multilayer_dxf(path)
        finally:
            os.remove(path)

    def test_counterbore_becomes_three_depth_layers(self):
        converted = self._convert(self.top_step)
        self.assertAlmostEqual(converted.thickness, 0.25, places=4)
        self.assertEqual([round(d, 3) for d in converted.layer_depths],
                         [0.0, 0.125, 0.25])
        self.assertGreater(converted.machining_normal[2], 0.99)

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            tmp.write(converted.dxf_bytes)
            dxf_path = tmp.name
        try:
            pp = FRCPostProcessor(material_thickness=1.0, tool_diameter=0.125)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(dxf_path)
            self.assertAlmostEqual(pp.material_thickness, 0.25, places=4)
            middle = pp.layer_data['Z_0p125']['polygons']
            self.assertEqual(len(middle), 1)
            self.assertEqual(len(middle[0].interiors), 1,
                             'counterbore floor should be an annulus around the through-hole')
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()
            self.assertTrue(result.success, result.errors)
            self.assertIn('LAYER: Z_0p125', result.gcode)
            self.assertIn('Layer Z_0p125: 1 pockets', result.gcode)
        finally:
            os.remove(dxf_path)

    def test_machining_side_is_detected_from_feature_direction(self):
        converted = self._convert(self.bottom_step)
        self.assertLess(converted.machining_normal[2], -0.99)
        self.assertEqual([round(d, 3) for d in converted.layer_depths],
                         [0.0, 0.125, 0.25])

    def test_side_hole_is_refused_instead_of_silently_ignored(self):
        with self.assertRaisesRegex(StepGeometryError, 'side-facing hole'):
            self._convert(self.side_hole_step)

    def test_part_outline_returns_browser_held_multilayer_dxf(self):
        from frc_cam_gui_app import app

        app.config['TESTING'] = True
        response = app.test_client().post(
            '/part-outline',
            data={'file': (io.BytesIO(self.top_step), 'counterbore.step')},
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(body['multilayer'])
        self.assertEqual(body['source_format'], 'step')
        self.assertAlmostEqual(body['thickness'], 0.25, places=4)
        self.assertGreater(len(base64.b64decode(body['dxf'])), 1000)

    def test_bad_step_is_a_user_error_not_a_server_error(self):
        from frc_cam_gui_app import app

        response = app.test_client().post(
            '/part-outline',
            data={'file': (io.BytesIO(b'not a STEP model'), 'bad.step')},
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Could not read', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
