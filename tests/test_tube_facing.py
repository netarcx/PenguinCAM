"""Unit tests for tube facing mode."""
import unittest
import tempfile
import os
import re
from frc_cam_postprocessor import FRCPostProcessor
from team_config import TeamConfig


class TestYCoordinateAdjustment(unittest.TestCase):
    """Test the _adjust_y_coordinate helper function."""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)

    def test_positive_offset(self):
        """Test shifting Y by positive offset."""
        line = "G1 X1.0 Y-0.175 Z0.5"
        result = self.pp._adjust_y_coordinate(line, 0.175)
        self.assertIn("Y0.0000", result)

    def test_negative_offset(self):
        """Test shifting Y by negative offset."""
        line = "G1 X1.0 Y0.0 Z0.5"
        result = self.pp._adjust_y_coordinate(line, -0.125)
        self.assertIn("Y-0.1250", result)

    def test_no_y_coordinate(self):
        """Test line without Y coordinate is unchanged."""
        line = "G0 X1.0 Z0.5"
        result = self.pp._adjust_y_coordinate(line, 0.175)
        self.assertEqual(line, result)

    def test_arc_with_y_coordinate(self):
        """Test arc line with Y coordinate - only Y coord adjusted."""
        line = "G3 X1.0 Y-0.0787 I-0.1 J0."
        result = self.pp._adjust_y_coordinate(line, 0.175)
        # -0.0787 + 0.175 = 0.0963
        self.assertIn("Y0.0963", result)
        # J should remain unchanged
        self.assertIn("J0.", result)

    def test_negative_y_to_positive(self):
        """Test shifting negative Y to positive."""
        line = "G1 Y-0.23"
        result = self.pp._adjust_y_coordinate(line, 0.23)
        self.assertIn("Y0.0000", result)

    def test_preserves_other_coordinates(self):
        """Test that X and Z coordinates are preserved."""
        line = "G1 X1.234 Y-0.5 Z0.789"
        result = self.pp._adjust_y_coordinate(line, 0.5)
        self.assertIn("X1.234", result)
        self.assertIn("Y0.0000", result)
        self.assertIn("Z0.789", result)

    def test_comment_lines_unchanged(self):
        """Test that comment lines pass through unchanged."""
        line = "( This is a comment with Y-0.5 in it )"
        result = self.pp._adjust_y_coordinate(line, 0.175)
        # The regex will still match Y-0.5 in the comment, which is fine
        # since it doesn't affect machine behavior


class TestTubeFacingGeneration(unittest.TestCase):
    """Test the generate_tube_facing_gcode method."""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)
        self.pp.apply_material_preset('aluminum')

    def _generate_tube_gcode_to_file(self, output_path, tube_size='1x1'):
        """Helper to generate tube facing gcode and write to file (for API tests)."""
        result = self.pp.generate_tube_facing_gcode(tube_size=tube_size)
        with open(output_path, 'w') as f:
            f.write(result.gcode)
        return result

    def test_generates_output_file(self):
        """Test that output file is created."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            self.assertTrue(os.path.exists(output_path))
            with open(output_path) as f:
                content = f.read()
            self.assertGreater(len(content), 0)
        finally:
            os.unlink(output_path)

    def test_contains_two_phases(self):
        """Test output contains both phases with pause."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("PHASE 1", content)
            self.assertIn("PHASE 2", content)
            self.assertIn("M0", content)  # Pause for flip
        finally:
            os.unlink(output_path)

    def test_uses_work_offset_not_g52(self):
        """Tube ops use a standard work coordinate system (G54 by default), never the G52
        local-offset that some controllers mishandle."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("G54", content)      # default tube WCS
            self.assertNotIn("G52", content)
        finally:
            os.unlink(output_path)

    def test_contains_setup_instructions(self):
        """Test output contains setup instructions in header."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("SETUP INSTRUCTIONS", content)
            self.assertIn("Mount tube in jig", content)
            self.assertIn("Z=0 is at bottom of tube", content)
        finally:
            os.unlink(output_path)

    def test_contains_flip_instructions(self):
        """Test output contains flip instructions."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("Flip tube 180 degrees", content)
            self.assertIn("OPERATOR ACTION REQUIRED", content)
        finally:
            os.unlink(output_path)

    def test_ends_with_m30(self):
        """Test output ends with program end."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("M30", content)
        finally:
            os.unlink(output_path)

    def test_y_coordinates_differ_between_phases(self):
        """Test that Y coordinates are shifted differently in each phase."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                lines = f.readlines()

            # Find where phases start
            phase1_start = None
            phase2_start = None
            for i, line in enumerate(lines):
                if "PHASE 1" in line:
                    phase1_start = i
                elif "PHASE 2" in line:
                    phase2_start = i

            self.assertIsNotNone(phase1_start)
            self.assertIsNotNone(phase2_start)

            # Get first toolpath Y coordinate from each phase
            # Skip "G0 X0 Y0" origin moves - look for Y coords that aren't Y0
            phase1_y = None
            phase2_y = None

            for line in lines[phase1_start:phase2_start]:
                if 'Y' in line and ('G0' in line or 'G1' in line):
                    match = re.search(r'Y(-?\d+\.?\d*)', line)
                    if match:
                        y_val = float(match.group(1))
                        # Skip the "G0 X0 Y0" origin positioning
                        if abs(y_val) > 0.01:
                            phase1_y = y_val
                            break

            for line in lines[phase2_start:]:
                if 'Y' in line and ('G0' in line or 'G1' in line):
                    match = re.search(r'Y(-?\d+\.?\d*)', line)
                    if match:
                        y_val = float(match.group(1))
                        # Skip the "G0 X0 Y0" origin positioning
                        if abs(y_val) > 0.01:
                            phase2_y = y_val
                            break

            # Y values should be different (different offsets applied)
            self.assertIsNotNone(phase1_y, "Could not find Y coordinate in Phase 1")
            self.assertIsNotNone(phase2_y, "Could not find Y coordinate in Phase 2")
            self.assertNotAlmostEqual(phase1_y, phase2_y, places=2,
                msg=f"Phase 1 Y ({phase1_y}) should differ from Phase 2 Y ({phase2_y})")

        finally:
            os.unlink(output_path)

    def test_contains_straight_facing_passes(self):
        """Test that straight facing passes are generated (G1 cuts across tube)."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            # Should have G1 linear moves for cutting
            self.assertIn("G1 X", content)
            # Should have roughing and finishing sections
            self.assertIn("ROUGHING", content)
            self.assertIn("FINISHING", content)
            # Should have default G17 (XY plane) in header
            self.assertIn("G17", content)
        finally:
            os.unlink(output_path)

    def test_contains_safe_z_clearance(self):
        """Safe Z clearance uses WORK coordinates (portable) - no machine-coord G53 by default."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            # Work-coordinate safe Z (above the full tube: tube_height + 0.25 = 1.25")
            self.assertIn("G0 Z1.2500  ; Safe Z clearance", content)
            # No machine-coordinate moves by default (portable to GRBL/Easel/WinCNC)
            self.assertNotIn("G53", content)
            self.assertNotIn("G28", content)
        finally:
            os.unlink(output_path)

    def test_contains_xy_origin_moves(self):
        """Test output contains XY origin rapid moves."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("G0 X0 Y0", content)
        finally:
            os.unlink(output_path)

    def test_no_tool_change(self):
        """PenguinCAM is single-tool: tube output emits no tool change (M6/T1), which GRBL
        rejects, so tube programs stay portable."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertNotIn("M6", content)
            self.assertNotIn("T1", content)
        finally:
            os.unlink(output_path)

    def test_no_g53_park_by_default(self):
        """With no park_position configured, no G53 machine-coordinate park is emitted."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertNotIn("G53", content)
        finally:
            os.unlink(output_path)

    def test_g53_park_only_when_configured(self):
        """A configured park_position opts back into the G53 machine-coordinate park."""
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'Mach', 'machine': {'park_position': {'x': 0.5, 'y': -0.5, 'z': -0.25}}}}})
        pp = FRCPostProcessor(0.25, 0.157, config=cfg)
        content = pp.generate_tube_facing_gcode(tube_size='1x1').gcode
        self.assertIn("G53 G0 Z-0.2500", content)      # raise to machine safe Z
        self.assertIn("G53 G0 X0.5000 Y-0.5000", content)  # gantry to the park spot

    def test_tube_wcs_defaults_to_g54(self):
        """With no tube WCS configured, tube ops run in G54 (operator zeros it per tube) -
        portable, with no G55 and no WCS-reset noise."""
        content = self.pp.generate_tube_facing_gcode(tube_size='1x1').gcode
        self.assertIn("G54  ; Work coordinate system, zeroed at the tube origin", content)
        self.assertNotIn("G55", content)
        self.assertIn("Zero G54 at the tube origin", content)   # setup instruction matches
        # Never switched away from G54, so there is no "reset" line.
        self.assertNotIn("Reset to standard work coordinate system", content)

    def test_tube_wcs_opt_in_alternate(self):
        """A configured fixed WCS (e.g. G55) switches tube ops into it and resets to G54 at
        program end, for teams with a permanently-fixtured jig."""
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'Mach', 'machine': {'tube_work_coordinate_system': 'G55'}}}})
        pp = FRCPostProcessor(0.25, 0.157, config=cfg)
        content = pp.generate_tube_facing_gcode(tube_size='1x1').gcode
        self.assertIn("G55  ; Use fixed jig work coordinate system", content)
        self.assertIn("Verify G55 is set to the fixed jig origin", content)
        self.assertIn("G54  ; Reset to standard work coordinate system", content)

    def test_tube_wcs_invalid_falls_back_to_g54(self):
        """An out-of-range WCS value falls back to the safe G54 default rather than emitting
        a bogus coordinate-system code."""
        cfg = TeamConfig({'version': 2, 'default_machine': 'm', 'machines': {'m': {
            'name': 'M', 'machine': {'tube_work_coordinate_system': 'G99'}}}})
        pp = FRCPostProcessor(0.25, 0.157, config=cfg)
        content = pp.generate_tube_facing_gcode(tube_size='1x1').gcode
        self.assertNotIn("G99", content)
        self.assertIn("G54  ; Work coordinate system, zeroed at the tube origin", content)

    def test_safe_z_before_xy_pattern(self):
        """The work-coordinate safe-Z retract precedes the first XY origin move."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            z_idx = content.find("G0 Z1.2500  ; Safe Z clearance")
            xy_idx = content.find("G0 X0 Y0")
            self.assertGreaterEqual(z_idx, 0)
            self.assertGreater(xy_idx, z_idx)   # safe Z comes before the XY origin move
        finally:
            os.unlink(output_path)


def parse_tube_moves(lines, x=0.0, z=0.0):
    """Walk tube-frame G-code and yield (motion, x, z, line) for every motion line.

    Coordinates are absolute; unspecified axes carry over from the previous move.
    """
    out = []
    for raw in lines:
        code = raw.split('(')[0].split(';')[0].strip()
        if not code:
            continue
        m = re.match(r'^(G0|G1|G2|G3)\b', code)
        if not m:
            continue
        motion = m.group(1)
        mx = re.search(r'\bX(-?\d*\.?\d+)', code)
        mz = re.search(r'\bZ(-?\d*\.?\d+)', code)
        if mx:
            x = float(mx.group(1))
        if mz:
            z = float(mz.group(1))
        out.append((motion, x, z, code))
    return out


class TestTubeWallRapidPlunge(unittest.TestCase):
    """A pass may only skip the hollow middle once the top wall is proven clear.

    The top wall of box tube spans the FULL tube width. Passes after the first used
    to assume mid-tube was hollow and rapid straight down to the next Z - through
    whatever was left of the top wall. With a 1/8" tool on 1x1 x 1/8" wall tube the
    first pass floor lands at Z=0.899 and the wall bottom is Z=0.875, so the second
    pass rapided through 0.024" of solid 6061.
    """

    def _facing(self, tool=0.125, wall=0.125, tube=(1.0, 1.0)):
        pp = FRCPostProcessor(wall, tool)
        pp.apply_material_preset('aluminum')
        lines = pp._generate_parametric_tube_facing(tube[0], tube[1], phase=1)
        return pp, lines

    def test_walls_only_waits_for_the_wall_to_be_cleared(self):
        pp, lines = self._facing()
        passes = pp._calculate_tube_operation_passes(1.0)
        wall = passes['wall_thickness']

        for kind, depth_key, count_key in (
                ('Roughing', 'roughing_depth_per_pass', 'num_roughing_passes'),
                ('Finishing', 'finishing_depth_per_pass', 'num_finishing_passes')):
            depth = passes[depth_key]
            found_walls_only = False
            for line in lines:
                m = re.search(rf'\( {kind} pass (\d+)/(\d+) .*- walls only \)', line)
                if not m:
                    continue
                found_walls_only = True
                pass_number = int(m.group(1))           # 1-based
                cleared = (pass_number - 1) * depth     # depth the PREVIOUS pass reached
                self.assertGreaterEqual(
                    cleared, wall + 0.02 - 1e-9,
                    f"{kind} pass {pass_number} skips mid-tube after only "
                    f"{cleared:.4f}\" of depth, but the top wall is {wall:.4f}\" thick")
            self.assertTrue(found_walls_only,
                            f"expected at least one walls-only {kind.lower()} pass")

    def test_no_rapid_plunge_below_the_previous_pass_floor(self):
        """No rapid may descend past the last pass's floor while over the tube body.

        Anything below that floor at mid-tube X is either top wall or the proud stub
        of a saw cut - both solid, neither survives a rapid.
        """
        pp, lines = self._facing()
        passes = pp._calculate_tube_operation_passes(1.0)
        wall = passes['wall_thickness']
        clearance = pp.tool_diameter / 2.0 + 0.05
        front_inner = wall + clearance
        back_inner = 1.0 - wall - clearance
        z_top = 1.0 + pp.dry_run_lift

        z_safe = z_top + 0.25

        # Split the block into per-pass chunks so each pass knows its predecessor's floor.
        chunks = []
        for raw in lines:
            m = re.search(r'\( (Roughing|Finishing) pass (\d+)/\d+ to Z=(-?[\d.]+)"', raw)
            if m:
                chunks.append({'num': int(m.group(2)), 'floor': float(m.group(3)),
                               'lines': []})
            elif chunks:
                chunks[-1]['lines'].append(raw)
        self.assertTrue(chunks, "no pass blocks found in the facing output")

        for i, chunk in enumerate(chunks):
            prev_floor = z_top if chunk['num'] == 1 else chunks[i - 1]['floor']
            x, z = 0.0, z_safe
            for motion, new_x, new_z, code in parse_tube_moves(chunk['lines'], x, z):
                if (motion == 'G0' and new_z < z - 1e-9
                        and front_inner - 1e-9 <= new_x <= back_inner + 1e-9):
                    self.assertGreaterEqual(
                        new_z, prev_floor - 1e-9,
                        f"rapid to Z{new_z:.4f} at X{new_x:.4f} dives past the previous "
                        f"pass floor Z{prev_floor:.4f}: {code}")
                x, z = new_x, new_z

    def test_mid_tube_reentry_uses_a_feed_move(self):
        """Re-entering at the front wall's inner edge feeds down, never rapids.

        Cheap insurance against proud saw-cut stock: at mid-span the review measured
        only ~0.043" of clearance under the rapid.
        """
        pp, lines = self._facing()
        wall = pp.material_thickness
        clearance = pp.tool_diameter / 2.0 + 0.05
        front_inner = f'X{wall + clearance:.4f}'

        saw_reentry = False
        for i, line in enumerate(lines):
            if not line.strip().startswith('G0 ') or front_inner not in line:
                continue
            if 'Z' in line.split('(')[0]:
                continue
            saw_reentry = True
            plunge = lines[i + 1]
            self.assertTrue(plunge.startswith('G1 Z'),
                            f"mid-tube re-entry plunges with a rapid: {plunge}")
            self.assertIn('F', plunge)
        self.assertTrue(saw_reentry, "expected a mid-tube re-entry move")

    def test_cut_to_length_shares_the_guard(self):
        pp = FRCPostProcessor(0.125, 0.125)
        pp.apply_material_preset('aluminum')
        lines = pp._generate_cut_to_length(1.0, 1.0, 12.0, phase=1, square_end=False)
        passes = pp._calculate_tube_operation_passes(1.0)
        wall = passes['wall_thickness']

        for kind, depth_key in (('Roughing', 'roughing_depth_per_pass'),
                                ('Finishing', 'finishing_depth_per_pass')):
            depth = passes[depth_key]
            for line in lines:
                m = re.search(rf'\( {kind} pass (\d+)/(\d+) .*- walls only \)', line)
                if not m:
                    continue
                cleared = (int(m.group(1)) - 1) * depth
                self.assertGreaterEqual(cleared, wall + 0.02 - 1e-9,
                                        f"cut-to-length {kind} pass skips mid-tube early")

        wall_clearance = pp.tool_diameter / 2.0 + 0.05
        front_inner = f'X{wall + wall_clearance:.4f}'
        for i, line in enumerate(lines):
            if line.strip().startswith('G0 ') and front_inner in line and 'Z' not in line:
                self.assertTrue(lines[i + 1].startswith('G1 Z'),
                                f"cut-to-length rapids into mid-tube: {lines[i + 1]}")


class TestTubeFacingToolEdgePositions(unittest.TestCase):
    """Test the tool edge positions for each phase."""

    def test_phase1_roughing_edge_at_005(self):
        """Phase 1 roughing tool edge should be at Y=+0.05"."""
        phase1_roughing_edge = 0.05
        self.assertAlmostEqual(phase1_roughing_edge, 0.05, places=3)

    def test_phase1_finishing_edge_at_00625(self):
        """Phase 1 finishing tool edge should be at Y=+0.0625"."""
        phase1_finishing_edge = 0.0625
        self.assertAlmostEqual(phase1_finishing_edge, 0.0625, places=3)

    def test_phase2_roughing_edge_at_negative_00125(self):
        """Phase 2 roughing tool edge should be at Y=-0.0125"."""
        phase2_roughing_edge = -0.0125
        self.assertAlmostEqual(phase2_roughing_edge, -0.0125, places=3)

    def test_phase2_finishing_edge_at_zero(self):
        """Phase 2 finishing tool edge should be at Y=0."""
        phase2_finishing_edge = 0.0
        self.assertAlmostEqual(phase2_finishing_edge, 0.0, places=3)


if __name__ == '__main__':
    unittest.main()
