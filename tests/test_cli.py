"""The command line: the entry point with no browser in front of it.

Everything the wizard validates before it posts has to be validated here too, because
`uv run python frc_cam_postprocessor.py` is a supported way to run a job and nothing
stands between it and the machine. These tests drive `main()` with real argv.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import frc_cam_postprocessor


def run_cli(*argv):
    """Run main() with these arguments. Returns (exit code or None, stdout+stderr)."""
    out = io.StringIO()
    old = sys.argv
    sys.argv = ['frc_cam_postprocessor.py', *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                frc_cam_postprocessor.main()
                return None, out.getvalue()
            except SystemExit as exit_code:
                return exit_code.code, out.getvalue()
    finally:
        sys.argv = old


def plate_dxf(path, holes=(), size=(4.0, 3.0)):
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    w, h = size
    msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    for x, y, d in holes:
        msp.add_circle((x, y), d / 2.0)
    doc.saveas(path)
    return path


class TestTubeModesAreInchOnly(unittest.TestCase):
    """Tube modes are inch-only everywhere - load_tube_pattern and load_tube_design
    already refuse mm. The CLI did not: `--mode tube-facing --units mm` built a
    millimetre post-processor and then emitted a hard-coded G20 with inch tube
    geometry. And `--z-zero` was accepted and silently ignored, which hides a real
    operator mistake: a tube job zeroes at the jig, not at a sheet.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_')
        self.out = os.path.join(self.tmp, 'out.nc')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mm_is_refused_for_tube_facing(self):
        code, text = run_cli(self.out, '--mode', 'tube-facing', '--units', 'mm',
                             '--tube-size', '1x1', '--material', 'aluminum',
                             '--thickness', '1.6', '--tool-diameter', '4.0')
        self.assertEqual(code, 2, text[-400:])   # argparse refusal
        self.assertIn('inch', text.lower())

    def test_mm_is_refused_for_tube_pattern(self):
        dxf = plate_dxf(os.path.join(self.tmp, 'face.dxf'))
        code, text = run_cli(dxf, self.out, '--mode', 'tube-pattern', '--units', 'mm',
                             '--material', 'aluminum', '--thickness', '1.6',
                             '--tool-diameter', '4.0')
        self.assertEqual(code, 2, text[-400:])   # argparse refusal
        self.assertIn('inch', text.lower())

    def test_z_zero_is_refused_for_tube_modes(self):
        for datum in ('board', 'stock-top'):
            with self.subTest(datum=datum):
                code, text = run_cli(self.out, '--mode', 'tube-facing',
                                     '--z-zero', datum, '--material', 'aluminum',
                                     '--thickness', '0.0625', '--tool-diameter', '0.157')
                self.assertEqual(code, 2, text[-400:])
                self.assertIn('jig', text.lower())

    def test_inch_tube_facing_still_runs(self):
        code, text = run_cli(self.out, '--mode', 'tube-facing', '--tube-size', '1x1',
                             '--material', 'aluminum', '--thickness', '0.0625',
                             '--tool-diameter', '0.157')
        self.assertIn(code, (None, 0), text[-800:])

    def test_standard_mode_still_takes_mm_and_z_zero(self):
        dxf = plate_dxf(os.path.join(self.tmp, 'p.dxf'), size=(20.0, 15.0))
        code, text = run_cli(dxf, self.out, '--units', 'mm', '--z-zero', 'stock-top',
                             '--material', 'plywood', '--thickness', '6.35',
                             '--tool-diameter', '4.0')
        self.assertIn(code, (None, 0), text[-800:])


if __name__ == '__main__':
    unittest.main()
