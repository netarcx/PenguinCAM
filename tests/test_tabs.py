"""Tabs: the only thing holding a profiled part while the cutter is still moving.

Two failures were verified in real output. On a small part the tab spacing was computed
over `contour_length - ramp_distance`, and aluminum's 4-degree ramp on 1/4" stock is
about 4.6" - so a 1"x1" part got a NEGATIVE spacing and stacked all three tabs on one
point. And because only the final pass lifted over the tab zones, every intermediate
pass cut straight through them: on 5-pass 1/4" aluminum the standing tabs were 0.054"
instead of the configured 0.15", a third of the designed holding area.
"""

import io
import math
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf

from frc_cam_postprocessor import FRCPostProcessor
from team_config import TeamConfig


def square_part(side=1.0):
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (side, 0), (side, side), (0, side)], close=True)
    path = tempfile.mktemp(suffix='.dxf')
    doc.saveas(path)
    return path


def build(side=1.0, material='aluminum', thickness=0.25, tool=0.157, config=None):
    path = square_part(side)
    try:
        with redirect_stdout(io.StringIO()):
            pp = FRCPostProcessor(thickness, tool, config=config) if config \
                else FRCPostProcessor(thickness, tool)
            pp.apply_material_preset(material)
            pp.load_dxf(path)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            result = pp.generate_gcode(suggested_filename='tab',
                                       timestamp='2026-08-27 12:00')
        return pp, result
    finally:
        os.remove(path)


def moves(lines):
    """(x, y, z, is_cut, text) for every motion line, coordinates carried forward."""
    x = y = z = None
    out = []
    for raw in lines:
        code = raw.split('(')[0].split(';')[0].strip()
        m = re.match(r'^(G0|G1|G2|G3)\b', code)
        if not m:
            continue
        for letter, store in (('X', 'x'), ('Y', 'y'), ('Z', 'z')):
            found = re.search(rf'\b{letter}(-?\d*\.?\d+)', code)
            if found:
                if store == 'x':
                    x = float(found.group(1))
                elif store == 'y':
                    y = float(found.group(1))
                else:
                    z = float(found.group(1))
        out.append((x, y, z, m.group(1) != 'G0', raw))
    return out


def point_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class TestTabPlacement(unittest.TestCase):

    def test_small_part_gets_positive_spacing(self):
        pp, result = build(side=1.0)
        self.assertTrue(result.success, result.errors)
        m = re.search(r'\(Tabs: (\d+) tabs - desired spacing: ([\d.]+)", '
                      r'actual: (-?[\d.]+)"', result.gcode)
        self.assertIsNotNone(m, 'no tab header line in the program')
        num_tabs, actual = int(m.group(1)), float(m.group(3))
        self.assertGreaterEqual(num_tabs, 3)
        self.assertGreaterEqual(
            actual, pp.tab_width,
            'tab spacing collapsed - the ramp ate the whole perimeter, so the tabs '
            'are closer together than they are wide')

    def test_tabs_do_not_stack_on_one_point(self):
        pp, result = build(side=1.0)
        lines = result.gcode.splitlines()
        centres = self._tab_centres(lines)
        self.assertGreaterEqual(len(centres), 3, f'only {len(centres)} tabs')
        for i, a in enumerate(centres):
            for b in centres[i + 1:]:
                self.assertGreaterEqual(
                    math.hypot(a[0] - b[0], a[1] - b[1]), pp.tab_width - 1e-6,
                    f'tabs at {a} and {b} overlap; they hold nothing')

    @staticmethod
    def _tab_paths(lines):
        """Each tab's waypoint polyline, taken from the removal pass's own moves.

        One tab can straddle several contour chords - a rounded corner is many short
        ones - so a tab is a path, not a pair of points.
        """
        start = next((i for i, l in enumerate(lines)
                      if 'TAB REMOVAL PASS' in l), None)
        if start is None:
            return []
        paths = []
        current = None
        for raw in lines[start:]:
            mx = re.search(r'X(-?[\d.]+)', raw)
            my = re.search(r'Y(-?[\d.]+)', raw)
            if 'tab start (in kerf)' in raw and mx and my:
                if current and len(current) > 1:
                    paths.append(current)
                current = [(float(mx.group(1)), float(my.group(1)))]
            elif 'Cut through tab' in raw and current is not None and mx and my:
                point = (float(mx.group(1)), float(my.group(1)))
                if point != current[-1]:
                    current.append(point)
        if current and len(current) > 1:
            paths.append(current)
        # Each tab is cut once per removal pass, so dedupe by the entry point.
        unique = {}
        for path in paths:
            unique.setdefault(path[0], path)
        return list(unique.values())

    @classmethod
    def _tab_centres(cls, lines):
        """The midpoint of each tab measured ALONG the contour, not the chord."""
        centres = []
        for path in cls._tab_paths(lines):
            spans = [math.hypot(b[0] - a[0], b[1] - a[1])
                     for a, b in zip(path, path[1:])]
            half = sum(spans) / 2.0
            walked = 0.0
            for (a, b), span in zip(zip(path, path[1:]), spans):
                if walked + span >= half or span == 0:
                    t = (half - walked) / span if span else 0.0
                    centres.append((a[0] + t * (b[0] - a[0]),
                                    a[1] + t * (b[1] - a[1])))
                    break
                walked += span
        return centres

    def test_a_part_too_small_for_three_tabs_is_refused(self):
        """Not a silent one-tab part: say the settings do not fit the geometry."""
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'M', 'materials': {'aluminum': {'tab_width': 2.0}}}}})
        pp, result = build(side=1.0, config=cfg)
        self.assertFalse(result.success)
        joined = ' '.join(result.errors).lower()
        self.assertIn('tab', joined)
        self.assertTrue('small' in joined or 'short' in joined, result.errors)


class TestStandingTabHeight(unittest.TestCase):
    """Simulate the emitted program, not the generator: how much material is actually
    left under each tab when the profile finishes?"""

    def _standing(self, side=1.0, thickness=0.25):
        pp, result = build(side=side, thickness=thickness)
        self.assertTrue(result.success, result.errors)
        lines = result.gcode.splitlines()
        removal = next((i for i, l in enumerate(lines)
                        if 'TAB REMOVAL PASS' in l), len(lines))
        centres = TestTabPlacement._tab_centres(lines)
        self.assertTrue(centres, 'no tabs found')

        floor = {c: pp.material_top for c in centres}
        prev = None
        for x, y, z, is_cut, raw in moves(lines[:removal]):
            if None in (x, y, z):
                prev = (x, y, z)
                continue
            if prev is not None and is_cut and None not in prev:
                for c in centres:
                    if point_to_segment(c[0], c[1], prev[0], prev[1], x, y) \
                            <= pp.tool_radius + 1e-6:
                        floor[c] = min(floor[c], prev[2], z)
            prev = (x, y, z)
        return pp, floor

    def test_tabs_keep_their_configured_height(self):
        pp, floor = self._standing()
        for centre, z in floor.items():
            standing = z - pp.cut_depth
            self.assertGreaterEqual(
                standing, pp.tab_height - 1e-4,
                f"tab at {centre} stands only {standing:.4f}\" of the configured "
                f"{pp.tab_height:.4f}\" - intermediate passes cut through it")

    def test_removal_pass_steps_through_the_full_tab(self):
        """The removal pass has to chew the height the tabs ACTUALLY have. It used to
        step from the thinned height, which is the same bug seen from the other end."""
        pp, result = build(side=1.0)
        lines = result.gcode.splitlines()
        start = next(i for i, l in enumerate(lines) if 'TAB REMOVAL PASS' in l)
        block = lines[start:]
        plunges = [float(re.search(r'Z(-?[\d.]+)', l).group(1))
                   for l in block if 'Plunge in kerf' in l]
        self.assertTrue(plunges)
        # Nothing steps deeper than the machine's depth-per-pass limit.
        top = min(pp.material_top, pp.cut_depth + pp.tab_height)
        previous = top
        for z in plunges:
            if z > previous:            # a new tab, back to the top
                previous = top
            self.assertLessEqual(previous - z, pp.max_slotting_depth + 1e-6,
                                 f'tab removal steps {previous - z:.4f}" in one pass')
            previous = z
        self.assertAlmostEqual(min(plunges), pp.cut_depth, places=4)


if __name__ == '__main__':
    unittest.main()
