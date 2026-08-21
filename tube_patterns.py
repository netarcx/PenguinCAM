"""Pre-designed hole and lightening patterns for FRC box tubing.

A tube job normally takes its pattern from a DXF the user drew. That is the right tool
when the pattern is specific to the part, and needless work when it is the pattern every
team drills into every piece of 1x1 and 2x1: a run of #10 clearance holes on half-inch
centres, and - on 2x1, where there is room between the two hole rows - a line of
triangular lightening pockets.

This module generates that geometry directly, so a tube can be machined with no CAD step
at all. It emits the SAME shapes the DXF path produces (circles, and closed polygons for
pockets) and hands them to FRCPostProcessor.classify_holes(), so generated holes go
through exactly the same size checks, peck-vs-helical decision and error reporting as
drawn ones. Nothing here decides how to cut anything; it only decides where the holes go.

Coordinates match the tube pattern frame used by generate_tube_pattern_gcode:

    X  across the machined face, 0 at the face's left edge
    Y  along the tube, 0 at the machined end, increasing into the tube
    (Z is the tube height and is not our business - the wall depth is the cut depth.)

The pattern is machined on both faces, with face 2 mirrored in X by the caller. Every
pattern here is symmetric about the face centreline, so the mirrored copy lands on the
same hole centres - a tube can be bolted from either side.
"""

import math

#: #10 clearance. The FRC default: it passes a #10 screw with room for build tolerance,
#: and it is what the hole spacing below is dimensioned around.
HOLE_DIAMETER = 0.201

#: Hole pitch along the tube. Half an inch is the FRC convention, so parts designed to
#: this grid interchange between tubes.
HOLE_SPACING = 0.5

#: Distance from a long edge of the face to a hole row centre. On 2x1 this puts the two
#: rows an inch apart; on a narrow face there is only one row and it is centred instead.
ROW_INSET = 0.5

#: A face narrower than this gets a single centred row. Two rows an inch apart need
#: 2 * ROW_INSET of face plus material outside them; on a 1" face they would sit on the
#: corner radii of the extrusion, where a drill wanders and a bolt head has nothing flat
#: to sit on.
TWO_ROW_MIN_WIDTH = 1.5

#: Minimum distance from the machined end of the tube to the CENTRE of the nearest hole.
#: Holes closer than this to a cut end tear out rather than cut cleanly.
MIN_END_MARGIN = 0.375

#: Material left between a hole edge and a pocket edge, and between adjacent pockets.
#: This is the truss web - the part actually carrying load - so it is a floor, not a
#: target: pockets shrink to preserve it and are dropped entirely if they cannot.
MIN_WEB = 0.125

#: Y-extent of one truss cell (pocket + following web). Two hole pitches, so the truss
#: stays in step with the hole grid however long the tube is.
TRUSS_CELL = 1.0

#: Clearance the tool needs INSIDE a pocket beyond its own radius. A pocket whose
#: inscribed circle only just equals the cutter cannot be cleared - the toolpath
#: degenerates to a point or vanishes when offset inward - so such pockets are dropped
#: with a warning rather than emitted as geometry the post-processor will silently skip.
POCKET_TOOL_CLEARANCE = 0.01


def hole_rows(face_width):
    """X positions of the hole rows across a face of this width.

    Two rows inset from each edge when there is room, otherwise one centred row.
    """
    if face_width >= TWO_ROW_MIN_WIDTH:
        return [ROW_INSET, face_width - ROW_INSET]
    return [face_width / 2.0]


def hole_run(tube_length, spacing=HOLE_SPACING, end_margin=MIN_END_MARGIN):
    """Y centres of one row of holes, centred on the tube length.

    Centred rather than marched from the end so both ends of the tube get equal material,
    which is what keeps a tube symmetric when it is cut down later. Returns [] when the
    tube is too short to take even one hole at a safe distance from both ends.
    """
    usable = tube_length - 2.0 * end_margin
    if usable < 0:
        return []
    count = int(math.floor(usable / spacing)) + 1
    span = (count - 1) * spacing
    start = (tube_length - span) / 2.0
    return [start + i * spacing for i in range(count)]


def _triangle_inradius(a, b):
    """Radius of the largest circle fitting inside a right triangle with legs a and b.

    This is the real constraint on whether a cutter can clear the pocket: the tool has to
    fit in the tightest part of the shape, which for a triangle is nowhere near its area
    or its bounding box.
    """
    c = math.hypot(a, b)
    return (a + b - c) / 2.0


def truss_pockets(face_width, tube_length, hole_ys, tool_diameter,
                  hole_diameter=HOLE_DIAMETER, web=MIN_WEB, cell=TRUSS_CELL):
    """Right-triangle lightening pockets down the middle of the face, as a truss.

    The pockets live in the band between the two hole rows. Each cell holds one right
    triangle, and the right angle alternates between the left and right side of the band
    from cell to cell, so the material left between them runs diagonally: a zigzag web
    that carries load in tension and compression rather than a straight rail that carries
    it in bending.

    Returns (pockets, warnings). A face with only one hole row has no band and gets no
    pockets - that is the 1x1 case, and it is not an error.
    """
    warnings = []
    rows = hole_rows(face_width)
    if len(rows) < 2:
        return [], warnings          # single-row face: no room between rows, by design

    hole_r = hole_diameter / 2.0
    band_lo = rows[0] + hole_r + web
    band_hi = rows[-1] - hole_r - web
    band = band_hi - band_lo
    if band <= 0:
        warnings.append(
            'No room for lightening pockets between the hole rows on a '
            f'{face_width:.3f}" face; holes only.')
        return [], warnings

    if not hole_ys:
        return [], warnings

    # Span the pockets over the same length the holes occupy, so the truss starts and
    # stops with the hole grid instead of running out past the last hole.
    run_lo, run_hi = hole_ys[0], hole_ys[-1]
    run = run_hi - run_lo
    cells = int(math.floor(run / cell))
    if cells < 1:
        return [], warnings          # too short for even one triangle; holes only

    # Centre the whole truss on the hole run, for the same reason the holes themselves
    # are centred: a symmetric tube stays symmetric.
    truss_span = cells * cell
    origin = run_lo + (run - truss_span) / 2.0

    leg_y = cell - web              # triangle's Y leg; the web is the gap to the next cell
    inradius = _triangle_inradius(band, leg_y)
    needed = tool_diameter / 2.0 + POCKET_TOOL_CLEARANCE
    if inradius < needed:
        warnings.append(
            f'Lightening pockets skipped: a {tool_diameter:.3f}" tool does not fit the '
            f'{band:.3f}" x {leg_y:.3f}" triangle (needs {needed * 2:.3f}" of room, '
            f'the triangle holds {inradius * 2:.3f}"). Use a smaller tool for pockets.')
        return [], warnings

    pockets = []
    for i in range(cells):
        y0 = origin + i * cell + web / 2.0
        y1 = y0 + leg_y
        if i % 2 == 0:
            # Right angle at the low-X end of the cell; apex points to high X.
            tri = [(band_lo, y0), (band_hi, y0), (band_lo, y1)]
        else:
            tri = [(band_lo, y0), (band_hi, y0), (band_hi, y1)]
        tri.append(tri[0])           # closed ring, as the pocket machining expects
        pockets.append(tri)
    return pockets, warnings


def generate(face_width, tube_length, tool_diameter,
             hole_diameter=HOLE_DIAMETER, spacing=HOLE_SPACING, pockets=True):
    """Build a standard tube pattern.

    Returns a dict with:
        circles   - [{'center': (x, y), 'radius': r, 'diameter': d}], for classify_holes
        pockets   - [[(x, y), ...]] closed rings
        warnings  - human-readable notes; not errors, the pattern is still usable
    """
    warnings = []
    if tube_length <= 0:
        raise ValueError('Tube length must be positive')
    if face_width <= 0:
        raise ValueError('Face width must be positive')

    rows = hole_rows(face_width)
    ys = hole_run(tube_length, spacing=spacing)
    if not ys:
        warnings.append(
            f'A {tube_length:.3f}" tube is too short to hold a hole at least '
            f'{MIN_END_MARGIN:.3f}" from both ends; no holes generated.')

    hole_r = hole_diameter / 2.0
    circles = []
    for x in rows:
        for y in ys:
            circles.append({'center': (x, y), 'radius': hole_r, 'diameter': hole_diameter})

    pocket_rings = []
    if pockets:
        pocket_rings, pocket_warnings = truss_pockets(
            face_width, tube_length, ys, tool_diameter, hole_diameter=hole_diameter)
        warnings.extend(pocket_warnings)

    return {'circles': circles, 'pockets': pocket_rings, 'warnings': warnings,
            'rows': rows, 'hole_ys': ys}
