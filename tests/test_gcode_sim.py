"""Regression tests for the standalone G-code heightmap parser/simulator."""

import unittest

from gcode_sim import parse_moves, simulate


class TestInitialMachinePosition(unittest.TestCase):
    def test_first_retract_does_not_invent_a_move_from_work_zero(self):
        """The cutter's position before the program starts is unknown, not XYZ zero."""
        text = "G20\nG0 Z1.0\nG0 X2.0 Y3.0\nG1 Z-0.1 F10\n"
        moves = parse_moves(text)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0], ('feed', 2.0, 3.0, 1.0, 2.0, 3.0, -0.1))

    def test_heightmap_has_no_phantom_cut_at_the_origin(self):
        text = """(Units: inch)
(Material: plywood, 0.250\" thick)
( Material top: Z=0.2500 )
( Tool: 0.125\" diam Flat End Mill )
G20
G0 Z1.0
G0 X2.0 Y3.0
G1 Z-0.1 F10
G0 Z1.0
"""
        heightmap = simulate(text, res=0.025)
        self.assertGreater(heightmap.grid['minx'], 1.0)
        self.assertGreater(heightmap.grid['miny'], 2.0)
        self.assertLess(float(heightmap.z.min()), 0.0)


if __name__ == '__main__':
    unittest.main()
