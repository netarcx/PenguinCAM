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



class TestMillimetreZFrame(unittest.TestCase):
    """In mm mode the feeds are converted and some preset lengths are, but a handful of
    inch constants in the Z frame were used verbatim. The numbers that came out are
    absurd once you read them as millimetres: 0.5 mm of traverse clearance over the
    stock, a 0.008 mm through-cut overcut (the part stays attached), 0.1 mm of peck
    chip-clearance, tabs every 2 mm.
    """

    def _pp(self, units, thickness):
        with contextlib.redirect_stdout(io.StringIO()):
            pp = frc_cam_postprocessor.FRCPostProcessor(thickness, 4.0 if units == 'mm'
                                                        else 0.157, units=units)
            pp.apply_material_preset('plywood')
        return pp

    def test_clearance_and_overcut_scale(self):
        inch = self._pp('inch', 0.25)
        mm = self._pp('mm', 6.35)
        self.assertAlmostEqual(mm.retract_height - mm.material_top,
                               (inch.retract_height - inch.material_top) * 25.4,
                               places=4)
        self.assertGreaterEqual(mm.retract_height - mm.material_top, 12.0)
        overcut = mm.stock_bottom - mm.cut_depth
        self.assertAlmostEqual(overcut,
                               (inch.stock_bottom - inch.cut_depth) * 25.4, places=4)
        self.assertGreaterEqual(overcut, 0.15)

    def test_peck_return_clearance_scales(self):
        mm = self._pp('mm', 6.35)
        self.assertAlmostEqual(mm.peck_return_clearance, 0.02 * 25.4, places=4)
        self.assertGreaterEqual(mm.peck_return_clearance, 0.5)

    def test_tab_spacing_scales(self):
        inch = self._pp('inch', 0.25)
        mm = self._pp('mm', 6.35)
        self.assertAlmostEqual(mm.tab_spacing, inch.tab_spacing * 25.4, places=4)
        self.assertGreater(mm.tab_spacing, 100.0)

    def test_drill_chip_clearance_planes_scale(self):
        inch = self._pp('inch', 0.25)
        mm = self._pp('mm', 6.35)
        self.assertAlmostEqual(mm.drill_retract_clearance,
                               inch.drill_retract_clearance * 25.4, places=4)
        self.assertGreaterEqual(mm.drill_retract_clearance, 2.0)
        self.assertAlmostEqual(mm.spot_approach_clearance,
                               inch.spot_approach_clearance * 25.4, places=4)
        self.assertGreaterEqual(mm.spot_approach_clearance, 1.0)

    def test_a_mm_program_retracts_and_overcuts_properly(self):
        tmp = tempfile.mkdtemp(prefix='mmz_')
        try:
            dxf = plate_dxf(os.path.join(tmp, 'p.dxf'), holes=[(10.0, 7.0, 5.0)],
                            size=(20.0, 15.0))
            with contextlib.redirect_stdout(io.StringIO()):
                pp = frc_cam_postprocessor.FRCPostProcessor(6.35, 4.0, units='mm')
                pp.apply_material_preset('plywood')
                pp.load_dxf(dxf)
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                result = pp.generate_gcode(timestamp='2026-08-27 12:00')
            self.assertTrue(result.success, result.errors)
            self.assertGreaterEqual(pp.retract_height, pp.material_top + 12.0)
            self.assertGreaterEqual(pp.stock_bottom - pp.cut_depth, 0.15)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_inch_mode_is_untouched(self):
        inch = self._pp('inch', 0.25)
        self.assertAlmostEqual(inch.retract_height - inch.material_top, 0.5)
        self.assertAlmostEqual(inch.stock_bottom - inch.cut_depth, 0.008)
        self.assertAlmostEqual(inch.peck_return_clearance, 0.02)



class TestClassifyOrder(unittest.TestCase):
    """A round part's OUTER boundary is a circle. identify_perimeter_and_pockets is what
    removes that circle from self.circles once it has been claimed as the perimeter, so
    it has to run FIRST - the route says so in a comment and does it in that order. Both
    CLI branches ran classify_holes first, so the outline was also machined as a giant
    hole: cleared out with a spiral from the centre.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='order_')
        self.out = os.path.join(self.tmp, 'out.nc')
        self.dxf = os.path.join(self.tmp, 'round.dxf')
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        msp.add_circle((3.0, 3.0), 2.5)          # the part outline
        msp.add_circle((3.0, 3.0), 0.25)         # a bore in the middle of it
        doc.saveas(self.dxf)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _emitted(self):
        code, text = run_cli(self.dxf, self.out, '--material', 'plywood',
                             '--thickness', '0.25', '--tool-diameter', '0.157')
        self.assertIn(code, (None, 0), text[-800:])
        produced = [os.path.join(self.tmp, f) for f in os.listdir(self.tmp)
                    if f.endswith('.nc')]
        self.assertTrue(produced, text[-800:])
        with open(produced[0]) as handle:
            return handle.read(), text

    def test_the_outline_is_the_perimeter_not_a_hole(self):
        gcode, printed = self._emitted()
        self.assertIn('PERIMETER', gcode)
        # One hole only - the 0.25" bore. The 5" outline must not be one of them.
        self.assertIn('Identified 1 millable holes', printed)

    def test_nothing_is_cleared_at_the_outline_diameter(self):
        gcode, _ = self._emitted()
        for line in gcode.splitlines():
            if line.startswith('(Hole '):
                self.assertNotIn('5.000"', line,
                                 'the outline was machined as a giant hole')

    def test_the_tube_pattern_branch_orders_the_same_way(self):
        code, text = run_cli(self.dxf, self.out, '--mode', 'tube-pattern',
                             '--tube-height', '1.0', '--material', 'aluminum',
                             '--thickness', '0.0625', '--tool-diameter', '0.157')
        self.assertIn(code, (None, 0), text[-800:])
        self.assertIn('Classified 1 holes', text)



class TestExplicitFeedFlagsSurviveTheLoader(unittest.TestCase):
    """--feed-rate and --plunge-rate are applied before the pattern loader runs, and the
    drilled hole pattern's own feed model then overwrote them. The comment above them
    says explicit flags come last; they did not, so a mentor pinning a known-good number
    for an unusual drill was silently ignored.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='feeds_')
        self.out = os.path.join(self.tmp, 'out.nc')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _program(self, *extra):
        code, text = run_cli(self.out, '--mode', 'tube-pattern',
                             '--tube-pattern', 'holes', '--tube-size', '1x1',
                             '--tube-length', '12', '--material', 'aluminum',
                             '--thickness', '0.0625', '--tool-diameter', '0.201',
                             *extra)
        produced = [os.path.join(self.tmp, f) for f in os.listdir(self.tmp)
                    if f.endswith('.nc')]
        if not produced:
            return code, text, None
        with open(produced[0]) as handle:
            return code, text, handle.read()

    def test_an_explicit_plunge_rate_reaches_the_moves(self):
        code, text, gcode = self._program('--plunge-rate', '9')
        self.assertIn(code, (None, 0), text[-800:])
        self.assertIsNotNone(gcode, text[-800:])
        self.assertIn('F9.0', gcode,
                      'the drilling model overwrote the explicit plunge rate')

    def test_an_explicit_feed_rate_reaches_the_moves(self):
        code, text, gcode = self._program('--feed-rate', '11', '--plunge-rate', '9')
        self.assertIn(code, (None, 0), text[-800:])
        self.assertIn('F9.0', gcode)

    def test_a_flag_over_the_aluminum_ceiling_is_still_refused(self):
        """"Explicit" does not mean "unbounded" - 40 IPM of plunge in 6061 breaks the
        drill whoever typed it."""
        code, text, _ = self._program('--plunge-rate', '40')
        self.assertEqual(code, 2, text[-400:])
        self.assertIn('ceiling', text.lower())

    def test_without_the_flags_the_drill_model_still_applies(self):
        code, text, gcode = self._program()
        self.assertIn(code, (None, 0), text[-800:])
        self.assertIn('Peck 1 of', gcode)
        self.assertNotIn('F9.0', gcode)



class TestShippedExampleJob(unittest.TestCase):
    """The example is what someone reads to learn the format, so every operation in it
    has to do something. It pointed at sample_part.dxf, which has no holes and no
    pockets, so three of its five operations emitted nothing at all."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='example_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_operation_emits_toolpath(self):
        out = os.path.join(self.tmp, 'out.nc')
        code, text = run_cli('--ops-file',
                             os.path.join(self.ROOT, 'examples', 'multitool_job.json'),
                             out)
        self.assertIn(code, (None, 0), text[-800:])
        produced = [os.path.join(self.tmp, f) for f in os.listdir(self.tmp)
                    if f.endswith('.nc')]
        self.assertTrue(produced, text[-800:])
        with open(produced[0]) as handle:
            gcode = handle.read()
        for banner in ('SMALL HOLES', 'LARGE BORES', 'LIGHTENING POCKETS',
                       'PROFILE', 'EDGE BREAK'):
            with self.subTest(banner=banner):
                self.assertIn(banner, gcode)

    def test_the_example_plate_can_be_regenerated(self):
        """The DXF is script-generated so its shapes stay readable and adjustable."""
        import importlib.util

        script = os.path.join(self.ROOT, 'examples', 'make_example_plate.py')
        spec = importlib.util.spec_from_file_location('make_example_plate', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rebuilt = module.build(os.path.join(self.tmp, 'plate.dxf'))

        doc = ezdxf.readfile(rebuilt)
        msp = doc.modelspace()
        circles = sorted(round(e.dxf.radius * 2, 4)
                         for e in msp.query('CIRCLE'))
        self.assertEqual(circles, [0.201, 0.201, 0.201, 0.201, 0.875])
        self.assertEqual(len(list(msp.query('LWPOLYLINE'))), 2)   # outline + pocket


if __name__ == '__main__':
    unittest.main()
