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

import math
import os
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


if __name__ == '__main__':
    unittest.main()
