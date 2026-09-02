"""Spoilboard-surfacing G-code generation.

"Bed leveling" on a router means taking a shallow raster cut across the
spoilboard so its top is parallel to the machine's XY motion.  This module is
deliberately independent of Flask: the web route and tests both use the same
validation and path generator.
"""

from dataclasses import dataclass
import math
from typing import Dict, List, Optional, Tuple


MAX_RASTER_ROWS = 2000


class BedLevelingError(ValueError):
    """A requested surfacing program is unsafe or cannot fit the machine."""


@dataclass(frozen=True)
class BedLevelingSpec:
    width: float
    height: float
    tool_diameter: float
    stepover_percent: float
    depth: float
    feed_rate: float
    plunge_rate: float
    spindle_speed: int
    safe_z: float
    raster_direction: str = 'long'


@dataclass(frozen=True)
class BedLevelingResult:
    gcode: str
    filename: str
    path: List[Tuple[float, float]]
    rows: int
    cutting_distance: float
    estimated_minutes: float
    pass_axis: str
    actual_stepover: float

    def stats(self) -> Dict[str, float]:
        return {
            'rows': self.rows,
            'passes': self.rows,
            'cutting_distance': round(self.cutting_distance, 2),
            'estimated_minutes': round(self.estimated_minutes, 1),
            'pass_axis': self.pass_axis,
            'actual_stepover': round(self.actual_stepover, 3),
        }


def _runs_along_x(width: float, length: float, direction: str) -> bool:
    """Resolve the human long/short choice to an actual machine axis."""
    return ((width >= length) if direction == 'long' else (width < length))


def _finite_number(name: str, value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BedLevelingError(f'{name} must be a number.') from exc
    if not math.isfinite(number):
        raise BedLevelingError(f'{name} must be finite.')
    return number


def parse_spec(data: Dict, machine_width: Optional[float] = None,
               machine_height: Optional[float] = None,
               machine_z: Optional[float] = None) -> BedLevelingSpec:
    """Validate JSON-like input and return a normalized inch-based spec."""
    width = _finite_number('Width', data.get('width'))
    # Accept the original `height` key for downloaded clients and older open pages.
    height = _finite_number('Length', data.get('length', data.get('height')))
    tool = _finite_number('Cutter diameter', data.get('tool_diameter'))
    stepover = _finite_number('Stepover', data.get('stepover_percent'))
    depth = _finite_number('Cut depth', data.get('depth'))
    feed = _finite_number('Feed rate', data.get('feed_rate'))
    plunge = _finite_number('Plunge rate', data.get('plunge_rate'))
    rpm_number = _finite_number('Spindle speed', data.get('spindle_speed'))
    safe_z = _finite_number('Safe Z', data.get('safe_z'))
    direction = str(data.get('raster_direction', 'long')).strip().lower()

    if width <= 0 or height <= 0:
        raise BedLevelingError('Width and length must be greater than zero.')
    if machine_width and width > machine_width + 1e-9:
        raise BedLevelingError(
            f'Width {width:g} in exceeds the machine X travel of {machine_width:g} in.')
    if machine_height and height > machine_height + 1e-9:
        raise BedLevelingError(
            f'Length {height:g} in exceeds the machine Y travel of {machine_height:g} in.')
    if tool <= 0:
        raise BedLevelingError('Cutter diameter must be greater than zero.')
    if tool > min(width, height):
        raise BedLevelingError('Cutter diameter must not exceed the surfacing area.')
    if not 10 <= stepover <= 90:
        raise BedLevelingError('Stepover must be between 10% and 90%.')
    if not 0 < depth <= 0.125:
        raise BedLevelingError('Cut depth must be greater than 0 and no more than 0.125 in.')
    if not 1 <= feed <= 500:
        raise BedLevelingError('Feed rate must be between 1 and 500 in/min.')
    if not 1 <= plunge <= 200:
        raise BedLevelingError('Plunge rate must be between 1 and 200 in/min.')
    if rpm_number != int(rpm_number) or not 1000 <= rpm_number <= 40000:
        raise BedLevelingError('Spindle speed must be a whole number from 1,000 to 40,000 RPM.')
    if safe_z < 0.05:
        raise BedLevelingError('Safe Z must be at least 0.05 in above the spoilboard.')
    if machine_z is None and safe_z > 2.0:
        raise BedLevelingError(
            'Safe Z must be no more than 2 in when machine Z travel is not configured.')
    if machine_z is not None and safe_z > machine_z + 1e-9:
        raise BedLevelingError(
            f'Safe Z {safe_z:g} in exceeds the machine Z travel of {machine_z:g} in.')
    if direction not in ('long', 'short'):
        raise BedLevelingError('Raster direction must be long or short.')
    along_x = _runs_along_x(width, height, direction)
    cross_span = height if along_x else width
    row_count = math.ceil(cross_span / (tool * stepover / 100.0)) + 1
    if row_count > MAX_RASTER_ROWS:
        raise BedLevelingError(
            f'This setup needs {row_count:,} raster passes; use a larger cutter or '
            f'stepover to stay at or below {MAX_RASTER_ROWS:,}.')

    return BedLevelingSpec(width, height, tool, stepover, depth, feed, plunge,
                           int(rpm_number), safe_z, direction)


def raster_path(spec: BedLevelingSpec) -> List[Tuple[float, float]]:
    """Return a continuous raster spanning the machine's full requested travel.

    Width and height describe cutter-center travel, matching the machine dimensions
    shown in the UI.  Driving the center all the way to each limit also lets the
    cutter overhang the spoilboard edge instead of leaving an uncut perimeter.
    """
    along_x = _runs_along_x(spec.width, spec.height, spec.raster_direction)
    along_span = spec.width if along_x else spec.height
    cross_span = spec.height if along_x else spec.width
    max_step = spec.tool_diameter * spec.stepover_percent / 100.0

    # Divide the cross span evenly. The last pass lands exactly at the far travel
    # limit and every actual step remains at or below the requested maximum.
    intervals = max(1, math.ceil(cross_span / max_step))
    cross_values = [cross_span * i / intervals for i in range(intervals + 1)]

    def point(along: float, cross: float) -> Tuple[float, float]:
        return (along, cross) if along_x else (cross, along)

    path: List[Tuple[float, float]] = [point(0.0, cross_values[0])]
    for index, cross in enumerate(cross_values):
        path.append(point(along_span if index % 2 == 0 else 0.0, cross))
        if index + 1 < len(cross_values):
            path.append(point(path[-1][0] if along_x else path[-1][1],
                              cross_values[index + 1]))
    return path


def generate_bed_leveling(spec: BedLevelingSpec) -> BedLevelingResult:
    """Build a conservative Mach/GRBL-compatible inch G-code program."""
    path = raster_path(spec)
    cutting_distance = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                           for a, b in zip(path, path[1:]))
    plunge_distance = spec.depth + min(spec.safe_z, 0.05)
    estimated = (cutting_distance / spec.feed_rate +
                 plunge_distance / spec.plunge_rate + 2.0 / 60.0)
    rows = (len(path) + 1) // 2
    pass_axis = 'X' if _runs_along_x(spec.width, spec.height,
                                     spec.raster_direction) else 'Y'
    cross_span = spec.height if pass_axis == 'X' else spec.width
    actual_stepover = cross_span / (rows - 1) if rows > 1 else 0.0

    lines = [
        '%',
        '(UV-CAM BED LEVELING - SPOILBOARD SURFACING)',
        f'(Area: {spec.width:.3f} wide x {spec.height:.3f} long)',
        f'(Cutter: {spec.tool_diameter:.3f} in; stepover: {spec.stepover_percent:g} percent)',
        f'(Cut depth: {spec.depth:.4f} in; passes: {rows})',
        f'(Pass direction: {spec.raster_direction} way, along {pass_axis})',
        '(G54 X0 Y0: lower-left cutter-center travel limit)',
        '(G54 Z0: TOP OF SPOILBOARD before this cut)',
        '(Cutter center traverses the full requested X/Y extents.)',
        '(Verify the cutter, work offset, hold-downs, and full travel before running.)',
        '',
        'G90 G94 G17 G40 G49  ; Absolute, feed/min, XY plane, compensation off',
        'G20  ; Inches',
        'G92.1  ; Cancel temporary coordinate offsets',
        'G54  ; Work coordinate system 1',
        'M5  ; Establish spindle-off state',
        f'G0 Z{spec.safe_z:.4f}  ; Safe Z',
        f'G0 X{path[0][0]:.4f} Y{path[0][1]:.4f}  ; First pass start',
        f'S{spec.spindle_speed} M3  ; Spindle on',
        'G4 P2  ; Wait for spindle',
        f'G0 Z{min(spec.safe_z, 0.05):.4f}  ; Approach surface',
        f'G1 Z{-spec.depth:.4f} F{spec.plunge_rate:.1f}  ; Plunge to surfacing depth',
    ]
    for index, (x, y) in enumerate(path[1:]):
        feed_word = f' F{spec.feed_rate:.1f}' if index == 0 else ''
        lines.append(f'G1 X{x:.4f} Y{y:.4f}{feed_word}')
    lines.extend([
        f'G0 Z{spec.safe_z:.4f}  ; Retract',
        'M5  ; Spindle off',
        'G0 X0 Y0  ; Return to work origin',
        'M30  ; Program end',
        '%',
        '',
    ])

    filename = f'UV-CAM_bed_level_{spec.width:g}x{spec.height:g}in.nc'
    return BedLevelingResult('\n'.join(lines), filename, path, rows,
                             cutting_distance, estimated, pass_axis,
                             actual_stepover)
