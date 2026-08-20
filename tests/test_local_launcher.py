"""The local launcher: argument handling and port detection.

`penguincam_local.py` is the command a user actually types, and it was the one entry
point with no coverage. The port logic in particular is worth pinning: it silently bound
a port another PenguinCAM was already listening on, sending every request to the old
process while the new one sat there doing nothing with the config it had just been given.
"""

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import penguincam_local


class TestArgumentParsing(unittest.TestCase):
    def test_defaults(self):
        args = penguincam_local.parse_args([])
        self.assertIsNone(args.port)          # None means "pick the next free one"
        self.assertEqual(args.host, '127.0.0.1')
        self.assertFalse(args.no_browser)
        self.assertFalse(args.debug)
        self.assertIsNone(args.config)

    def test_port_is_none_when_unset_so_it_is_distinguishable_from_the_default(self):
        """Comparing against the default VALUE as a sentinel meant an explicit
        `--port 6238` was indistinguishable from no flag and got relocated to 6239."""
        self.assertIsNone(penguincam_local.parse_args([]).port)
        self.assertEqual(penguincam_local.parse_args(['--port', '6238']).port, 6238)

    def test_flags_are_read(self):
        args = penguincam_local.parse_args(
            ['--port', '7000', '--host', '0.0.0.0', '--no-browser', '--debug',
             '--config', 'x.yaml'])
        self.assertEqual(args.port, 7000)
        self.assertEqual(args.host, '0.0.0.0')
        self.assertTrue(args.no_browser)
        self.assertTrue(args.debug)
        self.assertEqual(args.config, 'x.yaml')


class TestPortDetection(unittest.TestCase):
    """Tested by CONNECTING, not by binding. Binding with SO_REUSEADDR - which Werkzeug
    also sets - succeeds on Windows even while another socket is actively listening, so
    the bind test reported 'free' for the exact case it existed to catch."""

    def setUp(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.busy_port = self.listener.getsockname()[1]

    def tearDown(self):
        self.listener.close()

    def test_a_listening_port_is_not_free(self):
        self.assertFalse(penguincam_local.port_is_free('127.0.0.1', self.busy_port))

    def test_a_listening_port_is_not_free_even_with_so_reuseaddr(self):
        """The regression: a previous PenguinCAM left running is the case this exists
        for, and SO_REUSEADDR made the old bind-based check say 'free'."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            bind_said_free = True
            try:
                probe.bind(('127.0.0.1', self.busy_port))
            except OSError:
                bind_said_free = False
        finally:
            probe.close()
        # Whatever bind() thinks, port_is_free must say the port is taken.
        self.assertFalse(penguincam_local.port_is_free('127.0.0.1', self.busy_port),
                         f'bind-based check would have said free={bind_said_free}')

    def test_an_unused_port_is_free(self):
        spare = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        spare.bind(('127.0.0.1', 0))
        port = spare.getsockname()[1]
        spare.close()
        self.assertTrue(penguincam_local.port_is_free('127.0.0.1', port))

    def test_find_free_port_skips_the_busy_one(self):
        found = penguincam_local.find_free_port('127.0.0.1', self.busy_port)
        self.assertNotEqual(found, self.busy_port)
        self.assertGreater(found, self.busy_port)

    def test_find_free_port_gives_up_with_a_usable_message(self):
        with self.assertRaises(SystemExit) as ctx:
            penguincam_local.find_free_port('127.0.0.1', self.busy_port, attempts=0)
        self.assertIn('--port', str(ctx.exception))


class TestConfigFlag(unittest.TestCase):
    def test_a_missing_config_path_is_refused_before_the_app_starts(self):
        """Falling back to defaults on a typo'd path would have the machine running on
        someone else's feeds, so the launcher stops instead."""
        saved = os.environ.get('PENGUINCAM_CONFIG')
        try:
            code = penguincam_local.main(
                ['--config', os.path.join(os.path.dirname(__file__), 'nope.yaml'),
                 '--no-browser'])
            self.assertEqual(code, 1)
        finally:
            if saved is None:
                os.environ.pop('PENGUINCAM_CONFIG', None)
            else:
                os.environ['PENGUINCAM_CONFIG'] = saved


if __name__ == '__main__':
    unittest.main()
