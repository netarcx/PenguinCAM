"""The custom tube designer: expansion, named sizes, and everything it refuses.

`tube_designer.resolve` is the only thing standing between a design clicked together in
a browser and a program that machines it. Every validation it performs is here twice:
once with geometry that must be refused, and once with a near miss that must be
accepted - a check that refuses everything passes a test written only the first way.

The refusals are not hypothetical. Each row of the table in docs/TUBE_DESIGNER_PLAN.md
is a bug this project has already shipped on the fixed-pattern path: NaN reaching the
G-code as `Xnan`, a 0.201" hole placed on a 0.15" face, a pocket that passed its size
check and then vanished when offset inward, leaving a program that reported success and
cut nothing.
"""

import json
import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely.geometry import Polygon

import drill_sizes
import tube_designer
import tube_patterns

TOOL = 0.157
FACE = 2.0
LENGTH = 24.0


def resolve(features, face_width=FACE, tube_length=LENGTH, tool=TOOL, **kwargs):
    return tube_designer.resolve({'version': 1, 'features': features},
                                 face_width, tube_length, tool, **kwargs)


def errors_for(features, **kwargs):
    return resolve(features, **kwargs)['errors']


class TestNamedSizes(unittest.TestCase):
    """The size menu is the app's existing drill table, not a copy of it."""

    def test_every_clearance_size_comes_from_tap_drills(self):
        menu = {item['id']: item['diameter'] for item in tube_designer.clearance_sizes()}
        self.assertEqual(set(menu), set(drill_sizes.TAP_DRILLS))
        for name, spec in drill_sizes.TAP_DRILLS.items():
            self.assertAlmostEqual(menu[name], spec['clearance'], places=4)

    def test_the_three_sizes_this_was_asked_for(self):
        self.assertAlmostEqual(tube_designer.named_diameter('1/4-20'), 0.2656, places=4)
        self.assertAlmostEqual(tube_designer.named_diameter('10-32'), 0.1935, places=4)
        self.assertAlmostEqual(tube_designer.named_diameter('8-32'), 0.1695, places=4)

    def test_the_bearing_bore_is_the_frc_flanged_hex_bearing(self):
        self.assertAlmostEqual(tube_designer.BEARING_BORES['flanged-hex-bearing'], 1.125)

    def test_the_menu_offers_clearances_and_bores_together(self):
        ids = [item['id'] for item in tube_designer.size_menu()]
        self.assertIn('10-32', ids)
        self.assertIn('flanged-hex-bearing', ids)

    def test_an_unknown_name_answers_none_rather_than_guessing(self):
        self.assertIsNone(tube_designer.named_diameter('10-32ish'))

    def test_a_named_size_is_resolved_server_side(self):
        """A stale client cannot ship a wrong number: it can only ship a name."""
        circles = resolve([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '10-32',
                            'diameter': 0.75}])['circles']
        self.assertEqual(len(circles), 1)
        self.assertAlmostEqual(circles[0]['diameter'], 0.1935, places=4)

    def test_a_custom_diameter_is_used_when_no_name_is_given(self):
        circles = resolve([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'diameter': 0.196}])['circles']
        self.assertAlmostEqual(circles[0]['diameter'], 0.196, places=6)

    def test_a_hole_with_neither_name_nor_diameter_is_refused(self):
        self.assertTrue(any('named size' in e
                            for e in errors_for([{'type': 'hole', 'x': 1.0, 'y': 6.0}])))


class TestExpansion(unittest.TestCase):
    """Runs and arrays expand to plain holes; nothing downstream ever sees them."""

    def test_a_hole_run_makes_count_holes_on_pitch(self):
        r = resolve([{'type': 'hole-run', 'x': 1.0, 'y': 2.0, 'pitch': 0.5,
                      'count': 8, 'axis': 'y', 'size': '10-32'}])
        self.assertEqual(r['errors'], [])
        ys = sorted(c['center'][1] for c in r['circles'])
        self.assertEqual(len(ys), 8)
        self.assertEqual({round(b - a, 6) for a, b in zip(ys, ys[1:])}, {0.5})
        self.assertEqual({c['center'][0] for c in r['circles']}, {1.0})

    def test_a_hole_run_can_march_across_the_face(self):
        r = resolve([{'type': 'hole-run', 'x': 0.5, 'y': 6.0, 'pitch': 0.5,
                      'count': 3, 'axis': 'x', 'size': '8-32'}])
        self.assertEqual(r['errors'], [])
        self.assertEqual(sorted(c['center'][0] for c in r['circles']), [0.5, 1.0, 1.5])
        self.assertEqual({c['center'][1] for c in r['circles']}, {6.0})

    def test_an_array_makes_rows_times_columns(self):
        r = resolve([{'type': 'hole-array', 'x': 0.5, 'y': 4.0, 'pitch_x': 1.0,
                      'pitch_y': 0.5, 'cols': 2, 'rows': 4, 'size': '10-32'}])
        self.assertEqual(r['errors'], [])
        self.assertEqual(len(r['circles']), 8)
        self.assertEqual(sorted({c['center'][0] for c in r['circles']}), [0.5, 1.5])
        self.assertEqual(sorted({c['center'][1] for c in r['circles']}),
                         [4.0, 4.5, 5.0, 5.5])

    def test_a_single_column_array_needs_no_x_pitch(self):
        r = resolve([{'type': 'hole-array', 'x': 1.0, 'y': 4.0, 'pitch_y': 0.5,
                      'cols': 1, 'rows': 3, 'size': '10-32'}])
        self.assertEqual(r['errors'], [])
        self.assertEqual(len(r['circles']), 3)

    def test_a_bearing_expands_to_one_large_hole(self):
        r = resolve([{'type': 'bearing', 'x': 1.0, 'y': 6.0}])
        self.assertEqual(r['errors'], [])
        self.assertAlmostEqual(r['circles'][0]['diameter'], 1.125)

    def test_a_pocket_expands_to_one_closed_ring(self):
        r = resolve([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0, 'h': 2.0,
                      'corner_radius': 0.25}])
        self.assertEqual(r['errors'], [])
        ring = r['pockets'][0]
        self.assertEqual(ring[0], ring[-1], 'pocket ring must be closed')
        self.assertTrue(Polygon(ring).is_valid)

    def test_the_resolver_reports_each_feature_for_the_editor(self):
        r = resolve([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '10-32'},
                     {'type': 'hole', 'x': 1.0, 'y': 6.05, 'size': '10-32'}])
        self.assertEqual([f['index'] for f in r['features']], [0, 1])
        self.assertEqual([f['type'] for f in r['features']], ['hole', 'hole'])


class TestRoundedRect(unittest.TestCase):
    """Pockets are rounded rectangles, sampled to the DXF path's chord tolerance."""

    def test_a_zero_radius_pocket_is_a_plain_rectangle(self):
        ring = tube_designer.rounded_rect(1.0, 6.0, 1.0, 2.0, 0.0)
        self.assertEqual(len(ring), 5)
        self.assertAlmostEqual(Polygon(ring).area, 2.0, places=6)

    def test_the_corners_are_real_arcs_not_chamfers(self):
        ring = tube_designer.rounded_rect(1.0, 6.0, 1.0, 2.0, 0.25)
        area = Polygon(ring).area
        exact = 1.0 * 2.0 - (4 - math.pi) * 0.25 ** 2
        # Inscribed, so a shade under the true area - which is the safe direction: the
        # pocket comes out fractionally small rather than fractionally over size.
        self.assertLessEqual(area, exact)
        self.assertAlmostEqual(area, exact, delta=0.002)

    def test_the_chords_stay_inside_the_tolerance(self):
        """A coarse arc is not a rounding error - the perimeter is emitted as these
        points, so a fat chord is metal left on the part."""
        r = 0.25
        ring = tube_designer.rounded_rect(1.0, 6.0, 1.0, 2.0, r)
        centre = (1.0 + 0.5 - r, 6.0 + 1.0 - r)          # top-right corner centre
        on_arc = [p for p in ring
                  if abs(math.hypot(p[0] - centre[0], p[1] - centre[1]) - r) < 1e-6]
        worst = 0.0
        for a, b in zip(on_arc, on_arc[1:]):
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            worst = max(worst, r - math.hypot(mid[0] - centre[0], mid[1] - centre[1]))
        self.assertLessEqual(worst, 0.001 + 1e-9)

    def test_a_radius_larger_than_the_pocket_is_refused_not_clamped(self):
        errors = errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0, 'h': 1.0,
                              'corner_radius': 0.75}])
        self.assertTrue(any('does not fit' in e for e in errors), errors)


class TestRefusals(unittest.TestCase):
    """One refusing case and one passing near miss for every validation row."""

    # --- finite numbers ---------------------------------------------------------
    def test_non_finite_positions_are_refused(self):
        for bad in (float('nan'), float('inf'), 'x', None):
            errors = errors_for([{'type': 'hole', 'x': bad, 'y': 6.0, 'size': '10-32'}])
            self.assertTrue(errors, f'x={bad!r} was accepted')

    def test_non_finite_pitch_and_count_are_refused(self):
        self.assertTrue(errors_for([{'type': 'hole-run', 'x': 1.0, 'y': 6.0,
                                     'pitch': float('nan'), 'count': 3, 'size': '10-32'}]))
        self.assertTrue(errors_for([{'type': 'hole-run', 'x': 1.0, 'y': 6.0,
                                     'pitch': 0.5, 'count': 2.5, 'size': '10-32'}]))

    def test_a_finite_position_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'hole', 'x': 1.0, 'y': 6.0,
                                      'size': '10-32'}]), [])

    # --- inside the face --------------------------------------------------------
    def test_a_feature_past_the_long_edge_margin_is_refused(self):
        errors = errors_for([{'type': 'hole', 'x': 0.3, 'y': 6.0, 'size': '1/4-20'}])
        self.assertTrue(any('long edge' in e for e in errors), errors)

    def test_a_feature_inside_the_long_edge_margin_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'hole', 'x': 0.4, 'y': 6.0,
                                      'size': '1/4-20'}]), [])

    def test_a_bearing_bore_does_not_fit_a_1_inch_face(self):
        """1.125" of bore on a 1" face is not a hole, it is the end of the tube."""
        errors = errors_for([{'type': 'bearing', 'x': 0.5, 'y': 6.0}], face_width=1.0)
        self.assertTrue(any('long edge' in e for e in errors), errors)

    def test_a_hole_too_close_to_a_cut_end_is_refused(self):
        errors = errors_for([{'type': 'hole', 'x': 1.0, 'y': 0.3, 'size': '10-32'}])
        self.assertTrue(any('cut end' in e for e in errors), errors)

    def test_a_hole_at_the_end_margin_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'hole', 'x': 1.0,
                                      'y': tube_patterns.MIN_END_MARGIN,
                                      'size': '10-32'}]), [])

    def test_a_pocket_hanging_off_the_end_is_refused(self):
        errors = errors_for([{'type': 'pocket', 'x': 1.0, 'y': 1.0, 'w': 1.0, 'h': 2.0,
                              'corner_radius': 0.25}])
        self.assertTrue(any('off the ends' in e for e in errors), errors)

    def test_a_pocket_clear_of_the_end_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'pocket', 'x': 1.0, 'y': 1.5, 'w': 1.0,
                                      'h': 2.0, 'corner_radius': 0.25}]), [])

    # --- the web between features ----------------------------------------------
    def test_features_closer_than_the_web_are_refused(self):
        errors = errors_for([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '1/4-20'},
                             {'type': 'hole', 'x': 1.0, 'y': 6.3, 'size': '1/4-20'}])
        self.assertTrue(any('metal between them' in e for e in errors), errors)

    def test_features_a_web_apart_are_accepted(self):
        self.assertEqual(errors_for([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '1/4-20'},
                                     {'type': 'hole', 'x': 1.0, 'y': 6.4,
                                      'size': '1/4-20'}]), [])

    def test_a_run_that_overlaps_itself_is_refused(self):
        errors = errors_for([{'type': 'hole-run', 'x': 1.0, 'y': 4.0, 'pitch': 0.3,
                              'count': 4, 'size': '1/4-20'}])
        self.assertTrue(any('its own holes' in e for e in errors), errors)

    def test_a_hole_touching_a_pocket_is_refused(self):
        errors = errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0, 'h': 2.0,
                              'corner_radius': 0.25},
                             {'type': 'hole', 'x': 1.0, 'y': 7.05, 'size': '10-32'}])
        self.assertTrue(any('metal between them' in e for e in errors), errors)

    def test_a_hole_a_web_clear_of_a_pocket_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0,
                                      'h': 2.0, 'corner_radius': 0.25},
                                     {'type': 'hole', 'x': 1.0, 'y': 7.3,
                                      'size': '10-32'}]), [])

    def test_overlapping_pockets_are_refused(self):
        """Two pockets sharing metal would have it cut twice, and the second pass
        climbs into air where the first already removed the wall."""
        errors = errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0, 'h': 2.0,
                              'corner_radius': 0.25},
                             {'type': 'pocket', 'x': 1.0, 'y': 6.5, 'w': 1.0, 'h': 2.0,
                              'corner_radius': 0.25}])
        self.assertTrue(any('metal between them' in e for e in errors), errors)

    # --- hole vs tool -----------------------------------------------------------
    def test_a_hole_smaller_than_the_tool_is_refused_by_feature_number(self):
        errors = errors_for([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '4-40'}])
        self.assertTrue(errors and errors[0].startswith('Feature 1:'), errors)
        self.assertIn('smaller than', errors[0])

    def test_a_hole_at_the_tool_size_is_accepted(self):
        """Within hole_size_tolerance: this is the peck-plunge case, not an error."""
        self.assertEqual(errors_for([{'type': 'hole', 'x': 1.0, 'y': 6.0,
                                      'diameter': TOOL}]), [])

    # --- pockets vs tool --------------------------------------------------------
    def test_a_pocket_the_tool_cannot_helix_into_is_refused(self):
        errors = errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 0.28, 'h': 2.0,
                              'corner_radius': 0.14}])
        self.assertTrue(any('room to helix' in e for e in errors), errors)

    def test_a_pocket_the_tool_can_helix_into_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 0.4,
                                      'h': 2.0, 'corner_radius': 0.2}]), [])

    def test_a_pocket_that_survives_entry_but_not_the_offset_is_refused(self):
        """The inradius says a circle fits; it does not say the CLEARED path survives
        being offset inward by the tool radius. On the truss path that gap produced a
        job that reported success and cut nothing. Separating the two tests takes an
        extreme case - here, a tool that plunges with no helix at all - because for a
        rounded rectangle the entry test is otherwise the tighter of the two."""
        errors = errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 0.53,
                              'h': 0.53, 'corner_radius': 0.265}],
                            tool=0.5, helix_radius_multiplier=0.0)
        self.assertTrue(any('nothing to clear' in e for e in errors), errors)

    def test_a_pocket_that_survives_the_offset_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 0.8,
                                      'h': 0.8, 'corner_radius': 0.4}],
                                    tool=0.5, helix_radius_multiplier=0.0), [])

    # --- corner radius ----------------------------------------------------------
    def test_a_corner_sharper_than_the_tool_is_refused(self):
        errors = errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0, 'h': 2.0,
                              'corner_radius': 0.05}])
        self.assertTrue(any('inside corner' in e for e in errors), errors)

    def test_a_corner_at_the_tool_radius_is_accepted(self):
        self.assertEqual(errors_for([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0,
                                      'h': 2.0, 'corner_radius': TOOL / 2.0}]), [])

    # --- unknown names ----------------------------------------------------------
    def test_an_unknown_feature_type_is_named_in_the_refusal(self):
        errors = errors_for([{'type': 'slot', 'x': 1.0, 'y': 6.0}])
        self.assertTrue(any("'slot'" in e for e in errors), errors)

    def test_an_unknown_size_is_named_in_the_refusal(self):
        errors = errors_for([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': 'M2'}])
        self.assertTrue(any("'M2'" in e for e in errors), errors)

    def test_an_unknown_bearing_is_named_in_the_refusal(self):
        errors = errors_for([{'type': 'bearing', 'x': 1.0, 'y': 6.0,
                              'bearing': 'thrust'}])
        self.assertTrue(any("'thrust'" in e for e in errors), errors)


class TestCaps(unittest.TestCase):
    """A design is small by construction. The caps make that true rather than assumed."""

    def test_too_many_features_are_refused(self):
        features = [{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '10-32'}
                    ] * (tube_designer.MAX_FEATURES + 1)
        errors = resolve(features)['errors']
        self.assertEqual(len(errors), 1)
        self.assertIn('at most', errors[0])

    def test_too_many_resolved_holes_are_refused(self):
        features = [{'type': 'hole-run', 'x': 1.0, 'y': 1.0, 'pitch': 0.5,
                     'count': 300, 'size': '10-32'},
                    {'type': 'hole-run', 'x': 1.5, 'y': 1.0, 'pitch': 0.5,
                     'count': 300, 'size': '10-32'}]
        errors = resolve(features, tube_length=200.0)['errors']
        self.assertTrue(any('the limit is' in e for e in errors), errors)

    def test_a_runaway_count_is_refused_as_a_number(self):
        errors = errors_for([{'type': 'hole-run', 'x': 1.0, 'y': 1.0, 'pitch': 0.5,
                              'count': 10 ** 9, 'size': '10-32'}])
        self.assertTrue(any('between 1 and' in e for e in errors), errors)


class TestDocumentShape(unittest.TestCase):
    """What arrives is JSON someone else wrote; none of it may crash the resolver."""

    def test_a_non_object_design_is_refused(self):
        self.assertTrue(tube_designer.resolve([], FACE, LENGTH, TOOL)['errors'])

    def test_features_must_be_a_list(self):
        self.assertTrue(tube_designer.resolve({'features': {}}, FACE, LENGTH,
                                              TOOL)['errors'])

    def test_a_feature_must_be_an_object(self):
        self.assertTrue(errors_for(['hole']))

    def test_an_empty_design_warns_rather_than_failing(self):
        r = resolve([])
        self.assertEqual(r['errors'], [])
        self.assertTrue(any('nothing would be cut' in w for w in r['warnings']))

    def test_the_tube_itself_must_be_real(self):
        for kwargs in ({'face_width': 0}, {'face_width': float('nan')},
                       {'tube_length': -1}, {'tool': 0}):
            with self.assertRaises(ValueError):
                resolve([], **kwargs)

    def test_a_refused_feature_contributes_no_geometry(self):
        """A design with an error generates nothing at all, but the editor still needs
        to know WHICH feature to paint red."""
        r = resolve([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '10-32'},
                     {'type': 'hole', 'x': 0.1, 'y': 6.0, 'size': '10-32'}])
        self.assertEqual(len(r['circles']), 1)
        self.assertTrue(r['features'][0]['ok'])
        self.assertFalse(r['features'][1]['ok'])
        self.assertTrue(r['features'][1]['errors'])

    def test_describe_counts_what_will_be_cut(self):
        r = resolve([{'type': 'hole', 'x': 1.0, 'y': 6.0, 'size': '10-32'},
                     {'type': 'pocket', 'x': 1.0, 'y': 10.0, 'w': 1.0, 'h': 2.0,
                      'corner_radius': 0.25}])
        self.assertEqual(tube_designer.describe(r), '1 hole, 1 pocket')
        self.assertEqual(tube_designer.describe(resolve([])), 'nothing yet')


class TestLoadTubeDesign(unittest.TestCase):
    """The post-processor side: a design loads exactly as a DXF tube face does."""

    def setUp(self):
        from frc_cam_postprocessor import FRCPostProcessor
        self.pp = FRCPostProcessor(0.0625, TOOL)
        self.pp.apply_material_preset('aluminum_tube')
        self.pp.tube_height = 1.0

    def _load(self, features, face_width=FACE, tube_length=12.0):
        return self.pp.load_tube_design({'version': 1, 'features': features},
                                        face_width, tube_length)

    def test_holes_are_classified_not_hand_built(self):
        """Same size checks and the same peck-vs-helical decision a drawn hole gets."""
        self._load([{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '8-32'},
                    {'type': 'bearing', 'x': 1.0, 'y': 6.0}])
        self.assertEqual(len(self.pp.holes), 2)
        by_d = {round(h['diameter'], 4): h for h in self.pp.holes}
        self.assertTrue(by_d[0.1695]['needs_peck_drill'])
        self.assertFalse(by_d[1.125]['needs_peck_drill'],
                         'a 1.125" bore must be helixed into, never plunged')

    def test_no_perimeter_is_invented_for_a_tube_face(self):
        self._load([{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'}])
        self.assertIsNone(self.pp.perimeter)

    def test_pockets_reach_the_post_processor(self):
        self._load([{'type': 'pocket', 'x': 1.0, 'y': 6.0, 'w': 1.0, 'h': 2.0,
                     'corner_radius': 0.25}])
        self.assertEqual(len(self.pp.pockets), 1)

    def test_the_mode_is_custom_so_the_header_says_end_mill(self):
        self._load([{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'}])
        self.assertEqual(self.pp.tube_pattern_mode, 'custom')

    def test_a_design_that_cannot_be_machined_is_refused_whole(self):
        with self.assertRaises(ValueError) as caught:
            self._load([{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'},
                        {'type': 'hole', 'x': 0.1, 'y': 3.0, 'size': '10-32'}])
        self.assertIn('Feature 2', str(caught.exception))

    def test_a_stale_error_does_not_condemn_the_next_design(self):
        """Errors are per-load. Without the reset, a design that failed validation left
        its errors behind and the next, valid one was refused citing features that no
        longer exist."""
        self.pp.errors = ['left over from a previous load']
        self._load([{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'}])
        self.assertEqual(self.pp.errors, [])

    def test_metric_jobs_are_refused_rather_than_silently_wrong(self):
        from frc_cam_postprocessor import FRCPostProcessor
        pp = FRCPostProcessor(1.6, 4.0, units='mm')
        pp.apply_material_preset('aluminum_tube')
        with self.assertRaises(ValueError) as caught:
            pp.load_tube_design({'features': []}, 50.8, 300.0)
        self.assertIn('inch-only', str(caught.exception))

    def test_squaring_the_end_stays_allowed_on_a_milled_design(self):
        """The drill refusal keys on mode == 'holes', which is the only mode that puts a
        twist drill in the spindle. A custom design is milled, so facing the end is an
        ordinary operation - and this test exists so that stays true."""
        self._load([{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'}])
        result = self.pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=True, cut_to_length=False,
            tube_width=FACE, tube_length=12.0)
        self.assertTrue(result.success, result.errors)
        self.assertIn('Square tube end', result.gcode)

    def test_the_same_design_still_refuses_to_run_off_the_machine(self):
        self._load([{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'}],
                   tube_length=12.0)
        result = self.pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=False, cut_to_length=False,
            tube_width=FACE, tube_length=2000.0)
        self.assertFalse(result.success)
        self.assertTrue(any('does not fit the machine' in e for e in result.errors))


class TestGeneratedProgram(unittest.TestCase):
    """What a mixed design actually emits. Claims are checked against the program."""

    @classmethod
    def setUpClass(cls):
        from frc_cam_postprocessor import FRCPostProcessor
        pp = FRCPostProcessor(0.0625, TOOL)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_design({'version': 1, 'features': [
            {'type': 'hole', 'x': 1.0, 'y': 2.0, 'size': '8-32'},
            {'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'},
            {'type': 'hole', 'x': 1.0, 'y': 4.0, 'size': '1/4-20'},
            {'type': 'bearing', 'x': 1.0, 'y': 6.0},
            {'type': 'pocket', 'x': 1.0, 'y': 9.0, 'w': 1.2, 'h': 2.0,
             'corner_radius': 0.25}]}, 2.0, 12.0)
        cls.pp = pp
        cls.gcode = pp.generate_tube_pattern_gcode(
            tube_height=1.0, square_end=False, cut_to_length=False,
            tube_width=2.0, tube_length=12.0).gcode

    def test_both_faces_are_machined(self):
        self.assertIn('PHASE 1', self.gcode)
        self.assertIn('PHASE 2', self.gcode)

    def test_the_bearing_bore_is_helixed_into_and_then_cleared(self):
        self.assertIn('Hole 1.125" dia: helical entry', self.gcode)
        self.assertIn('Archimedean spiral', self.gcode)

    def test_the_header_claims_an_end_mill_not_a_drill(self):
        """A design mixing 0.1695", 0.1935", 0.2656" and 1.125" cannot be drilled with
        one bit, and the operator loads whatever the header names."""
        self.assertNotIn('twist drill', self.gcode)
        self.assertIn(f'( Tool: {TOOL:.3f}" 1-flute end mill )', self.gcode)

    def test_the_header_says_what_the_design_contains(self):
        self.assertIn('( Custom design: 4 holes, 1 pocket )', self.gcode)

    def test_no_canned_cycles(self):
        self.assertNotIn('G83', self.gcode)

    def test_the_first_motion_retracts_before_moving_in_xy(self):
        """At program start the tool is wherever the last job left it; a rapid across
        the tube at that height drags the cutter through it."""
        for line in self.gcode.splitlines():
            code = re.sub(r'\(.*?\)', '', line).split(';')[0].strip()
            if not code or not re.match(r'G0?[0-3]\b', code):
                continue
            words = code.split()
            self.assertFalse(any(w[:1] in ('X', 'Y') for w in words)
                             and not any(w.startswith('Z') for w in words),
                             f'first motion moves in XY before retracting: {code}')
            break

    def test_no_rapid_traverses_inside_the_tube(self):
        """The wall top is at Z = tube height. A G0 with the tool below that, moving in
        XY, is the cutter being dragged sideways through metal."""
        tube_top = 1.0
        x = y = z = 0.0
        offences = []
        for line in self.gcode.splitlines():
            code = re.sub(r'\(.*?\)', '', line).split(';')[0].strip()
            if not code or not re.match(r'G0?[0-3]\b', code):
                continue
            words = dict((w[0], float(w[1:])) for w in code.split()[1:]
                         if w[:1] in 'XYZ')
            nx, ny, nz = words.get('X', x), words.get('Y', y), words.get('Z', z)
            moved_xy = abs(nx - x) > 1e-9 or abs(ny - y) > 1e-9
            if (re.match(r'G0?0\b', code) and moved_xy
                    and z < tube_top - 1e-6 and nz < tube_top - 1e-6):
                offences.append(code)
            x, y, z = nx, ny, nz
        self.assertEqual(offences, [])

    def test_comment_rules(self):
        for line in self.gcode.splitlines():
            line.encode('ascii')                       # no unicode reaches a controller
            depth = worst = 0
            for ch in line.split(';')[0]:
                if ch == '(':
                    depth += 1
                    worst = max(worst, depth)
                elif ch == ')':
                    depth -= 1
            self.assertLessEqual(worst, 1, f'nested comment: {line}')
            self.assertNotIn('[', line)
            self.assertNotIn(']', line)


class TestCustomDesignRoute(unittest.TestCase):
    """/process with tube_pattern=custom, and the editor's /api/tube-pattern POST."""

    @classmethod
    def setUpClass(cls):
        os.environ['PENGUINCAM_LOCAL'] = '1'
        from frc_cam_gui_app import app, limiter
        app.config['TESTING'] = True
        # /process allows 10 requests a minute, which is right for the deployed app and
        # wrong here; the limiter's 429 surfaces as a KeyError on the body rather than
        # as anything that reads like rate limiting.
        limiter.enabled = False
        cls.client = app.test_client()

    DESIGN = {'version': 1, 'features': [
        {'type': 'hole', 'x': 1.0, 'y': 2.0, 'size': '8-32'},
        {'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'},
        {'type': 'hole', 'x': 1.0, 'y': 4.0, 'size': '1/4-20'},
        {'type': 'bearing', 'x': 1.0, 'y': 6.0},
        {'type': 'pocket', 'x': 1.0, 'y': 9.0, 'w': 1.2, 'h': 2.0,
         'corner_radius': 0.25}]}

    def _post(self, **overrides):
        data = {'material': 'aluminum_tube', 'tube_pattern': 'custom',
                'tube_size': '2x1-flat', 'tube_pattern_length': '12',
                'tube_height': '1.0', 'thickness': '0.0625',
                'tool_diameter': '0.157', 'square_end': '0', 'cut_to_length': '0',
                'tube_design': json.dumps(self.DESIGN)}
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post('/process', data=data,
                                content_type='multipart/form-data')

    def _api(self, **overrides):
        data = {'size': '2x1-flat', 'length': '12', 'tool': '0.157',
                'design': json.dumps(self.DESIGN)}
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post('/api/tube-pattern', data=data)

    # --- /process ---------------------------------------------------------------
    def test_a_custom_design_needs_no_dxf(self):
        response = self._post()
        self.assertEqual(response.status_code, 200,
                         response.get_data(as_text=True)[:400])
        gcode = response.get_json()['gcode']
        self.assertIn('PHASE 1', gcode)
        self.assertIn('PHASE 2', gcode)

    def test_the_program_mills_the_mixed_sizes_rather_than_claiming_a_drill(self):
        gcode = self._post().get_json()['gcode']
        self.assertNotIn('twist drill', gcode)
        self.assertIn('Hole 1.125" dia: helical entry', gcode)

    def test_the_tool_reported_back_is_the_users_end_mill(self):
        """Unlike a drilled pattern, nothing is substituted here: a custom design is cut
        with the tool the user said they would load. The bore-only design is used
        because a 1/4" cutter cannot make the 8-32 holes in the mixed one - which the
        route refuses, correctly."""
        bore = {'features': [{'type': 'bearing', 'x': 1.0, 'y': 6.0}]}
        body = self._post(tool_diameter='0.25',
                          tube_design=json.dumps(bore)).get_json()
        self.assertAlmostEqual(body['parameters']['tool_diameter'], 0.25, places=6)
        self.assertIn('( Tool: 0.250" 1-flute end mill )', body['gcode'])

    def test_bad_json_is_a_400_not_a_500(self):
        response = self._post(tube_design='{"features": [')
        self.assertEqual(response.status_code, 400)
        self.assertIn('JSON', response.get_json()['error'])

    def test_a_missing_design_is_refused(self):
        response = self._post(tube_design=None)
        self.assertEqual(response.status_code, 400)

    def test_a_design_that_cuts_nothing_is_refused(self):
        response = self._post(tube_design=json.dumps({'features': []}))
        self.assertEqual(response.status_code, 400)
        self.assertIn('no features', response.get_json()['error'])

    def test_an_unmachinable_design_is_refused_naming_the_feature(self):
        bad = {'features': [{'type': 'hole', 'x': 0.1, 'y': 3.0, 'size': '10-32'}]}
        response = self._post(tube_design=json.dumps(bad))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Feature 1', response.get_json()['error'])

    def test_the_hole_cap_is_enforced_by_the_route(self):
        big = {'features': [{'type': 'hole-run', 'x': 0.5, 'y': 1.0, 'pitch': 0.5,
                             'count': 400, 'size': '10-32'},
                            {'type': 'hole-run', 'x': 1.5, 'y': 1.0, 'pitch': 0.5,
                             'count': 400, 'size': '10-32'}]}
        response = self._post(tube_design=json.dumps(big), tube_pattern_length='18')
        self.assertEqual(response.status_code, 400)

    def test_a_tube_longer_than_the_machine_is_still_refused(self):
        response = self._post(tube_pattern_length='2000')
        self.assertEqual(response.status_code, 400)
        self.assertIn('does not fit the machine', response.get_json()['error'])

    def test_an_unknown_pattern_is_still_refused(self):
        self.assertEqual(self._post(tube_pattern='swiss-cheese').status_code, 400)

    def test_the_response_carries_the_resolved_geometry_for_the_preview(self):
        """The viewer draws the tube itself, which it cannot do from G-code alone:
        nothing in a toolpath distinguishes a hole from a circular pocket."""
        preview = self._post().get_json()['tube_preview']
        self.assertEqual(preview['mode'], 'custom')
        self.assertEqual(preview['face_width'], 2.0)
        self.assertEqual(len(preview['holes']), 4)
        self.assertEqual(len(preview['pockets']), 1)
        self.assertTrue(any(abs(h['d'] - 1.125) < 1e-6 for h in preview['holes']))

    def test_the_counts_in_the_preview_match_the_program(self):
        body = self._post().get_json()
        holes = len(body['tube_preview']['holes'])
        self.assertEqual(body['gcode'].count('( Custom design: '), 1)
        self.assertIn(f'( Custom design: {holes} holes, 1 pocket )', body['gcode'])

    # --- /api/tube-pattern (POST) ------------------------------------------------
    def test_the_editor_gets_geometry_without_generating_gcode(self):
        body = self._api().get_json()
        self.assertEqual(body['mode'], 'custom')
        self.assertEqual(len(body['holes']), 4)
        self.assertEqual(len(body['pockets']), 1)
        self.assertEqual(body['errors'], [])
        self.assertEqual(body['summary'], '4 holes, 1 pocket')
        self.assertNotIn('gcode', body)

    def test_the_editor_is_told_which_feature_is_bad(self):
        bad = {'features': [{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '10-32'},
                            {'type': 'hole', 'x': 0.1, 'y': 3.0, 'size': '10-32'}]}
        body = self._api(design=json.dumps(bad)).get_json()
        self.assertTrue(body['features'][0]['ok'])
        self.assertFalse(body['features'][1]['ok'])
        self.assertTrue(body['features'][1]['errors'])
        self.assertTrue(body['errors'])

    def test_an_unmachinable_design_is_200_with_errors_not_400(self):
        """The editor asks on every edit; a design mid-edit is not a bad request."""
        bad = {'features': [{'type': 'hole', 'x': 0.1, 'y': 3.0, 'size': '10-32'}]}
        self.assertEqual(self._api(design=json.dumps(bad)).status_code, 200)

    def test_bad_json_is_a_400(self):
        response = self._api(design='{oops')
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_tube_size_is_refused(self):
        self.assertEqual(self._api(size='3x3').status_code, 400)

    def test_a_half_typed_length_is_answered_not_rejected(self):
        body = self._api(length='0').get_json()
        self.assertEqual(body['holes'], [])
        self.assertTrue(body['errors'])

    def test_the_envelope_is_reported_before_anything_is_generated(self):
        body = self._api(length='2000').get_json()
        self.assertTrue(any('does not fit the machine' in e for e in body['errors']))

    def test_the_named_sizes_are_resolved_server_side(self):
        """The browser sends a name; the server decides the number. A stale client
        cannot ship a wrong diameter."""
        design = {'features': [{'type': 'hole', 'x': 1.0, 'y': 3.0, 'size': '1/4-20',
                                'diameter': 0.75}]}
        body = self._api(design=json.dumps(design)).get_json()
        self.assertAlmostEqual(body['holes'][0]['d'], 0.2656, places=4)

    def test_the_generated_patterns_still_answer_on_get(self):
        response = self.client.get('/api/tube-pattern?size=2x1-flat&length=24&mode=holes')
        self.assertEqual(response.status_code, 200)
        # 47 columns of 3 on a 24" 2x1 face - the generated pattern, untouched.
        self.assertEqual(len(response.get_json()['holes']), 141)


if __name__ == '__main__':
    unittest.main()
