"""Regenerate examples/example_plate.dxf, the part the shipped example job cuts.

    uv run python examples/make_example_plate.py

The example job exists to show every operation type working, so the part has to HAVE
one of each: small holes for the 1/8" cutter, a bore big enough to need the 1/4", a
pocket to clear, and an outline to profile and chamfer. It used to point at
sample_part.dxf, which has none of them - three of the job's five operations emitted
nothing at all, which is a poor advertisement for a multi-tool example.

Generated rather than committed as opaque binary-ish text so the shapes are readable
and adjustable: the numbers below ARE the documentation of what the example machines.
"""

import os

import ezdxf

#: Overall plate, inches. Comfortably inside the 24x24 default machine.
WIDTH, HEIGHT = 6.0, 4.0

#: Four #10 clearance holes (0.201"), one at each corner. Too small for the 1/4"
#: cutter, so the example's small-hole operation has something only T1 can make.
SMALL_HOLES = [(0.75, 0.75), (5.25, 0.75), (0.75, 3.25), (5.25, 3.25)]
SMALL_HOLE_DIAMETER = 0.201

#: One bearing-sized bore, comfortably over the 0.3" split the example scopes on, so
#: the large-hole operation picks it up and the small one does not.
BORE_CENTRE = (1.75, 2.0)
BORE_DIAMETER = 0.875

#: A rectangular pocket with rounded corners the 1/4" cutter can actually turn. Cut to
#: a partial depth in the example, which is what makes it a pocket rather than a window.
POCKET = (3.0, 1.25, 5.0, 2.75)
POCKET_CORNER_RADIUS = 0.25


def rounded_rectangle(x0, y0, x1, y1, radius, segments=16):
    """Corner points of a rounded rectangle, counter-clockwise."""
    import math

    corners = [
        (x1 - radius, y0 + radius, -90.0),   # bottom right
        (x1 - radius, y1 - radius, 0.0),     # top right
        (x0 + radius, y1 - radius, 90.0),    # top left
        (x0 + radius, y0 + radius, 180.0),   # bottom left
    ]
    points = []
    for cx, cy, start in corners:
        for i in range(segments + 1):
            angle = math.radians(start + 90.0 * i / segments)
            points.append((cx + radius * math.cos(angle),
                           cy + radius * math.sin(angle)))
    return points


def build(path):
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    doc.header['$INSUNITS'] = 1          # inches, so the units cross-check is happy

    msp.add_lwpolyline([(0, 0), (WIDTH, 0), (WIDTH, HEIGHT), (0, HEIGHT)], close=True)
    for x, y in SMALL_HOLES:
        msp.add_circle((x, y), SMALL_HOLE_DIAMETER / 2.0)
    msp.add_circle(BORE_CENTRE, BORE_DIAMETER / 2.0)
    msp.add_lwpolyline(rounded_rectangle(*POCKET, POCKET_CORNER_RADIUS), close=True)

    doc.saveas(path)
    return path


if __name__ == '__main__':
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'example_plate.dxf')
    print(f'Wrote {build(target)}')
