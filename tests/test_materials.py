"""Material identity: spelling an alloy differently must not disarm the metal rules.

Feeds, spindle ceilings, the preflight pause and the chipload guard all hang off one
question - "is this aluminum?" - answered by string matching on an id that arrives from
YAML, the web form, a saved job, or a typed CLI flag. When the answer came back wrong the
program still generated, just with plywood numbers and none of the aluminum protections.
These tests pin the answer for every spelling that reaches it, and pin the refusal for
the spellings nothing recognises.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

import feeds_speeds
import tooling
from tooling import MultiToolJob, Operation, PartOps, Tool, ToolingError
from frc_cam_postprocessor import FRCPostProcessor
from team_config import TeamConfig


def square_dxf(size=4.0, hole=True):
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (size, 0), (size, size), (0, size)], close=True)
    if hole:
        msp.add_circle((size / 2, size / 2), 0.125)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


class TestCanonicalMaterialKey(unittest.TestCase):
    """Every spelling of the alloy an operator might type resolves to aluminum."""

    ALUMINUM = ['aluminum', 'aluminium', 'Aluminum', 'aluminum_tube', 'al', 'alu',
                'al6061', 'alu6061', 'al-6061', '6061', '6061-T6', 'AL 7075',
                '7075', 'al7075', 'aluminum 6063', '6063']
    NOT_ALUMINUM = ['alder', 'plywood', 'polycarbonate', 'hdpe', 'srpp']

    def test_aluminum_spellings_resolve(self):
        for spelling in self.ALUMINUM:
            with self.subTest(spelling=spelling):
                self.assertTrue(
                    feeds_speeds.is_aluminum_material(spelling),
                    f"{spelling!r} is aluminum but the feeds model did not think so")

    def test_alder_is_not_aluminum(self):
        """Token matching, not `'al' in id`: alder is a wood."""
        for spelling in self.NOT_ALUMINUM:
            with self.subTest(spelling=spelling):
                self.assertFalse(feeds_speeds.is_aluminum_material(spelling))

    def test_6061_keeps_its_own_grade(self):
        self.assertEqual(feeds_speeds.canonical_material_key('al6061'), 'aluminum_6061')

    def test_7075_takes_the_conservative_preset(self):
        """No 7075 entry exists; the less machinable 6063 numbers are the safe stand-in."""
        self.assertEqual(feeds_speeds.canonical_material_key('al7075'), 'aluminum_6063')

    def test_unknown_material_stays_unknown(self):
        for spelling in ('unobtainium', 'aluminun', 'brass', ''):
            with self.subTest(spelling=spelling):
                self.assertIsNone(feeds_speeds.canonical_material_key(spelling))


class TestAluminumProtectionsApply(unittest.TestCase):
    """`--material al6061` used to fabricate a plywood preset relabelled "Al6061":
    75 IPM, 18000 RPM, no envelope clamp, no chipload guard, no preflight pause."""

    def _program(self, material):
        path = square_dxf()
        try:
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(0.25, 0.157)
                pp.apply_material_preset(material)
                pp.load_dxf(path)
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                result = pp.generate_gcode(suggested_filename='p',
                                           timestamp='2026-08-27 12:00')
            return pp, result.gcode
        finally:
            os.remove(path)

    def test_al6061_gets_the_preflight_pause(self):
        pp, gcode = self._program('al6061')
        self.assertIn('REQUIRED ALUMINUM PREFLIGHT', gcode)
        self.assertIn('M0', gcode)

    def test_al6061_respects_the_router_envelope(self):
        pp, gcode = self._program('al6061')
        envelope = feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX
        self.assertLessEqual(pp.feed_rate, envelope['feed_rate'] + 1e-9)
        self.assertLessEqual(pp.plunge_rate, envelope['plunge_rate'] + 1e-9)
        machine = feeds_speeds.MACHINES['omio_x8']
        self.assertLessEqual(pp.spindle_speed, machine['rpm_max'])

    def test_al6061_is_not_labelled_plywood_feeds(self):
        pp, gcode = self._program('al6061')
        plywood = FRCPostProcessor(0.25, 0.157)
        with redirect_stdout(io.StringIO()):
            plywood.apply_material_preset('plywood')
        self.assertLess(pp.feed_rate, plywood.feed_rate,
                        "aluminum is running at or above the plywood feed")

    def test_unknown_material_is_refused(self):
        """A wrong feed table in metal is a broken bit. Refuse, do not guess."""
        pp = FRCPostProcessor(0.25, 0.157)
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError) as caught:
                pp.apply_material_preset('unobtainium')
        message = str(caught.exception)
        self.assertIn('unobtainium', message)
        self.assertIn('plywood', message)      # the message lists what IS known
        self.assertIn('aluminum', message)

    def test_team_configured_material_still_works(self):
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'M', 'materials': {'brass': {'name': 'Brass', 'feed_rate': 18.0}}}}})
        pp = FRCPostProcessor(0.25, 0.157, config=cfg)
        with redirect_stdout(io.StringIO()):
            pp.apply_material_preset('brass')
        self.assertAlmostEqual(pp.feed_rate, 18.0)


class TestMultiToolMaterialResolution(unittest.TestCase):
    """A material id the feeds model has never heard of must not be quietly re-quoted
    with the plywood chipload model - that overwrites the team's tested numbers."""

    def _job(self, material, config=None):
        path = square_dxf(6.0, hole=False)
        return MultiToolJob(
            tools=[Tool(1, '1/8 in 1-flute endmill', 0.125, 1)],
            parts=[PartOps(path, 'plate',
                           operations=[Operation('perimeter', 1)])],
            material=material, thickness=0.25, config=config)

    def test_team_preset_feed_survives(self):
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'M', 'materials': {'brass': {
                'name': 'Brass', 'feed_rate': 18.0, 'plunge_rate': 6.0,
                'ramp_feed_rate': 12.0, 'spindle_speed': 9000}}}}})
        job = self._job('brass', config=cfg)
        with redirect_stdout(io.StringIO()):
            result = tooling.generate_multitool_job(job, timestamp='2026-08-27 12:00')
        self.assertTrue(result.success, '; '.join(result.errors))
        self.assertIn('F18', result.gcode.replace('F18.0', 'F18'))
        self.assertTrue(any('brass' in w.lower() for w in result.warnings),
                        f"no warning named the unmodelled material: {result.warnings}")

    def test_unknown_material_without_a_preset_refuses(self):
        job = self._job('unobtainium')
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(ToolingError) as caught:
                tooling.resolve_feeds_material('unobtainium', job.config)
        self.assertIn('unobtainium', str(caught.exception))

    def test_unknown_material_job_reports_the_refusal(self):
        job = self._job('unobtainium')
        with redirect_stdout(io.StringIO()):
            result = tooling.generate_multitool_job(job, timestamp='2026-08-27 12:00')
        self.assertFalse(result.success)
        self.assertTrue(any('unobtainium' in e for e in result.errors),
                        result.errors)


class TestDrillingMaterialFallback(unittest.TestCase):
    """`DRILLING.get(key) or DRILLING['plywood']` ran a twist drill in an unknown metal
    at plywood surface speed. It has to refuse instead."""

    def test_unknown_drilling_material_refuses(self):
        with self.assertRaises(ValueError) as caught:
            feeds_speeds.calculate_drill_feeds('omio_x8', 'unobtainium',
                                               {'diameter': 0.201})
        self.assertIn('unobtainium', str(caught.exception))

    def test_known_drilling_material_works(self):
        drill = feeds_speeds.calculate_drill_feeds('omio_x8', 'aluminum_6061',
                                                   {'diameter': 0.201})
        self.assertGreater(drill['rpm'], 0)



class TestPresetResolution(unittest.TestCase):
    """A dict spec may name a base preset to start from. An unknown name resolved to an
    empty base, so the caller's few overrides became the WHOLE spec and the first field
    the model reached for was missing - surfacing in the calculator API as
    `KeyError: 'preferred_rpm'`, a 500 with nothing in it for the user.
    """

    def test_an_unknown_machine_preset_is_named(self):
        with self.assertRaises(ValueError) as caught:
            feeds_speeds.calculate_feeds({'preset': 'omio_x9', 'rpm_max': 20000},
                                         'plywood', {'diameter': 0.157, 'flutes': 1})
        message = str(caught.exception)
        self.assertIn('omio_x9', message)
        self.assertIn('omio_x8', message)      # and says what there is

    def test_an_unknown_material_preset_is_named(self):
        with self.assertRaises(ValueError) as caught:
            feeds_speeds.calculate_feeds('omio_x8',
                                         {'preset': 'plywoood', 'chipload_min': 0.002},
                                         {'diameter': 0.157, 'flutes': 1})
        message = str(caught.exception)
        self.assertIn('plywoood', message)
        self.assertIn('plywood', message)

    def test_a_known_preset_still_overlays(self):
        feeds = feeds_speeds.calculate_feeds(
            {'preset': 'omio_x8', 'feed_max': 90.0}, 'plywood',
            {'diameter': 0.157, 'flutes': 1})
        self.assertLessEqual(feeds['feed_xy'], 90.0)

    def test_a_dict_with_no_preset_needs_no_base(self):
        """A fully-specified custom spec is passed through as it stands."""
        resolved = feeds_speeds._resolve({'rpm_min': 8000, 'rpm_max': 24000},
                                        feeds_speeds.MACHINES, 'machine')
        self.assertEqual(resolved, {'rpm_min': 8000, 'rpm_max': 24000})


if __name__ == '__main__':
    unittest.main()
