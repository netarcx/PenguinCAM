"""The stock library: the sheets and offcuts a shop actually has.

Stock used to be implicit - whatever bounding box the placed parts happened to make -
which answers "what am I cutting?" with "whatever you drew". A named sheet changes two
things that matter downstream: the G54 origin becomes the SHEET's corner (so a part
keeps its place on the material between jobs), and there is something to check a
layout against and something to nest into.

The offcut half is the point of the feature: an offcut nobody wrote down is an offcut
nobody uses, and it goes in the bin while someone opens a fresh sheet.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

import local_mode
from team_config import TeamConfig

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StockConfigTest(unittest.TestCase):
    def _config(self, stock):
        return TeamConfig({'version': 2, 'default_machine': 'm1',
                           'machines': {'m1': {'name': 'M1'}}, 'stock': stock})

    def test_reads_sheets_and_offcuts(self):
        sheets = self._config([
            {'name': 'Half sheet', 'width': '48"', 'height': '48"',
             'thickness': '0.25"', 'material': 'plywood'},
            {'name': 'Al offcut', 'width': '11.5"', 'height': '6"', 'remnant': True},
        ]).saved_stock
        self.assertEqual([s['name'] for s in sheets], ['Half sheet', 'Al offcut'])
        self.assertAlmostEqual(sheets[0]['width'], 48.0)
        self.assertAlmostEqual(sheets[0]['thickness'], 0.25)
        self.assertEqual(sheets[0]['material'], 'plywood')
        self.assertFalse(sheets[0]['remnant'])
        self.assertTrue(sheets[1]['remnant'])

    def test_units_are_honoured_and_shown_as_written(self):
        sheet = self._config([{'name': 'Metric', 'width': '600mm', 'height': '300mm'}]).saved_stock[0]
        self.assertAlmostEqual(sheet['width'], 600 / 25.4)
        self.assertEqual(sheet['width_text'], '600mm')

    def test_a_bad_entry_is_dropped_not_fatal(self):
        sheets = self._config([
            {'name': 'Good', 'width': 24, 'height': 24},
            {'name': 'No size'},
            {'width': 24, 'height': 24},
            'not a mapping',
        ]).saved_stock
        self.assertEqual([s['name'] for s in sheets], ['Good'])

    def test_a_v1_config_keeps_its_stock_and_tools(self):
        """Root-level lists belong to the shop, not to the one machine a v1 config
        describes. Folding them into the machine block lost them entirely."""
        config = TeamConfig({
            'machine': {'dimensions': {'x_max': 24, 'y_max': 24}},
            'stock': [{'name': 'Sheet', 'width': 24, 'height': 24}],
            'tools': [{'name': 'Shop 1/4', 'diameter': '1/4"', 'flutes': 2}],
        })
        self.assertEqual([s['name'] for s in config.saved_stock], ['Sheet'])
        self.assertEqual([t['name'] for t in config.saved_tools], ['Shop 1/4'])


class StockRouteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='stock_')
        self.path = os.path.join(self.dir, 'PenguinCAM-config-2129.yaml')
        shutil.copy(os.path.join(REPO, 'PenguinCAM-config-2129.yaml'), self.path)
        self._env = {k: os.environ.get(k) for k in (local_mode.CONFIG_ENV_VAR,
                                                    local_mode.LOCAL_ENV_VAR)}
        os.environ[local_mode.CONFIG_ENV_VAR] = self.path
        os.environ[local_mode.LOCAL_ENV_VAR] = '1'
        import frc_cam_gui_app as gui
        self.gui = gui
        self._local = gui.LOCAL_MODE
        gui.LOCAL_MODE = True
        self.client = gui.app.test_client()

    def tearDown(self):
        self.gui.LOCAL_MODE = self._local
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def _save(self, stock):
        response = self.client.post('/stock/save', json={'stock': stock})
        return response.status_code, response.get_json()

    def test_save_read_back_and_delete(self):
        status, body = self._save({'name': 'Half sheet', 'width': '48"', 'height': '48"',
                                   'thickness': '0.25"', 'material': 'plywood'})
        self.assertEqual(status, 200, msg=body)
        self.assertEqual([s['name'] for s in body['stock']], ['Half sheet'])

        status, body = self._save({'name': 'Al offcut', 'width': 11.5, 'height': 6,
                                   'remnant': True})
        self.assertEqual(status, 200, msg=body)
        self.assertTrue([s for s in body['stock'] if s['name'] == 'Al offcut'][0]['remnant'])

        response = self.client.post('/stock/delete', json={'id': 'half_sheet'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s['name'] for s in response.get_json()['stock']], ['Al offcut'])

    def test_saving_the_same_name_corrects_it(self):
        self._save({'name': 'Sheet', 'width': 24, 'height': 24})
        status, body = self._save({'name': 'Sheet', 'width': 30, 'height': 24})
        self.assertEqual(status, 200, msg=body)
        sheets = [s for s in body['stock'] if s['name'] == 'Sheet']
        self.assertEqual(len(sheets), 1, 'a re-save must correct, not duplicate')
        self.assertAlmostEqual(sheets[0]['width'], 30.0)

    def test_rejects_what_cannot_be_stock(self):
        for stock, expected in (
            ({'name': '', 'width': 24, 'height': 24}, 'name'),
            ({'name': 'Junk', 'width': 'banana', 'height': 24}, 'width'),
            ({'name': 'Enormous', 'width': 500, 'height': 24}, 'larger'),
            ({'name': 'Thin', 'width': 24, 'height': 24, 'thickness': 'x'}, 'thickness'),
            ({'name': 'Break\nit', 'width': 24, 'height': 24}, 'control characters'),
        ):
            status, body = self._save(stock)
            self.assertEqual(status, 400, msg=f'{stock} was accepted')
            self.assertIn(expected.lower(), body['error'].lower())

    def test_stock_and_bits_coexist_in_one_config(self):
        """Two managed blocks, one file, and the rest of it untouched."""
        original = io.open(self.path, encoding='utf-8').read()
        self._save({'name': 'Sheet', 'width': 24, 'height': 24})
        self.client.post('/tools/save',
                         json={'tool': {'name': 'Shop 1/4', 'diameter': '1/4"', 'flutes': 2}})
        text = io.open(self.path, encoding='utf-8').read()
        self.assertIn('\nstock:', text)
        self.assertIn('\ntools:', text)
        before = {k: v for k, v in yaml.safe_load(original).items()
                  if k not in ('stock', 'tools')}
        after = {k: v for k, v in yaml.safe_load(text).items()
                 if k not in ('stock', 'tools')}
        self.assertEqual(before, after)
        for comment in [l for l in original.splitlines() if l.strip().startswith('#')]:
            self.assertIn(comment, text, 'a save lost a comment')


if __name__ == '__main__':
    unittest.main()
