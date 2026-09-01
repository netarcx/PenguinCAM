"""Saved bits: the cutters a shop owns, written into its team config file.

Two things are worth testing here and one of them is not the happy path.

The happy path is easy: a bit goes in, comes back out of the config, and appears in the
library ahead of the built-ins. The part that matters is what happens to the REST of the
file. A team config is a hand-written document - feeds someone measured, machine limits
someone verified, and a page of comments explaining why - and the app now rewrites it.
Every test below that ends in `_preserved` is guarding that document.
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
import tooling
from team_config import TeamConfig, slugify_tool_id

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The shape the config file uses, which is also what the writer accepts.
BITS = [
    {'name': '1/4 in 2-flute endmill', 'diameter': '1/4"', 'flutes': 2,
     'type': 'endmill'},
    {'name': '6mm 1-flute', 'diameter': '6mm', 'flutes': 1, 'type': 'endmill'},
    {'name': '1/2 in 90 deg V-bit', 'diameter': '1/2"', 'flutes': 2,
     'type': 'vbit', 'included_angle': 90},
]


class SavedToolsConfigTest(unittest.TestCase):
    """Reading bits out of a config."""

    def _config(self, tools):
        return TeamConfig({'version': 2, 'default_machine': 'm1',
                           'machines': {'m1': {'name': 'M1'}}, 'tools': tools})

    def test_reads_names_sizes_and_types(self):
        tools = self._config(BITS).saved_tools
        self.assertEqual([t['name'] for t in tools], [b['name'] for b in BITS])
        self.assertAlmostEqual(tools[0]['diameter'], 0.25)
        self.assertAlmostEqual(tools[1]['diameter'], 6 / 25.4)
        self.assertEqual(tools[1]['diameter_text'], '6mm')   # shown as written
        self.assertEqual(tools[2]['type'], 'vbit')
        self.assertAlmostEqual(tools[2]['included_angle'], 90.0)

    def test_a_bad_entry_is_dropped_not_fatal(self):
        """One typo should cost that bit, not the other nine and the app's startup."""
        tools = self._config([
            BITS[0],
            {'name': 'No size'},                       # unusable
            {'diameter': '1/4"'},                      # unnamed
            'not a mapping',
            {'name': 'Odd type', 'diameter': 0.25, 'type': 'laser'},   # kept, as endmill
        ]).saved_tools
        self.assertEqual([t['name'] for t in tools], ['1/4 in 2-flute endmill', 'Odd type'])
        self.assertEqual(tools[1]['type'], 'endmill')

    def test_duplicate_names_keep_the_first(self):
        tools = self._config([BITS[0], dict(BITS[0], flutes=4)]).saved_tools
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['flutes'], 2)

    def test_no_tools_key_is_simply_no_bits(self):
        self.assertEqual(TeamConfig().saved_tools, [])

    def test_library_puts_the_shop_before_the_built_ins(self):
        saved = self._config(BITS).saved_tools
        library = tooling.merge_tool_library(saved)
        self.assertEqual(list(library)[:3], [t['id'] for t in saved])
        self.assertTrue(all(library[t['id']]['source'] == 'team' for t in saved))
        self.assertEqual(library['125_1f']['source'], 'builtin')

    def test_a_saved_bit_replaces_the_built_in_it_shadows(self):
        """If the team has written down what their 1/4 2-flute really is, that wins."""
        mine = {'id': '250_2f', 'name': 'Ours', 'diameter': 0.2495, 'flutes': 2,
                'type': 'endmill', 'diameter_text': '0.2495"'}
        library = tooling.merge_tool_library([mine])
        self.assertEqual(library['250_2f']['name'], 'Ours')
        self.assertEqual(library['250_2f']['source'], 'team')

    def test_default_shelf_has_the_shop_starter_bits(self):
        library = tooling.merge_tool_library([])
        self.assertEqual(library['125_1f']['flutes'], 1)
        self.assertEqual(library['250_1f']['flutes'], 1)
        self.assertEqual(library['156_drill']['type'], 'drill')
        self.assertAlmostEqual(library['156_drill']['diameter'], 5 / 32)
        engraving_angles = sorted(
            tool['included_angle'] for tool in library.values()
            if tool['type'] == 'vbit')
        self.assertTrue({30.0, 60.0, 90.0}.issubset(set(engraving_angles)))


class SavedToolsWriteTest(unittest.TestCase):
    """Writing bits back into a config file without harming it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='bits_')
        self.path = os.path.join(self.dir, 'PenguinCAM-config-2129.yaml')
        shutil.copy(os.path.join(REPO, 'PenguinCAM-config-2129.yaml'), self.path)
        self._saved_env = os.environ.get(local_mode.CONFIG_ENV_VAR)
        os.environ[local_mode.CONFIG_ENV_VAR] = self.path
        self.original = io.open(self.path, encoding='utf-8').read()

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop(local_mode.CONFIG_ENV_VAR, None)
        else:
            os.environ[local_mode.CONFIG_ENV_VAR] = self._saved_env
        shutil.rmtree(self.dir, ignore_errors=True)

    def _text(self):
        return io.open(self.path, encoding='utf-8').read()

    def test_writes_bits_that_read_back(self):
        ok, detail = local_mode.save_tools_to_config(BITS)
        self.assertTrue(ok, msg=detail)
        loaded = TeamConfig(yaml.safe_load(self._text())).saved_tools
        self.assertEqual([t['name'] for t in loaded], [b['name'] for b in BITS])
        self.assertAlmostEqual(loaded[2]['included_angle'], 90.0)

    def test_a_quote_in_the_diameter_survives(self):
        '''1/4" ends in the quote character; writing it raw produced `diameter: "1/4""`,
        which is not YAML - the round-trip check caught it, and this keeps it caught.'''
        ok, detail = local_mode.save_tools_to_config([BITS[0]])
        self.assertTrue(ok, msg=detail)
        self.assertEqual(yaml.safe_load(self._text())['tools'][0]['diameter'], '1/4"')

    def test_the_rest_of_the_config_is_preserved(self):
        local_mode.save_tools_to_config(BITS)
        before = yaml.safe_load(self.original)
        after = yaml.safe_load(self._text())
        self.assertEqual({k: v for k, v in before.items() if k != 'tools'},
                         {k: v for k, v in after.items() if k != 'tools'})

    def test_the_comments_are_preserved(self):
        """The reason this does not go through yaml.safe_dump - and saved TWICE, because
        the first version of the writer took every comment line above its block with it,
        so the second save silently deleted the section's own documentation."""
        local_mode.save_tools_to_config(BITS)
        local_mode.save_tools_to_config(BITS[:1])
        local_mode.save_tools_to_config(BITS)
        after = self._text()
        for comment in [l for l in self.original.splitlines() if l.strip().startswith('#')]:
            self.assertIn(comment, after, f'lost a comment line: {comment.strip()[:60]}')

    def test_saving_twice_does_not_stack_blocks(self):
        local_mode.save_tools_to_config(BITS)
        once = self._text()
        local_mode.save_tools_to_config(BITS)
        self.assertEqual(self._text(), once)
        self.assertEqual(once.count('\ntools:'), 1)

    def test_bits_can_be_removed_again(self):
        local_mode.save_tools_to_config(BITS)
        ok, _ = local_mode.save_tools_to_config([])
        self.assertTrue(ok)
        self.assertEqual(yaml.safe_load(self._text())['tools'], [])
        # And the document is still the document.
        before = yaml.safe_load(self.original)
        after = yaml.safe_load(self._text())
        self.assertEqual({k: v for k, v in before.items() if k != 'tools'},
                         {k: v for k, v in after.items() if k != 'tools'})

    def test_a_config_that_is_not_yaml_is_left_alone(self):
        io.open(self.path, 'w', encoding='utf-8').write('machines: [unclosed\n')
        broken = self._text()
        ok, message = local_mode.save_tools_to_config(BITS)
        self.assertFalse(ok)
        self.assertIn('not valid YAML', message)
        self.assertEqual(self._text(), broken, 'a broken config must not be rewritten')

    def test_reports_when_there_is_nothing_to_write_into(self):
        os.environ[local_mode.CONFIG_ENV_VAR] = os.path.join(self.dir, 'nope.yaml')
        ok, message = local_mode.save_tools_to_config(BITS)
        self.assertFalse(ok)
        self.assertIn('No local team config', message)


class SavedToolsPreserveTest(unittest.TestCase):
    """What a save must NOT destroy.

    The round-trip guard compares parsed keys, so it cannot see any of these: comments,
    entries the app's own reader throws away, or fields it does not understand. Every
    one of them was silently deleted by a save that had nothing to do with it, and every
    one returned HTTP 200 "Saved".
    """

    ORIGINAL = '\n'.join([
        'version: 2',
        'default_machine: m1',
        'machines:',
        '  m1:',
        '    name: "M1"',
        '',
        '# ==================================================================',
        "# The team's own documentation, written ABOVE the block",
        '# ==================================================================',
        local_mode.TOOLS_BLOCK_SENTINEL,
        'tools:',
        '  - name: "The expensive 8mm carbide"',
        '    diamter: "8mm"',
        '  - name: "Shop 6mm"',
        '    diameter: "6mm"',
        '    flutes: 2',
        '    type: endmill',
        '    vendor: "Harvey 993293"',
        '    notes: "chipped corner - regrind"',
        '',
        '# ------------------------------------------------------------------',
        '# Notes the team wrote BELOW the block (notebook p.42)',
        'integrations:',
        '  google_drive:',
        '    enabled: false',
        ''])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='bitskeep_')
        self.path = os.path.join(self.dir, 'PenguinCAM-config.yaml')
        io.open(self.path, 'w', encoding='utf-8').write(self.ORIGINAL)
        self._env = os.environ.get(local_mode.CONFIG_ENV_VAR)
        os.environ[local_mode.CONFIG_ENV_VAR] = self.path

    def tearDown(self):
        if self._env is None:
            os.environ.pop(local_mode.CONFIG_ENV_VAR, None)
        else:
            os.environ[local_mode.CONFIG_ENV_VAR] = self._env
        shutil.rmtree(self.dir, ignore_errors=True)

    def _save_an_unrelated_bit(self):
        tools = local_mode.read_raw_tools()
        tools.append({'name': 'Brand new', 'diameter': '1/8 in', 'flutes': 1,
                      'type': 'endmill'})
        ok, detail = local_mode.save_tools_to_config(tools)
        self.assertTrue(ok, msg=detail)
        return io.open(self.path, encoding='utf-8').read()

    def test_an_entry_the_reader_cannot_parse_is_kept(self):
        """A typo'd key costs that bit its place in the UI - not its place in the file."""
        text = self._save_an_unrelated_bit()
        self.assertIn('The expensive 8mm carbide', text)
        self.assertIn('diamter', text, 'the typo itself must survive for someone to fix')

    def test_fields_the_app_does_not_understand_are_kept(self):
        text = self._save_an_unrelated_bit()
        for field in ('vendor', 'Harvey 993293', 'notes', 'chipped corner'):
            self.assertIn(field, text)

    def test_comments_above_and_below_the_block_are_kept(self):
        text = self._save_an_unrelated_bit()
        self.assertIn("own documentation, written ABOVE", text)
        # BOTH comment lines. Asserting only the second one meant a writer that ate
        # exactly one trailing line - which is what two earlier versions of _block_span
        # did - deleted a team's heading with the test still green.
        self.assertIn('# ------------------------------------------------------------------\n'
                      '# Notes the team wrote BELOW the block (notebook p.42)', text)

    def test_the_block_stays_where_the_team_put_it(self):
        """Appending at the end instead of replacing in place is how the notes below it
        came to be deleted, and it makes a needlessly large diff."""
        text = self._save_an_unrelated_bit()
        self.assertLess(text.index('\ntools:'), text.index('\nintegrations:'))

    def test_everything_else_in_the_config_is_untouched(self):
        text = self._save_an_unrelated_bit()
        before = {k: v for k, v in yaml.safe_load(self.ORIGINAL).items() if k != 'tools'}
        after = {k: v for k, v in yaml.safe_load(text).items() if k != 'tools'}
        self.assertEqual(before, after)

    def test_a_sequence_at_column_zero_is_handled(self):
        """`tools:` followed by dashes in column 0 is ordinary YAML. Removing only the
        `tools:` line orphaned the sequence, and every save failed from then on."""
        io.open(self.path, 'w', encoding='utf-8').write('\n'.join([
            'version: 2', 'default_machine: m1', 'machines:', '  m1:', '    name: "M1"',
            'tools:', '- name: "Old"', '  diameter: "6mm"', '  flutes: 1',
            '  type: endmill', '']))
        ok, detail = local_mode.save_tools_to_config(
            [{'name': 'New', 'diameter': '1/8 in', 'flutes': 2, 'type': 'endmill'}])
        self.assertTrue(ok, msg=detail)
        tools = yaml.safe_load(io.open(self.path, encoding='utf-8').read())['tools']
        self.assertEqual([t['name'] for t in tools], ['New'])

    def test_line_endings_and_permissions_survive(self):
        """A config edited on Windows has CRLF; one chmod'd 600 is 600 on purpose."""
        io.open(self.path, 'w', encoding='utf-8', newline='').write(
            'version: 2\r\ndefault_machine: m1\r\nmachines:\r\n  m1:\r\n    name: "M1"\r\n')
        os.chmod(self.path, 0o600)
        ok, detail = local_mode.save_tools_to_config(
            [{'name': 'A', 'diameter': '6mm', 'flutes': 1, 'type': 'endmill'}])
        self.assertTrue(ok, msg=detail)
        raw = io.open(self.path, 'rb').read()
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), 'mixed line endings')
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_symlinked_config_is_followed_not_replaced(self):
        real = os.path.join(self.dir, 'real.yaml')
        os.rename(self.path, real)
        os.symlink(real, self.path)
        self._save_an_unrelated_bit()
        self.assertTrue(os.path.islink(self.path), 'the symlink was replaced by a file')
        self.assertIn('Brand new', io.open(real, encoding='utf-8').read())


class SavedToolsRouteTest(unittest.TestCase):
    """The save/delete endpoints the star button calls."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='bitsroute_')
        self.path = os.path.join(self.dir, 'PenguinCAM-config-2129.yaml')
        shutil.copy(os.path.join(REPO, 'PenguinCAM-config-2129.yaml'), self.path)
        self._env = {k: os.environ.get(k) for k in (local_mode.CONFIG_ENV_VAR,
                                                    local_mode.LOCAL_ENV_VAR)}
        os.environ[local_mode.CONFIG_ENV_VAR] = self.path
        os.environ[local_mode.LOCAL_ENV_VAR] = '1'
        import frc_cam_gui_app as gui
        self.gui = gui
        self._local_mode = gui.LOCAL_MODE
        gui.LOCAL_MODE = True
        self.client = gui.app.test_client()

    def tearDown(self):
        self.gui.LOCAL_MODE = self._local_mode
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def _save(self, tool):
        response = self.client.post('/tools/save', json={'tool': tool})
        return response.status_code, response.get_json()

    def test_save_then_read_back_in_the_library(self):
        status, body = self._save({'name': '1/4 in 2-flute endmill', 'diameter': '1/4"',
                                   'flutes': 2, 'type': 'endmill'})
        self.assertEqual(status, 200, msg=body)
        entry = body['library'][slugify_tool_id('1/4 in 2-flute endmill')]
        self.assertEqual(entry['source'], 'team')
        self.assertEqual(entry['flutes'], 2)

    def test_saving_the_same_name_corrects_it(self):
        self._save({'name': 'Shop 1/8', 'diameter': '1/8"', 'flutes': 1})
        status, body = self._save({'name': 'Shop 1/8', 'diameter': '1/8"', 'flutes': 3})
        self.assertEqual(status, 200, msg=body)
        team = [k for k, v in body['library'].items() if v['source'] == 'team']
        self.assertEqual(len(team), 1, 'a re-save must correct, not duplicate')
        self.assertEqual(body['library'][team[0]]['flutes'], 3)

    def test_rejects_what_cannot_be_a_bit(self):
        for tool, expected in (
            ({'name': '', 'diameter': '1/4"'}, 'name'),
            ({'name': 'Mystery', 'diameter': 'banana'}, 'diameter'),
            ({'name': 'Tree trunk', 'diameter': '5in'}, 'typo'),
            ({'name': 'Vee', 'diameter': '1/2"', 'type': 'vbit', 'included_angle': 200}, 'V-bit'),
            ({'name': 'Fluty', 'diameter': '1/4"', 'flutes': 99}, 'Flutes'),
            ({'name': 'Laser', 'diameter': '1/4"', 'type': 'laser'}, 'type'),
        ):
            status, body = self._save(tool)
            self.assertEqual(status, 400, msg=f'{tool} was accepted')
            self.assertIn(expected.lower(), body['error'].lower())

    def test_delete_removes_only_the_teams_own(self):
        self._save({'name': 'Shop 1/8', 'diameter': '1/8"', 'flutes': 1})
        response = self.client.post('/tools/delete',
                                    json={'id': slugify_tool_id('Shop 1/8')})
        self.assertEqual(response.status_code, 200)
        self.assertFalse([k for k, v in response.get_json()['library'].items()
                          if v['source'] == 'team'])
        # A built-in is not the team's to delete.
        response = self.client.post('/tools/delete', json={'id': '125_1f'})
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
