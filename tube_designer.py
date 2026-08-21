"""Custom tube faces: turn a browser-authored feature list into machinable geometry.

`tube_patterns.py` generates the two FIXED patterns - a drilled mounting-hole grid, or a
truss. This module is the third path: the operator places features themselves (a hole
here, a run of them there, a bearing bore, a lightening pocket) and this turns that
design into the SAME shapes a DXF tube face produces - circles and closed rings.

The one architectural decision worth stating, because everything else follows from it:

    A CUSTOM DESIGN IS MACHINED EXACTLY LIKE A DXF TUBE FACE, WITH ONE END MILL.

It is emphatically NOT an extension of the drilled (`mode='holes'`) path. A design
mixing 0.1695", 0.2656" and 1.125" holes cannot be drilled with one bit and the tube
program has no tool change; the DXF tube path already machines arbitrary mixed circles
and pocket rings with a single end mill (`classify_holes` picks peck-plunge+spiral for
small holes and helical entry for large ones, `_generate_pocket_gcode` clears pockets,
and `generate_tube_pattern_gcode` mirrors face 2). A 1.125" bearing bore is just a large
hole to that code. So the whole job here is: expand the feature list, validate it hard,
and hand back circles and rings. No new toolpath generation exists in this file, and
none should be added to it.

Coordinates are the tube frame `tube_patterns` and the Layout canvas already use:

    X  across the machined face, 0 at the face's left edge
    Y  along the tube, 0 at the machined end, increasing into the tube

Every feature's x/y is its CENTRE. Face 2 is the mirror of face 1 (the pipeline mirrors
it automatically), so a design is authored once; distinct per-face designs are not v1.

Named sizes come from `drill_sizes.TAP_DRILLS` - the clearance column of the table the
rest of the app already uses. There is deliberately no second size table here: the one
time a constant was duplicated for the browser's benefit, the copy went stale and the
note disagreed with the program. The only sizes added are bearing bores, which are not
fastener clearances and are not in that table.
"""

import math
from typing import Dict, List, Optional, Sequence

import drill_sizes
from dxf_geometry import CHORD_TOLERANCE
from tube_patterns import (DEFAULT_HELIX_RADIUS_MULTIPLIER, LIGHTENING_EDGE_MARGIN,
                           MIN_END_MARGIN, MIN_WEB, POCKET_TOOL_CLEARANCE)

#: Bearing bores an FRC team actually cuts into a tube. 1.125" is the OD of the standard
#: flanged bearing for 1/2" hex shaft, which is the one this was asked for. A bore is not
#: a fastener clearance, so it is not in TAP_DRILLS and gets its own (tiny) registry.
BEARING_BORES = {'flanged-hex-bearing': 1.125}   # FRC 0.5in hex bearing OD

#: Which bore a {'type': 'bearing'} feature means when it does not say.
DEFAULT_BEARING = 'flanged-hex-bearing'

#: The grid the editor snaps to. Half the minimum web, so two snapped features can never
#: land closer together than a check below would refuse for a reason the user cannot see.
GRID_SNAP = MIN_WEB / 2.0

#: What a design may contain. Caps, not opinions: the resolver is O(n^2) in resolved
#: holes for the spacing check, and a runaway `count` on a hole run is one typo away.
MAX_FEATURES = 200
MAX_HOLES = 500

#: The features the editor can place. `hole-run` and `hole-array` are conveniences that
#: expand to plain holes; nothing downstream ever sees them.
FEATURE_TYPES = ('hole', 'bearing', 'hole-run', 'hole-array', 'pocket')

#: Matches FRCPostProcessor.hole_size_tolerance for inch jobs. Callers pass their own.
DEFAULT_HOLE_SIZE_TOLERANCE = 0.002

#: Cap on a single run/array dimension, so `count: 1e9` is refused as a number rather
#: than expanded into a hang.
MAX_RUN_COUNT = 500


def clearance_sizes() -> List[Dict[str, object]]:
    """The named hole sizes the editor offers, smallest first.

    Straight off drill_sizes.TAP_DRILLS' clearance column - the diameter a fastener of
    that thread passes through. Built here rather than in the browser so a stale client
    cannot ship a number the server would not have chosen.
    """
    items = []
    for name, spec in drill_sizes.TAP_DRILLS.items():
        items.append({'id': name,
                      'label': f'{name} clearance',
                      'diameter': round(float(spec['clearance']), 4),
                      'kind': 'clearance'})
    items.sort(key=lambda item: item['diameter'])
    return items


def bearing_sizes() -> List[Dict[str, object]]:
    """The named bearing bores the editor offers."""
    return [{'id': name, 'label': f'{name.replace("-", " ")} bore', 'diameter': d,
             'kind': 'bearing'}
            for name, d in sorted(BEARING_BORES.items(), key=lambda kv: kv[1])]


def size_menu() -> List[Dict[str, object]]:
    """Everything the size dropdown offers, in one list: clearances then bores.

    "Custom diameter" is not in here - it is the absence of a named size, expressed by
    the feature carrying `diameter` instead of `size`.
    """
    return clearance_sizes() + bearing_sizes()


def named_diameter(name: str) -> Optional[float]:
    """Diameter for a named size, or None if the name is not one we know.

    None rather than a guess. `_parse_tube_size` answering "1x1" for anything it did not
    recognise turned a typo into a job on the wrong tube with no error anywhere, and that
    is the cautionary tale this whole module is validated against.
    """
    if name in drill_sizes.TAP_DRILLS:
        return float(drill_sizes.TAP_DRILLS[name]['clearance'])
    if name in BEARING_BORES:
        return float(BEARING_BORES[name])
    return None


def _finite(value) -> bool:
    """True for a real, finite number. Written this way - and used in `not _finite(x)`
    guards - because NaN compares False against everything, so `x <= 0` let NaN straight
    through and it came out the far end as `Xnan` in the G-code."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(v) or math.isinf(v))


def _positive(value) -> bool:
    return _finite(value) and float(value) > 0


def rounded_rect(cx: float, cy: float, w: float, h: float, corner_radius: float = 0.0,
                 chord_tolerance: float = CHORD_TOLERANCE) -> List[tuple]:
    """A closed, counter-clockwise ring for a rounded rectangle centred on (cx, cy).

    The corners are real arcs sampled to `chord_tolerance`, the same fidelity the DXF
    path flattens arcs to, so a pocket authored here and one drawn in CAD reach the
    post-processor as the same kind of geometry.
    """
    r = max(0.0, min(corner_radius, w / 2.0, h / 2.0))
    x0, x1 = cx - w / 2.0, cx + w / 2.0
    y0, y1 = cy - h / 2.0, cy + h / 2.0
    if r <= 1e-9:
        ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(round(x, 6), round(y, 6)) for x, y in ring + [ring[0]]]

    # Segments per quarter turn from the chord tolerance: sagitta = r(1 - cos(dtheta/2)).
    ratio = 1.0 - min(chord_tolerance / r, 1.0)
    dtheta = 2.0 * math.acos(max(-1.0, min(1.0, ratio)))
    per_corner = max(4, int(math.ceil((math.pi / 2.0) / dtheta))) if dtheta > 0 else 16

    corners = (((x1 - r, y0 + r), -math.pi / 2.0),      # bottom-right
               ((x1 - r, y1 - r), 0.0),                 # top-right
               ((x0 + r, y1 - r), math.pi / 2.0),       # top-left
               ((x0 + r, y0 + r), math.pi))             # bottom-left
    pts = []
    for (ax, ay), start in corners:
        for i in range(per_corner + 1):
            a = start + (math.pi / 2.0) * (i / float(per_corner))
            pts.append((ax + r * math.cos(a), ay + r * math.sin(a)))
    pts.append(pts[0])
    return [(round(x, 6), round(y, 6)) for x, y in pts]


def _pocket_ring(feature) -> List[tuple]:
    return rounded_rect(float(feature['x']), float(feature['y']),
                        float(feature['w']), float(feature['h']),
                        float(feature.get('corner_radius', 0.0) or 0.0))


def _circle(x, y, diameter, source):
    return {'center': (round(float(x), 6), round(float(y), 6)),
            'radius': round(float(diameter) / 2.0, 6),
            'diameter': round(float(diameter), 6),
            'source': source}


def _resolve_diameter(feature, index, errors) -> Optional[float]:
    """The diameter a hole-ish feature means, named size winning over a raw number.

    Named wins and is looked up HERE, server-side, so a client holding a stale copy of
    the table cannot ship a wrong number - it can only ship a wrong name, which is
    refused by name.
    """
    name = feature.get('size')
    diameter = feature.get('diameter')
    if name:
        d = named_diameter(str(name))
        if d is None:
            errors.append(f'Feature {index + 1}: unknown hole size {str(name)!r}. '
                          f'Choose one of: {", ".join(s["id"] for s in size_menu())}.')
            return None
        return d
    if not _positive(diameter):
        errors.append(f'Feature {index + 1}: needs a named size or a positive '
                      f'diameter, got {diameter!r}.')
        return None
    return float(diameter)


def _expand(feature, index, errors) -> Dict[str, List]:
    """One feature -> the circles and rings it stands for. Errors are appended, never
    raised: a design is validated as a whole so the editor can mark every bad feature at
    once instead of one per round trip."""
    out = {'circles': [], 'pockets': []}
    ftype = feature.get('type')
    if ftype not in FEATURE_TYPES:
        errors.append(f'Feature {index + 1}: unknown feature type {ftype!r}. '
                      f'Expected one of: {", ".join(FEATURE_TYPES)}.')
        return out

    x, y = feature.get('x'), feature.get('y')
    if not _finite(x) or not _finite(y):
        errors.append(f'Feature {index + 1} ({ftype}): position must be two finite '
                      f'numbers, got x={x!r}, y={y!r}.')
        return out
    x, y = float(x), float(y)

    if ftype == 'bearing':
        name = str(feature.get('bearing') or DEFAULT_BEARING)
        if name not in BEARING_BORES:
            errors.append(f'Feature {index + 1}: unknown bearing {name!r}. '
                          f'Expected one of: {", ".join(sorted(BEARING_BORES))}.')
            return out
        out['circles'].append(_circle(x, y, BEARING_BORES[name], index))
        return out

    if ftype == 'hole':
        d = _resolve_diameter(feature, index, errors)
        if d is not None:
            out['circles'].append(_circle(x, y, d, index))
        return out

    if ftype == 'hole-run':
        d = _resolve_diameter(feature, index, errors)
        pitch = feature.get('pitch')
        count = feature.get('count')
        axis = str(feature.get('axis', 'y')).lower()
        if axis not in ('x', 'y'):
            errors.append(f'Feature {index + 1} (hole run): axis must be "x" or "y", '
                          f'got {feature.get("axis")!r}.')
            return out
        if not _positive(pitch):
            errors.append(f'Feature {index + 1} (hole run): pitch must be a positive '
                          f'finite number, got {pitch!r}.')
            return out
        n = _whole_count(count, index, 'hole run', 'count', errors)
        if n is None or d is None:
            return out
        for i in range(n):
            step = float(pitch) * i
            out['circles'].append(_circle(x + step if axis == 'x' else x,
                                          y if axis == 'x' else y + step, d, index))
        return out

    if ftype == 'hole-array':
        d = _resolve_diameter(feature, index, errors)
        px, py = feature.get('pitch_x'), feature.get('pitch_y')
        cols = _whole_count(feature.get('cols'), index, 'hole array', 'cols', errors)
        rows = _whole_count(feature.get('rows'), index, 'hole array', 'rows', errors)
        if cols is not None and cols > 1 and not _positive(px):
            errors.append(f'Feature {index + 1} (hole array): pitch_x must be a positive '
                          f'finite number, got {px!r}.')
            return out
        if rows is not None and rows > 1 and not _positive(py):
            errors.append(f'Feature {index + 1} (hole array): pitch_y must be a positive '
                          f'finite number, got {py!r}.')
            return out
        if d is None or cols is None or rows is None:
            return out
        for c in range(cols):
            for r in range(rows):
                out['circles'].append(
                    _circle(x + float(px or 0.0) * c, y + float(py or 0.0) * r, d, index))
        return out

    # pocket
    w, h = feature.get('w'), feature.get('h')
    radius = feature.get('corner_radius', 0.0) or 0.0
    if not _positive(w) or not _positive(h):
        errors.append(f'Feature {index + 1} (pocket): width and height must be positive '
                      f'finite numbers, got w={w!r}, h={h!r}.')
        return out
    if not _finite(radius) or float(radius) < 0:
        errors.append(f'Feature {index + 1} (pocket): corner radius must be a finite '
                      f'number of zero or more, got {radius!r}.')
        return out
    if float(radius) > min(float(w), float(h)) / 2.0 + 1e-9:
        errors.append(f'Feature {index + 1} (pocket): a {float(radius):.3f}" corner '
                      f'radius does not fit a {float(w):.3f}" x {float(h):.3f}" pocket; '
                      f'the largest that does is '
                      f'{min(float(w), float(h)) / 2.0:.3f}".')
        return out
    out['pockets'].append(_pocket_ring(feature))
    return out


def _whole_count(value, index, what, field, errors) -> Optional[int]:
    if not _finite(value) or float(value) != int(float(value)):
        errors.append(f'Feature {index + 1} ({what}): {field} must be a whole number, '
                      f'got {value!r}.')
        return None
    n = int(float(value))
    if n < 1 or n > MAX_RUN_COUNT:
        errors.append(f'Feature {index + 1} ({what}): {field} must be between 1 and '
                      f'{MAX_RUN_COUNT}, got {n}.')
        return None
    return n


def _shape_bounds(circles, pockets):
    """(minx, miny, maxx, maxy) over a feature's resolved geometry, or None."""
    xs, ys = [], []
    for c in circles:
        xs.extend([c['center'][0] - c['radius'], c['center'][0] + c['radius']])
        ys.extend([c['center'][1] - c['radius'], c['center'][1] + c['radius']])
    for ring in pockets:
        xs.extend([p[0] for p in ring])
        ys.extend([p[1] for p in ring])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _fits_the_face(index, ftype, circles, pockets, face_width, tube_length, errors):
    """Every feature stays clear of the extrusion's corners and of both cut ends.

    Two different constants, each used for what it means:

    * LIGHTENING_EDGE_MARGIN along the LONG edges, measured to the feature's EDGE. The
      corner radius of the extrusion lives there and is the stiffest part of the
      section - this is the same margin the truss keeps.
    * MIN_END_MARGIN at the ENDS, measured to the feature's CENTRE (that is what the
      constant is defined as: a hole closer than this to a cut end tears out), PLUS
      MIN_WEB to its EDGE, so a 1.125" bore cannot hang off the end of the tube while
      its centre sits happily inside the margin.
    """
    bounds = _shape_bounds(circles, pockets)
    if bounds is None:
        return
    minx, miny, maxx, maxy = bounds
    if minx < LIGHTENING_EDGE_MARGIN - 1e-9 or maxx > face_width - LIGHTENING_EDGE_MARGIN + 1e-9:
        errors.append(
            f'Feature {index + 1} ({ftype}) reaches X {minx:.3f}" to {maxx:.3f}", '
            f'outside the {LIGHTENING_EDGE_MARGIN:.3f}" that must be left along each '
            f'long edge of a {face_width:.3f}" face.')
    if miny < MIN_WEB - 1e-9 or maxy > tube_length - MIN_WEB + 1e-9:
        errors.append(
            f'Feature {index + 1} ({ftype}) reaches Y {miny:.3f}" to {maxy:.3f}", '
            f'off the ends of a {tube_length:.3f}" tube (keep {MIN_WEB:.3f}" of metal '
            f'at each end).')
        return
    centres_y = [c['center'][1] for c in circles]
    for ring in pockets:
        centres_y.append(sum(p[1] for p in ring[:-1]) / max(1, len(ring[:-1])))
    for cy in centres_y:
        if cy < MIN_END_MARGIN - 1e-9 or cy > tube_length - MIN_END_MARGIN + 1e-9:
            errors.append(
                f'Feature {index + 1} ({ftype}) is centred {min(cy, tube_length - cy):.3f}" '
                f'from a cut end; keep it at least {MIN_END_MARGIN:.3f}" away or it '
                f'tears out rather than cutting cleanly.')
            return


def _check_hole_size(index, circle, tool_diameter, hole_size_tolerance, errors):
    """A hole narrower than the cutter cannot be made by it.

    classify_holes refuses these too, but it does so with the hole's coordinates and no
    idea which feature they came from - and it does so AFTER the pattern reports itself
    as loaded. Refused here, early, naming the feature the operator can actually click.
    """
    d = circle['diameter']
    if d < tool_diameter - hole_size_tolerance:
        errors.append(
            f'Feature {index + 1}: a {d:.4f}" hole is smaller than the {tool_diameter:.4f}" '
            f'cutter, which cannot make it. Use a smaller tool, or a larger hole.')
        return False
    return True


def _check_pocket_clearable(index, ring, tool_diameter, helix_radius_multiplier, errors):
    """Can the tool get INTO this pocket, and is there anything left once it has?

    Two tests, because each has passed something the other caught. The inscribed circle
    has to hold what the cutter SWEEPS on the way in - it helixes down around the
    centroid at tool_radius * (1 + helix_radius_multiplier), and sizing against the bare
    radius let the entry spill straight through the edge margin. And the inradius test
    alone passed pockets whose inward offset then vanished, so the job reported success
    and cut nothing.
    """
    from shapely.geometry import Polygon
    poly = Polygon(ring)
    minx, miny, maxx, maxy = poly.bounds
    inradius = min(maxx - minx, maxy - miny) / 2.0
    sweep = (tool_diameter / 2.0) * (1.0 + helix_radius_multiplier)
    needed = sweep + POCKET_TOOL_CLEARANCE
    if inradius < needed:
        errors.append(
            f'Feature {index + 1} (pocket): a {tool_diameter:.3f}" tool needs '
            f'{needed * 2:.3f}" of room to helix in, and this pocket holds '
            f'{inradius * 2:.3f}". Make it bigger, or use a smaller tool.')
        return False
    if poly.buffer(-tool_diameter / 2.0).area <= 0.001:
        errors.append(
            f'Feature {index + 1} (pocket): a {tool_diameter:.3f}" tool leaves nothing '
            f'to clear inside it. Make it bigger, or use a smaller tool.')
        return False
    return True


def _check_corner_radius(index, feature, tool_diameter, errors):
    """An end mill cannot cut an inside corner sharper than its own radius."""
    radius = float(feature.get('corner_radius', 0.0) or 0.0)
    tool_radius = tool_diameter / 2.0
    if radius < tool_radius - 1e-9:
        errors.append(
            f'Feature {index + 1} (pocket): a {tool_diameter:.3f}" end mill cannot cut a '
            f'{radius:.3f}" inside corner. The corner radius must be at least '
            f'{tool_radius:.4f}", which is what the tool will leave anyway.')
        return False
    return True


def _check_spacing(entries, errors):
    """MIN_WEB of metal between every pair of feature edges, across the whole design.

    All pairs, deliberately: two features that overlap would have the same metal cut
    twice, and the second pass would climb into air where the first already removed the
    wall. Circle-to-circle is analytic (500 holes is 125k pairs and shapely would be far
    too slow for a live editor); anything involving a pocket goes through shapely, and
    there are only ever a handful of those.
    """
    from shapely.geometry import Polygon

    shapes = []          # (index, kind, geometry-ish)
    for entry in entries:
        for c in entry['circles']:
            shapes.append((entry['index'], entry['type'], 'circle', c))
        for ring in entry['pockets']:
            shapes.append((entry['index'], entry['type'], 'pocket', Polygon(ring)))

    reported = set()
    for i in range(len(shapes)):
        ia, ta, ka, ga = shapes[i]
        for j in range(i + 1, len(shapes)):
            ib, tb, kb, gb = shapes[j]
            if ka == 'circle' and kb == 'circle':
                gap = (math.hypot(ga['center'][0] - gb['center'][0],
                                  ga['center'][1] - gb['center'][1])
                       - ga['radius'] - gb['radius'])
            else:
                a = (Polygon(_circle_ring(ga)) if ka == 'circle' else ga)
                b = (Polygon(_circle_ring(gb)) if kb == 'circle' else gb)
                gap = 0.0 if a.intersects(b) else a.distance(b)
                if a.intersects(b):
                    gap = -1.0
            if gap < MIN_WEB - 1e-6:
                key = tuple(sorted((ia, ib)))
                if key in reported:
                    continue
                reported.add(key)
                if ia == ib:
                    errors.append(
                        f'Feature {ia + 1} ({ta}) puts its own holes {max(gap, 0):.3f}" '
                        f'apart; {MIN_WEB:.3f}" of metal must be left between them. '
                        f'Increase the pitch or use a smaller size.')
                else:
                    errors.append(
                        f'Features {ia + 1} ({ta}) and {ib + 1} ({tb}) leave '
                        f'{max(gap, 0):.3f}" of metal between them; {MIN_WEB:.3f}" is '
                        f'the minimum. Move them apart.')


def _circle_ring(circle, segments=48):
    cx, cy = circle['center']
    r = circle['radius']
    return [(cx + r * math.cos(2 * math.pi * i / segments),
             cy + r * math.sin(2 * math.pi * i / segments))
            for i in range(segments)]


def resolve(design: dict, face_width: float, tube_length: float, tool_diameter: float,
            helix_radius_multiplier: float = DEFAULT_HELIX_RADIUS_MULTIPLIER,
            hole_size_tolerance: float = DEFAULT_HOLE_SIZE_TOLERANCE) -> dict:
    """Expand and validate a design document.

    Returns
        {'circles': [...],        # ready for FRCPostProcessor.classify_holes()
         'pockets': [ring, ...],  # closed rings, ready for _sort_pockets()
         'features': [...],       # per-feature geometry + verdict, for the editor
         'warnings': [...],       # advice; the design is still machinable
         'errors': [...]}         # refusals; nothing may be generated while any exist

    Errors REFUSE rather than silently mis-machining, which is this project's standing
    policy for tube work. They are collected rather than raised so the editor can mark
    every offending feature at once.

    The machine envelope is NOT checked here - `resolve` knows nothing about the machine.
    The tube's face width and length are checked against it by the /process route, which
    already did exactly that for the fixed patterns.
    """
    if not _positive(face_width):
        raise ValueError(f'Face width must be a positive finite number, got {face_width!r}')
    if not _positive(tube_length):
        raise ValueError(f'Tube length must be a positive finite number, got {tube_length!r}')
    if not _positive(tool_diameter):
        raise ValueError(f'Tool diameter must be a positive finite number, got {tool_diameter!r}')

    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(design, dict):
        return {'circles': [], 'pockets': [], 'features': [], 'warnings': [],
                'errors': ['A custom design must be a JSON object with a "features" list.']}
    features = design.get('features')
    if features is None:
        features = []
    if not isinstance(features, list):
        return {'circles': [], 'pockets': [], 'features': [], 'warnings': [],
                'errors': ['A custom design\'s "features" must be a list.']}
    if len(features) > MAX_FEATURES:
        return {'circles': [], 'pockets': [], 'features': [], 'warnings': [],
                'errors': [f'A design may hold at most {MAX_FEATURES} features; this one '
                           f'has {len(features)}.']}
    if not features:
        warnings.append('This design has no features yet, so nothing would be cut.')

    entries = []
    for index, feature in enumerate(features):
        feature_errors: List[str] = []
        if not isinstance(feature, dict):
            feature_errors.append(f'Feature {index + 1}: expected an object, got '
                                  f'{type(feature).__name__}.')
            expanded = {'circles': [], 'pockets': []}
        else:
            expanded = _expand(feature, index, feature_errors)

        ftype = (feature.get('type') if isinstance(feature, dict) else None) or 'feature'
        if not feature_errors:
            for circle in expanded['circles']:
                _check_hole_size(index, circle, tool_diameter, hole_size_tolerance,
                                 feature_errors)
            for ring in expanded['pockets']:
                if _check_corner_radius(index, feature, tool_diameter, feature_errors):
                    _check_pocket_clearable(index, ring, tool_diameter,
                                            helix_radius_multiplier, feature_errors)
            _fits_the_face(index, ftype, expanded['circles'], expanded['pockets'],
                           face_width, tube_length, feature_errors)

        entries.append({'index': index, 'type': ftype,
                        'circles': expanded['circles'], 'pockets': expanded['pockets'],
                        'errors': feature_errors, 'ok': not feature_errors})
        errors.extend(feature_errors)

    total_holes = sum(len(e['circles']) for e in entries)
    if total_holes > MAX_HOLES:
        errors.append(f'This design resolves to {total_holes} holes; the limit is '
                      f'{MAX_HOLES}. Reduce a run or array count.')
        return {'circles': [], 'pockets': [], 'features': entries, 'warnings': warnings,
                'errors': errors}

    # Only features that are individually sound take part: a feature already refused for
    # being off the face would otherwise also be reported for crowding its neighbour,
    # which buries the real message under a second one the user cannot act on.
    _check_spacing([e for e in entries if e['ok']], errors)

    circles = [dict(c) for e in entries if e['ok'] for c in e['circles']]
    pockets = [list(ring) for e in entries if e['ok'] for ring in e['pockets']]
    return {'circles': circles, 'pockets': pockets, 'features': entries,
            'warnings': warnings, 'errors': errors}


def describe(resolved: dict) -> str:
    """One line for the editor's note and the program header: what this design cuts."""
    holes = len(resolved.get('circles', []))
    pockets = len(resolved.get('pockets', []))
    if not holes and not pockets:
        return 'nothing yet'
    bits = []
    if holes:
        bits.append(f'{holes} hole' + ('' if holes == 1 else 's'))
    if pockets:
        bits.append(f'{pockets} pocket' + ('' if pockets == 1 else 's'))
    return ', '.join(bits)
