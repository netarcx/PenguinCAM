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

#: Distance from a long edge of the face to the outer hole rows. On a 2" face this puts
#: the outer rows an inch apart with a third row down the centreline.
ROW_INSET = 0.5

#: A face narrower than this gets a single centred row. Three rows need 2 * ROW_INSET of
#: face plus material outside them; on a 1" face the outer two would sit on the corner
#: radii of the extrusion, where a drill wanders and a bolt head has nothing flat to sit
#: on - so a 1" face gets one hole per column, down the centre.
MULTI_ROW_MIN_WIDTH = 1.5

#: Material left along each long edge of the face when lightening. The corner radius of
#: the extrusion lives here, and it is the stiffest part of the section - cutting into it
#: costs far more strength than the weight is worth.
LIGHTENING_EDGE_MARGIN = 0.25

#: Minimum distance from the machined end of the tube to the CENTRE of the nearest hole.
#: Holes closer than this to a cut end tear out rather than cut cleanly.
MIN_END_MARGIN = 0.375

#: Material left between a hole edge and a pocket edge, and between adjacent pockets.
#: This is the truss web - the part actually carrying load - so it is a floor, not a
#: target: pockets shrink to preserve it and are dropped entirely if they cannot.
MIN_WEB = 0.125

#: Y-extent of one truss cell (pocket + following web). Four hole pitches, so the truss
#: stays in step with the hole grid however long the tube is.
#:
#: The band across the face is fixed - it is whatever the two hole rows leave once MIN_WEB
#: is kept clear of each - so this is the only dimension a bigger pocket can grow in. The
#: trade is real: a longer cell removes more material, and it also lays the diagonal web
#: down flatter, which is the direction that carries truss load less well. Two inches is
#: about as long as the triangle stays a sensible shape on a 2x1.
TRUSS_CELL = 2.0

#: Clearance the tool needs INSIDE a pocket beyond its own radius. A pocket whose
#: inscribed circle only just equals the cutter cannot be cleared - the toolpath
#: degenerates to a point or vanishes when offset inward - so such pockets are dropped
#: with a warning rather than emitted as geometry the post-processor will silently skip.
POCKET_TOOL_CLEARANCE = 0.01


def hole_rows(face_width):
    """X positions of the hole rows across a face of this width.

    A 2" face carries three holes per column - the two outer rows an inch apart, plus one
    on the centreline. A 1" face carries one, centred.
    """
    if face_width >= MULTI_ROW_MIN_WIDTH:
        return [ROW_INSET, face_width / 2.0, face_width - ROW_INSET]
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


def truss_pockets(face_width, tube_length, tool_diameter, web=MIN_WEB, cell=TRUSS_CELL,
                  edge_margin=LIGHTENING_EDGE_MARGIN, end_margin=MIN_END_MARGIN):
    """Right-triangle lightening pockets down the face, as a truss.

    A lightening pattern carries no holes, so the pockets get the whole face rather than
    a narrow band between hole rows: everything inside `edge_margin` of the long edges,
    and inside `end_margin` of the ends. That is what makes the triangles worth cutting -
    on a 2" face the band is 1.5" wide instead of the half inch left between hole rows.

    Each cell holds one right triangle and the right angle alternates between the low-X
    and high-X side from cell to cell, so the material left between them runs diagonally:
    a zigzag web carrying load in tension and compression rather than a straight rail
    carrying it in bending.

    Returns (pockets, warnings).
    """
    warnings = []
    band_lo = edge_margin
    band_hi = face_width - edge_margin
    band = band_hi - band_lo
    if band <= 0:
        warnings.append(
            f'A {face_width:.3f}" face is too narrow to lighten once {edge_margin:.3f}" '
            f'is left along each edge.')
        return [], warnings

    run_lo = end_margin
    run_hi = tube_length - end_margin
    run = run_hi - run_lo
    cells = int(math.floor(run / cell)) if run > 0 else 0
    if cells < 1:
        warnings.append(
            f'A {tube_length:.3f}" tube is too short for a lightening triangle.')
        return [], warnings

    # Centre the truss on the tube, for the same reason a hole run is centred.
    truss_span = cells * cell
    origin = run_lo + (run - truss_span) / 2.0

    leg_y = cell - web              # the web is the gap to the next cell
    inradius = _triangle_inradius(band, leg_y)
    needed = tool_diameter / 2.0 + POCKET_TOOL_CLEARANCE
    if inradius < needed:
        warnings.append(
            f'Lightening pockets skipped: a {tool_diameter:.3f}" tool does not fit the '
            f'{band:.3f}" x {leg_y:.3f}" triangle (needs {needed * 2:.3f}" of room, '
            f'the triangle holds {inradius * 2:.3f}"). Use a smaller tool.')
        return [], warnings

    pockets = []
    for i in range(cells):
        y0 = origin + i * cell + web / 2.0
        y1 = y0 + leg_y
        if i % 2 == 0:
            tri = [(band_lo, y0), (band_hi, y0), (band_lo, y1)]
        else:
            tri = [(band_lo, y0), (band_hi, y0), (band_hi, y1)]
        tri.append(tri[0])           # closed ring, as the pocket machining expects
        pockets.append(tri)

    # Separation is guaranteed by the arithmetic above - each triangle's base is
    # horizontal, so cell i ends a full `web` before cell i+1 starts, at every X. Checked
    # anyway rather than trusted: two overlapping pockets would be cut twice, and the
    # second pass would climb into air where the first already removed the wall.
    overlap = _first_overlap(pockets)
    if overlap is not None:
        warnings.append(
            f'Lightening pockets skipped: triangles {overlap[0]} and {overlap[1]} overlap. '
            f'This is a bug in the pattern geometry, not something you did - please report '
            f'the tube size and length.')
        return [], warnings
    return pockets, warnings


def _first_overlap(rings):
    """Indices of the first pair of rings that share area, or None. Touching along an
    edge is not overlapping - only shared AREA means the same metal gets cut twice."""
    from shapely.geometry import Polygon
    polys = [Polygon(r) for r in rings]
    for i, a in enumerate(polys):
        for j, b in enumerate(polys[i + 1:], i + 1):
            if a.intersects(b) and a.intersection(b).area > 1e-12:
                return (i, j)
    return None


#: The two things a tube pattern can be. They are mutually exclusive on purpose: a face
#: drilled on half-inch centres has no room left to lighten, and a face cut away by a
#: truss has nothing solid left to bolt through. Mixing them would produce a pattern that
#: does neither job well, and - because holes want a twist drill and pockets want an end
#: mill - would need a tool change mid-face to produce it.
MODES = ('holes', 'lightening')


def generate(face_width, tube_length, tool_diameter, mode='holes',
             hole_diameter=HOLE_DIAMETER, spacing=HOLE_SPACING):
    """Build a standard tube pattern.

    mode='holes'      - mounting holes only, drilled. Three per column on a 2" face, one
                        on a 1" face. No pockets.
    mode='lightening' - truss triangles only, milled. No holes.

    Returns a dict with:
        circles   - [{'center': (x, y), 'radius': r, 'diameter': d}], for classify_holes
        pockets   - [[(x, y), ...]] closed rings
        warnings  - human-readable notes; not errors, the pattern is still usable
        rows/hole_ys - the hole grid, for callers that want to describe it
    """
    if mode not in MODES:
        raise ValueError(f'mode must be one of {MODES}, got {mode!r}')
    if tube_length <= 0:
        raise ValueError('Tube length must be positive')
    if face_width <= 0:
        raise ValueError('Face width must be positive')

    warnings = []
    circles = []
    pocket_rings = []
    rows = []
    ys = []

    if mode == 'holes':
        rows = hole_rows(face_width)
        ys = hole_run(tube_length, spacing=spacing)
        if not ys:
            warnings.append(
                f'A {tube_length:.3f}" tube is too short to hold a hole at least '
                f'{MIN_END_MARGIN:.3f}" from both ends; no holes generated.')
        hole_r = hole_diameter / 2.0
        for x in rows:
            for y in ys:
                circles.append({'center': (x, y), 'radius': hole_r,
                                'diameter': hole_diameter})
    else:
        pocket_rings, pocket_warnings = truss_pockets(
            face_width, tube_length, tool_diameter)
        warnings.extend(pocket_warnings)

    return {'circles': circles, 'pockets': pocket_rings, 'warnings': warnings,
            'rows': rows, 'hole_ys': ys, 'mode': mode}
