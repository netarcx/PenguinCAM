"""Custom text, exact sizing, and uploaded outline-font engraving."""

import base64
import io
import json
import os
import tempfile
import unittest

import ezdxf
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

import outline_font
import job_library
from frc_cam_gui_app import app, limiter


def _glyph(rectangles):
    pen = TTGlyphPen(None)
    for x0, y0, x1, y1 in rectangles:
        pen.moveTo((x0, y0))
        pen.lineTo((x1, y0))
        pen.lineTo((x1, y1))
        pen.lineTo((x0, y1))
        pen.closePath()
    return pen.glyph()


def _test_font_bytes():
    """A tiny deterministic font, so tests do not depend on OS-installed fonts."""
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(['.notdef', 'space', 'H', 'i'])
    builder.setupCharacterMap({32: 'space', 72: 'H', 105: 'i'})
    builder.setupGlyf({
        '.notdef': _glyph([(0, 0, 500, 700)]),
        'space': _glyph([]),
        'H': _glyph([(0, 0, 100, 700), (400, 0, 500, 700),
                     (100, 300, 400, 400)]),
        'i': _glyph([(100, 0, 200, 500), (100, 600, 200, 700)]),
    })
    builder.setupHorizontalMetrics({'.notdef': (600, 0), 'space': (300, 0),
                                    'H': (600, 0), 'i': (300, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'Penguin Test Block', 'styleName': 'Regular',
                            'uniqueFontIdentifier': 'Penguin Test Block',
                            'fullName': 'Penguin Test Block Regular',
                            'psName': 'PenguinTestBlock-Regular'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200,
                     usWinAscent=800, usWinDescent=200, sCapHeight=700)
    builder.setupPost()
    builder.setupMaxp()
    with tempfile.NamedTemporaryFile(suffix='.ttf', delete=False) as handle:
        path = handle.name
    try:
        builder.save(path)
        with open(path, 'rb') as handle:
            return handle.read()
    finally:
        os.remove(path)


def _square_dxf_bytes(size=4.0):
    doc = ezdxf.new('R2010')
    doc.modelspace().add_lwpolyline(
        [(0, 0), (size, 0), (size, size), (0, size)], close=True)
    with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as handle:
        path = handle.name
    try:
        doc.saveas(path)
        with open(path, 'rb') as handle:
            return handle.read()
    finally:
        os.remove(path)


class OutlineFontTest(unittest.TestCase):
    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix='.ttf', delete=False) as handle:
            handle.write(_test_font_bytes())
            self.path = handle.name

    def tearDown(self):
        os.remove(self.path)

    def test_preserves_case_and_exact_cap_height(self):
        strokes, width, family = outline_font.text_strokes('Hi', 0.5, self.path)
        points = [point for stroke in strokes for point in stroke]
        self.assertEqual(family, 'Penguin Test Block')
        self.assertAlmostEqual(max(y for _x, y in points), 0.5, places=6)
        self.assertAlmostEqual(width, 0.5, places=6)
        self.assertEqual(len(strokes), 5)

    def test_missing_glyph_is_an_actionable_error(self):
        with self.assertRaisesRegex(outline_font.OutlineFontError, 'no glyph'):
            outline_font.text_strokes('No', 0.5, self.path)


class UploadedFontRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # These are generation-route tests, not limiter tests. The full suite has other
        # /process-job callers sharing the test client's loopback address.
        limiter.enabled = False

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.dxf = _square_dxf_bytes()
        self.font = _test_font_bytes()

    def _post(self, font_bytes=None, include_font=True):
        job = {
            'material': 'plywood', 'tool_diameter': 0.0625, 'tool_flutes': 1,
            'thickness': 0.25, 'tab_spacing': 6.0,
            'stock': {'width': 4, 'height': 4}, 'name': 'custom-text',
            'engrave': '1', 'engrave_font': 'uploaded',
            'parts': [{
                'file_index': 0, 'name': 'plate', 'place_x': 0, 'place_y': 0,
                'rotation': 0, 'mirror': False, 'engrave_text': 'Hi',
                'engrave_height': 0.5, 'engrave_depth': 0.01,
                'engrave_anchor_x': 2, 'engrave_anchor_y': 2,
            }],
        }
        data = {'job': json.dumps(job), 'timestamp': '2026-09-01 12:00:00',
                'file_0': (io.BytesIO(self.dxf), 'plate.dxf')}
        if include_font:
            data['engrave_font_file'] = (
                io.BytesIO(self.font if font_bytes is None else font_bytes), 'chosen.ttf')
        return self.client.post('/process-job', data=data,
                                content_type='multipart/form-data')

    def test_uploaded_font_generates_outline_toolpaths_at_requested_size(self):
        response = self._post()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        gcode = response.get_json()['gcode']
        self.assertIn('(Text: Hi)', gcode)
        self.assertIn('(Font: Penguin Test Block)', gcode)
        self.assertIn('(Cap height 0.500 in', gcode)
        self.assertGreater(gcode.count('Down to engraving depth'), 3)

    def test_uploaded_mode_requires_a_font(self):
        response = self._post(include_font=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Choose a TTF or OTF', response.get_json()['error'])

    def test_invalid_font_is_rejected_before_toolpath_generation(self):
        response = self._post(font_bytes=b'not a font')
        self.assertEqual(response.status_code, 400)
        self.assertIn('could not be read', response.get_json()['error'])

    def test_uploaded_font_also_reaches_a_multi_tool_program(self):
        job = {
            'material': 'plywood', 'thickness': 0.25, 'tab_spacing': 6.0,
            'name': 'custom-multitool', 'engrave': True,
            'engrave_font': 'uploaded',
            'tools': [{'slot': 1, 'name': 'engraver', 'diameter': 0.0625,
                       'flutes': 1, 'type': 'endmill'}],
            'parts': [{
                'file_index': 0, 'name': 'plate', 'place_x': 0, 'place_y': 0,
                'rotation': 0, 'mirror': False, 'engrave_text': 'Hi',
                'engrave_height': 0.5, 'engrave_depth': 0.01,
                'engrave_anchor_x': 2, 'engrave_anchor_y': 2,
                'operations': [{'op_type': 'perimeter', 'tool_slot': 1}],
            }],
        }
        response = self.client.post('/process-multitool', data={
            'job': json.dumps(job), 'timestamp': '2026-09-01 12:00:00',
            'file_0': (io.BytesIO(self.dxf), 'plate.dxf'),
            'engrave_font_file': (io.BytesIO(self.font), 'chosen.ttf'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        gcode = response.get_json()['gcode']
        self.assertIn('(Text: Hi)', gcode)
        self.assertIn('(Font: Penguin Test Block)', gcode)
        self.assertLess(gcode.index('ENGRAVE PART NAME'),
                        gcode.index('PERIMETER WITH TABS'))


class SavedTextEngravingTest(unittest.TestCase):
    def test_text_size_and_font_round_trip_with_the_part(self):
        with tempfile.TemporaryDirectory() as root:
            config = os.path.join(root, 'PenguinCAM-config.yaml')
            part = {
                'name': 'plaque', 'number': '', 'dxf_bytes': _square_dxf_bytes(),
                'place_x': 0, 'place_y': 0, 'center_x': 2, 'center_y': 2,
                'label_x': 2, 'label_y': 2, 'rotation': 0, 'mirror': False,
                'engrave_text': 'Hi', 'engrave_height': 0.5,
                'engrave_height_text': '1/2"', 'ops': None,
            }
            font = _test_font_bytes()
            job_id, _path = job_library.save_job(
                config, 'Plaque', {'engrave': True, 'engrave_font': 'uploaded'}, [part],
                font={'bytes': font, 'suffix': '.ttf', 'name': 'chosen.ttf'})
            loaded = job_library.load_job(config, job_id)
            self.assertEqual(loaded['parts'][0]['engrave_text'], 'Hi')
            self.assertEqual(loaded['parts'][0]['engrave_height'], 0.5)
            self.assertEqual(loaded['parts'][0]['engrave_height_text'], '1/2"')
            self.assertEqual(loaded['engrave_font_name'], 'chosen.ttf')
            self.assertEqual(font, base64.b64decode(loaded['engrave_font_base64']))


if __name__ == '__main__':
    unittest.main()
