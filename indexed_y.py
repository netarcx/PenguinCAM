"""Two-setup, end-for-end machining for flat parts longer than Y travel."""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Sequence, Tuple

from shapely.geometry import LineString, MultiLineString, GeometryCollection, Polygon, box
from shapely.geometry.polygon import orient

import tooling
from frc_cam_postprocessor import FRCPostProcessor, PostProcessorResult, build_output_filename


def _line_parts(geometry) -> List[List[Tuple[float, float]]]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [list(geometry.coords)] if geometry.length > 1e-5 else []
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        answer = []
        for child in geometry.geoms:
            answer.extend(_line_parts(child))
        return answer
    return []


def _transform_180(lines: Sequence[str], width: float, length: float) -> List[str]:
    """Map original stock coordinates to the same G54 after an in-plane 180 turn."""
    word = re.compile(r'(?<![A-Z])([XYIJ])(-?\d+(?:\.\d*)?)', re.I)
    answer = []
    for line in lines:
        if not line.lstrip().startswith(('G0', 'G1', 'G2', 'G3')):
            answer.append(line)
            continue
        def replace(match):
            axis, raw = match.group(1).upper(), float(match.group(2))
            value = ({'X': width - raw, 'Y': length - raw,
                      'I': -raw, 'J': -raw})[axis]
            return f'{axis}{value:.4f}'
        answer.append(word.sub(replace, line))
    return answer


def _point_at(points, distance):
    line = LineString(points)
    p = line.interpolate(max(0.0, min(distance, line.length)))
    return p.x, p.y


def _trace_with_tabs(pp, points, z, zones, tab_z):
    """Trace an open polyline at depth, lifting over distance-based tab zones."""
    line = LineString(points)
    cuts = {0.0, line.length}
    for start, end in zones:
        cuts.add(max(0.0, start)); cuts.add(min(line.length, end))
    ordered = sorted(cuts)
    out, tab_positions = [], []
    current_z = z
    for start, end in zip(ordered, ordered[1:]):
        mid = (start + end) / 2
        is_tab = any(a - 1e-8 <= mid <= b + 1e-8 for a, b in zones)
        sx, sy = _point_at(points, start)
        ex, ey = _point_at(points, end)
        target_z = tab_z if is_tab and z < tab_z else z
        if abs(current_z - target_z) > 1e-8:
            out.append(f'G1 Z{target_z:.4f} F{pp.plunge_rate:.1f}' +
                       ('  ; Indexed holding tab' if is_tab else '  ; Tab end'))
            current_z = target_z
        out.append(f'G1 X{ex:.4f} Y{ey:.4f} F{pp.feed_rate:.1f}')
        if is_tab:
            tab_positions.append([(sx, sy), (ex, ey)])
    return out, tab_positions


def _open_profile(pp, fragments, with_tabs=False, tab_region_y=None):
    """Cut compensated open profile fragments without inventing a seam wall."""
    total_depth = pp.material_top - pp.cut_depth
    passes = pp.passes_for_depth(total_depth, pp.max_slotting_depth)
    tab_z = min(pp.material_top, pp.cut_depth + pp.tab_height)
    lengths = [LineString(points).length for points in fragments]
    if not lengths:
        return [], []
    tab_centres = []
    if with_tabs:
        eligible = []
        for index, points in enumerate(fragments):
            line = LineString(points)
            region = (line if tab_region_y is None else
                      line.intersection(box(-1e6, tab_region_y, 1e6, 1e6)))
            for piece in _line_parts(region):
                piece_line = LineString(piece)
                a = line.project(piece_line.interpolate(0))
                b = line.project(piece_line.interpolate(piece_line.length))
                start, end = sorted((a, b))
                # Keep a half-tab plus a little cutting room away from the ownership
                # boundary and fragment ends.
                margin = pp.tab_width / 2 + .05
                if end - start > 2 * margin:
                    eligible.append((index, start + margin, end - margin))
        total = sum(end-start for _, start, end in eligible)
        if total < 2 * pp.tab_width + 0.5:
            raise tooling.ToolingError(
                'Setup 2 has too little uncut profile outside the overlap for two safe holding tabs.')
        # Two separated tabs are the minimum that prevents the final fragment pivoting.
        targets = (total / 3.0, 2.0 * total / 3.0)
        for target in targets:
            cursor = 0.0
            for index, start, end in eligible:
                span = end - start
                if cursor <= target <= cursor + span:
                    tab_centres.append((index, start + target - cursor))
                    break
                cursor += span

    lines = ['(===== INDEXED OPEN PROFILE =====)']
    removal = []
    for frag_index, points in enumerate(fragments):
        path = LineString(points)
        if path.length < 0.25:
            continue
        centres = [d for i, d in tab_centres if i == frag_index]
        zones = [(d - pp.tab_width / 2, d + pp.tab_width / 2) for d in centres]
        for pass_no in range(1, passes + 1):
            z = pp.cut_depth if pass_no == passes else pp.material_top - total_depth * pass_no / passes
            ramp_start = pp.material_top + pp.ramp_start_clearance
            ramp_len = (ramp_start - z) / math.tan(math.radians(pp.ramp_angle))
            if path.length < ramp_len + 0.1:
                raise tooling.ToolingError(
                    f'An indexed profile fragment is only {path.length:.3f} in long, '
                    f'but this material needs {ramp_len:.3f} in for a safe ramp. '
                    'Increase the overlap or move the part away from the handoff.')
            ramp_end = _point_at(points, ramp_len)
            start = points[0]
            lines += [f'(Indexed fragment {frag_index + 1}, pass {pass_no}/{passes})',
                      f'G0 Z{pp.retract_height:.4f}  ; Safe before fragment',
                      f'G0 X{start[0]:.4f} Y{start[1]:.4f}',
                      f'G1 Z{ramp_start:.4f} F{pp.approach_rate:.1f}',
                      f'G1 X{ramp_end[0]:.4f} Y{ramp_end[1]:.4f} Z{z:.4f} '
                      f'F{pp.ramp_feed_rate:.1f}  ; Linear ramp in scrap-side kerf']
            # Recut the ramped region at depth, respecting tabs, then re-enter its open
            # kerf and finish the rest. This makes every point reach the requested depth.
            ramp_piece = _substring(points, 0.0, ramp_len)
            traced, tabs = _trace_with_tabs(pp, list(reversed(ramp_piece)), z,
                                            [(ramp_len-b, ramp_len-a) for a, b in zones
                                             if a < ramp_len], tab_z)
            lines.extend(traced)
            lines += [f'G0 Z{pp.retract_height:.4f}',
                      f'G0 X{ramp_end[0]:.4f} Y{ramp_end[1]:.4f}',
                      f'G1 Z{z:.4f} F{pp.approach_rate:.1f}  ; Re-enter open kerf']
            tail = _substring(points, ramp_len, path.length)
            tail_zones = [(max(0, a-ramp_len), b-ramp_len) for a, b in zones if b > ramp_len]
            traced2, tabs2 = _trace_with_tabs(pp, tail, z, tail_zones, tab_z)
            lines.extend(traced2)
            lines.append(f'G0 Z{pp.retract_height:.4f}  ; Retract')
            if pass_no == passes:
                removal.extend(tabs + tabs2)
    return lines, removal


def _open_chamfer(pp, fragments, width, angle):
    depth = pp.chamfer_depth(width, angle)
    if width > pp.chamfer_max_width(pp.tool_diameter, angle) + 1e-9:
        raise tooling.ToolingError('The selected V-bit is too small for the indexed perimeter chamfer.')
    if depth >= pp.material_thickness:
        raise tooling.ToolingError('The indexed perimeter chamfer would cut through the stock.')
    z = pp.material_top - depth
    lines = ['(===== INDEXED OPEN PERIMETER CHAMFER =====)']
    for points in fragments:
        path = LineString(points)
        if path.length < .1:
            continue
        start = points[0]
        ramp = min(path.length / 3, max(.1, depth / math.tan(math.radians(pp.ramp_angle))))
        end = _point_at(points, ramp)
        lines += [f'G0 Z{pp.retract_height:.4f}',
                  f'G0 X{start[0]:.4f} Y{start[1]:.4f}',
                  f'G1 Z{pp.material_top + pp.ramp_start_clearance:.4f} F{pp.approach_rate:.1f}',
                  f'G1 X{end[0]:.4f} Y{end[1]:.4f} Z{z:.4f} F{pp.ramp_feed_rate:.1f}']
        for x, y in reversed(_substring(points, 0, ramp)):
            lines.append(f'G1 X{x:.4f} Y{y:.4f} F{pp.feed_rate:.1f}')
        lines += [f'G0 Z{pp.retract_height:.4f}', f'G0 X{end[0]:.4f} Y{end[1]:.4f}',
                  f'G1 Z{z:.4f} F{pp.approach_rate:.1f}']
        for x, y in _substring(points, ramp, path.length)[1:]:
            lines.append(f'G1 X{x:.4f} Y{y:.4f} F{pp.feed_rate:.1f}')
        lines.append(f'G0 Z{pp.retract_height:.4f}')
    return lines


def _substring(points, start, end):
    line = LineString(points)
    result = [(_point_at(points, start))]
    travelled = 0.0
    for a, b in zip(points, points[1:]):
        travelled += math.hypot(b[0]-a[0], b[1]-a[1])
        if start + 1e-8 < travelled < end - 1e-8:
            result.append(b)
    result.append(_point_at(points, end))
    return result


def _fixture_program(job, tool, _job_pp):
    cfg = job.indexing
    hole = cfg.pin_diameter + cfg.pin_clearance
    if tool.type != 'endmill' or tool.diameter >= hole - 0.005:
        raise tooling.ToolingError(
            f'The fixture needs an end mill smaller than the {hole:.4f} in locator holes.')
    pp = FRCPostProcessor(cfg.pin_depth, tool.diameter, config=job.config,
                          z_datum='stock_top', tool_flutes=tool.flutes)
    pp.apply_material_preset('plywood', job.machine_id)
    feeds, _ = tooling.compute_tool_feeds(tool, 'plywood', job.machine_id, 'holes',
                                           feeds_machine=job.feeds_machine,
                                           config=job.config)
    tooling.apply_tool_feeds(pp, tool, feeds)
    x0, y0, width = cfg.fixture_x, cfg.fixture_y, cfg.stock_width
    points = [(x0 + width * .25, y0 - cfg.pin_diameter / 2),
              (x0 + width * .75, y0 - cfg.pin_diameter / 2),
              (x0 - cfg.pin_diameter / 2,
               y0 + min(6.0, job.config.machine_travel(job.machine_id)[1] / 2))]
    lines = ['(PENGUINCAM OVERSIZED-Y LOCATOR FIXTURE)',
             '(Temporarily set G54 X0 Y0 at the machine-bed lower-left)',
             '(Set G54 Z0 on the top of the spoilboard)',
             '(After this program, set main-job G54 X Y at the L witness corner)',
             'G90 G94 G91.1 G17 G20 G40 G49',
             'G54', f'S{pp.spindle_speed}', 'M3', 'G4 P3']
    radius = (hole - tool.diameter) / 2
    step = min(pp.max_slotting_depth, cfg.pin_depth)
    passes = max(1, math.ceil(cfg.pin_depth / step))
    for i, (x, y) in enumerate(points, 1):
        lines += [f'(Locator P{i})', f'G0 Z{pp.retract_height:.4f}',
                  f'G0 X{x+radius:.4f} Y{y:.4f}',
                  f'G1 Z0.0500 F{pp.plunge_rate:.1f}  ; Approach spoilboard']
        prior_z = 0.05
        for p in range(1, passes + 1):
            z = -cfg.pin_depth * p / passes
            per_loop = max(1e-6, 2*math.pi*radius*math.tan(math.radians(pp.ramp_angle)))
            loops = max(1, math.ceil((prior_z-z)/per_loop))
            for loop in range(1, loops+1):
                loop_z = prior_z + (z-prior_z)*loop/loops
                lines.append(f'G3 X{x+radius:.4f} Y{y:.4f} I{-radius:.4f} J0 '
                             f'Z{loop_z:.4f} F{pp.ramp_feed_rate:.1f}')
            lines.append(f'G3 X{x+radius:.4f} Y{y:.4f} I{-radius:.4f} J0 '
                         f'F{pp.feed_rate:.1f}')
            prior_z = z
    mark = 1.0
    lines += ['(Shallow L witness for the stock datum)', f'G0 Z{pp.retract_height:.4f}',
              f'G0 X{x0:.4f} Y{y0+mark:.4f}',
              f'G1 Z{-cfg.witness_depth:.4f} F{pp.plunge_rate:.1f}',
              f'G1 X{x0:.4f} Y{y0:.4f} F{pp.feed_rate:.1f}',
              f'G1 X{x0+mark:.4f} Y{y0:.4f}', f'G0 Z{pp.retract_height:.4f}',
              'M5', 'M30']
    return '\n'.join(lines) + '\n'


def generate_indexed_y_job(job, timestamp=None, suggested_filename=None):
    cfg = job.indexing
    machine_x, machine_y = job.config.machine_travel(job.machine_id)
    widest = max(t.diameter for t in job.used_tools)
    overlap = 2 * machine_y - cfg.stock_length
    required = max(1.0, 4 * widest)
    errors = []
    if cfg.fixture_x + cfg.stock_width > machine_x + 1e-6:
        errors.append(f'Stock at fixture X{cfg.fixture_x:.3f} reaches '
                      f'X{cfg.fixture_x+cfg.stock_width:.3f}, past {machine_x:.2f} in X travel.')
    if cfg.fixture_x < cfg.pin_diameter + .05 or cfg.fixture_y < cfg.pin_diameter + .05:
        errors.append('Leave the pin diameter plus 0.05 in outside the stock for locator holes.')
    if cfg.stock_length <= machine_y + 1e-6:
        errors.append('Two-setup mode is only for stock longer than the machine Y travel.')
    if overlap < required - 1e-6:
        errors.append(f'Stock length leaves only {overlap:.2f} in overlap; this job needs '
                      f'at least {required:.2f} in. Maximum length is {2*machine_y-required:.2f} in.')
    part = job.parts[0]
    survey = tooling.survey_part(job, part)
    errors.extend(survey['errors'])
    errors.extend(tooling._validate_feature_coverage(part, survey)[0])
    errors.extend(tooling._validate_profile_order(job))
    if errors:
        return PostProcessorResult(success=False, errors=errors)
    if not any(op.op_type == 'perimeter' for op in part.operations):
        return PostProcessorResult(success=False,
                                   errors=['An oversized part needs a perimeter operation.'])
    if not job.config.tabs_enabled:
        return PostProcessorResult(success=False,
                                   errors=['Two-setup profiling requires holding tabs in Setup 2.'])

    windows = [(0.0, machine_y), (cfg.stock_length-machine_y, cfg.stock_length)]
    handoff = cfg.stock_length / 2
    owner = [{'holes': set(), 'pockets': set()}, {'holes': set(), 'pockets': set()}]
    geometry_pp = tooling.build_part_postprocessor(job, part, widest)
    geometry_pp.classify_holes(reject_undersized=False)
    placed_bounds = geometry_pp.placed_polygon().bounds
    pad = widest / 2
    if (placed_bounds[0] - pad < -1e-6 or placed_bounds[1] - pad < -1e-6
            or placed_bounds[2] + pad > cfg.stock_width + 1e-6
            or placed_bounds[3] + pad > cfg.stock_length + 1e-6):
        return PostProcessorResult(success=False, errors=[
            'The part and its widest cutter path must stay inside the exact long stock.'])
    for feature in survey['holes']:
        radius = feature['diameter']/2 + widest/2
        bounds = (feature['y']-radius, feature['y']+radius)
        choices = [i for i, w in enumerate(windows) if bounds[0] >= w[0] and bounds[1] <= w[1]]
        if not choices:
            errors.append(f'Hole at Y{feature["y"]:.3f} cannot fit wholly in either setup.')
        else:
            pick = min(choices, key=lambda i: abs(feature['y'] - (machine_y/2 if i == 0 else cfg.stock_length-machine_y/2)))
            owner[pick]['holes'].add(feature['key'])
    pocket_by_key = {tooling.pocket_key(p): Polygon(p).bounds for p in geometry_pp.pockets or []}
    for feature in survey['pockets']:
        b = pocket_by_key[feature['key']]
        bounds = (b[1]-widest/2, b[3]+widest/2)
        choices = [i for i, w in enumerate(windows) if bounds[0] >= w[0] and bounds[1] <= w[1]]
        if not choices:
            errors.append(f'Pocket at Y{feature["y"]:.3f} cannot fit wholly in either setup.')
        else:
            pick = min(choices, key=lambda i: abs(feature['y'] - (machine_y/2 if i == 0 else cfg.stock_length-machine_y/2)))
            owner[pick]['pockets'].add(feature['key'])
    if errors:
        return PostProcessorResult(success=False, errors=errors)

    blend = min(overlap/2, max(.25, 2*widest))
    bodies_by_setup = [[], []]
    warnings = []
    profile_op = next(op for op in part.operations if op.op_type == 'perimeter')
    profile_body = tooling.generate_operation(job, part, profile_op, survey, defer_tabs=True)
    if profile_body['errors']:
        return PostProcessorResult(success=False, errors=profile_body['errors'])
    pp = profile_body['pp']
    compensated = orient(Polygon(pp.perimeter).buffer(pp.tool_radius), 1.0)
    ring = LineString(list(compensated.exterior.coords)[::-1])
    regions = [box(-1e6, -1e6, 1e6, handoff+blend),
               box(-1e6, handoff-blend, 1e6, 1e6)]
    fragments = [_line_parts(ring.intersection(region)) for region in regions]
    setup2_removal = None
    for setup in (0, 1):
        for op in part.operations:
            if op.op_type == 'perimeter':
                indexed_lines, tab_positions = _open_profile(
                    pp, fragments[setup], with_tabs=(setup == 1),
                    tab_region_y=(handoff + blend if setup == 1 else None))
                body = dict(profile_body, lines=indexed_lines, deferred=None,
                            op=tooling.Operation('perimeter', profile_op.tool_slot,
                                                 f'{profile_op.label} - Setup {setup+1}'))
                if setup == 1:
                    body['lines'] = _transform_180(body['lines'], cfg.stock_width,
                                                   cfg.stock_length)
                    if tab_positions and pp.config.remove_tabs:
                        setup2_removal = _transform_180(
                            pp._generate_tab_removal_gcode(list(enumerate(tab_positions))),
                            cfg.stock_width, cfg.stock_length)
                bodies_by_setup[setup].append(body)
                continue
            # Perimeter chamfer uses the indexed profile path below; retain its closed
            # interior targets through the normal generator.
            actual = op
            closed_targets = None
            has_perimeter_chamfer = (op.op_type == 'chamfer'
                                     and 'perimeter' in (op.scope.get('targets') or ['perimeter']))
            if has_perimeter_chamfer:
                targets = [t for t in (op.scope.get('targets') or ['perimeter']) if t != 'perimeter']
                closed_targets = targets
                if targets:
                    actual = tooling.Operation('chamfer', op.tool_slot, op.name,
                                               scope=dict(op.scope, targets=targets))
            body = tooling.generate_operation(job, part, actual, survey,
                                              index_window=windows[setup],
                                              index_feature_keys=owner[setup])
            if body['errors']:
                errors.extend(body['errors'])
            if has_perimeter_chamfer:
                if not closed_targets:
                    body['lines'] = []
                true_ring = LineString(list(orient(Polygon(body['pp'].perimeter), 1.0)
                                                .exterior.coords)[::-1])
                chamfer_fragments = _line_parts(true_ring.intersection(regions[setup]))
                body['lines'] += _open_chamfer(body['pp'], chamfer_fragments,
                                               op.chamfer_width,
                                               job.tool(op.tool_slot).included_angle)
            if body['lines']:
                if setup == 1:
                    body['lines'] = _transform_180(body['lines'], cfg.stock_width,
                                                   cfg.stock_length)
                bodies_by_setup[setup].append(body)
    if setup2_removal:
        bodies_by_setup[1].append(dict(
            profile_body, lines=setup2_removal, deferred=None,
            op=tooling.Operation('perimeter', profile_op.tool_slot,
                                 f'{profile_op.label} - tab removal')))
    if job.engrave:
        writing_tool = next((t for t in job.used_tools if t.type == 'endmill'), None)
        if writing_tool is None:
            warnings.append(f'{part.name}: engraving skipped because no end mill is loaded.')
        else:
            engraving, engraving_warnings = tooling._engrave_lines(
                job, part, writing_tool.diameter, tool_flutes=writing_tool.flutes)
            warnings.extend(engraving_warnings)
            ys = [float(v) for line in engraving
                  for v in re.findall(r'(?<![A-Z])Y(-?\d+(?:\.\d*)?)', line, re.I)]
            choices = [i for i, w in enumerate(windows)
                       if ys and min(ys)-widest/2 >= w[0]-1e-6
                       and max(ys)+widest/2 <= w[1]+1e-6]
            if engraving and not choices:
                errors.append('The engraving crosses the indexed handoff and cannot be '
                              'completed wholly from either setup. Move or shorten its text.')
            elif engraving:
                setup = min(choices, key=lambda i: abs(sum(ys)/len(ys) -
                            (machine_y/2 if i == 0 else cfg.stock_length-machine_y/2)))
                lines = (_transform_180(engraving, cfg.stock_width, cfg.stock_length)
                         if setup == 1 else engraving)
                target = next((b for b in bodies_by_setup[setup]
                               if b['tool'].slot == writing_tool.slot), None)
                if target:
                    target['lines'] = lines + target['lines']
                else:
                    errors.append('Engraving has no compatible cutting operation in its setup.')
    if errors:
        return PostProcessorResult(success=False, errors=errors)
    bodies = bodies_by_setup[0] + bodies_by_setup[1]
    result = tooling.assemble_job(job, bodies, timestamp=timestamp,
                                  suggested_filename=suggested_filename,
                                  extra_warnings=warnings,
                                  setup_break_index=len(bodies_by_setup[0]))
    if result.success:
        result.stats.update({'indexed_y': True, 'setup_count': 2,
                             'overlap': overlap, 'handoff_y': handoff,
                             'setup_ranges': [[0, machine_y],
                                              [cfg.stock_length-machine_y, cfg.stock_length]],
                             'fixture_gcode': _fixture_program(job, job.tool(profile_op.tool_slot), pp),
                             'fixture_filename': build_output_filename(
                                 (suggested_filename or job.name) + '_FIXTURE', timestamp, 'fixture')})
    return result
