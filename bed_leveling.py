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


@dataclass(frozen=True)
class BedLevelingResult:
    gcode: str
    filename: str
    path: List[Tuple[float, float]]
    rows: int
    cutting_distance: float
    estimated_minutes: float

    def stats(self) -> Dict[str, float]:
        return {
            'rows': self.rows,
            'cutting_distance': round(self.cutting_distance, 2),
            'estimated_minutes': round(self.estimated_minutes, 1),
        }


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
    height = _finite_number('Height', data.get('height'))
    tool = _finite_number('Cutter diameter', data.get('tool_diameter'))
    stepover = _finite_number('Stepover', data.get('stepover_percent'))
    depth = _finite_number('Cut depth', data.get('depth'))
    feed = _finite_number('Feed rate', data.get('feed_rate'))
    plunge = _finite_number('Plunge rate', data.get('plunge_rate'))
    rpm_number = _finite_number('Spindle speed', data.get('spindle_speed'))
    safe_z = _finite_number('Safe Z', data.get('safe_z'))

    if width <= 0 or height <= 0:
        raise BedLevelingError('Width and height must be greater than zero.')
    if machine_width and width > machine_width + 1e-9:
        raise BedLevelingError(
            f'Width {width:g} in exceeds the machine X travel of {machine_width:g} in.')
    if machine_height and height > machine_height + 1e-9:
        raise BedLevelingError(
            f'Height {height:g} in exceeds the machine Y travel of {machine_height:g} in.')
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
    usable_y = max(0.0, height - tool)
    row_count = (math.ceil(usable_y / (tool * stepover / 100.0)) + 1
                 if usable_y else 1)
    if row_count > MAX_RASTER_ROWS:
        raise BedLevelingError(
            f'This setup needs {row_count:,} raster rows; use a larger cutter or '
            f'stepover to stay at or below {MAX_RASTER_ROWS:,}.')

    return BedLevelingSpec(width, height, tool, stepover, depth, feed, plunge,
                           int(rpm_number), safe_z)


def raster_path(spec: BedLevelingSpec) -> List[Tuple[float, float]]:
    """Return a continuous, alternating-X path that covers the requested rectangle."""
    radius = spec.tool_diameter / 2.0
    first_y = radius
    last_y = spec.height - radius
    usable_y = max(0.0, last_y - first_y)
    max_step = spec.tool_diameter * spec.stepover_percent / 100.0

    # Divide the usable span evenly.  That makes the final row land exactly at the
    # far cutter-radius boundary and guarantees every actual step is <= max_step.
    intervals = max(1, math.ceil(usable_y / max_step)) if usable_y else 0
    y_values = ([first_y] if intervals == 0 else
                [first_y + usable_y * i / intervals for i in range(intervals + 1)])
    x_low, x_high = radius, spec.width - radius

    path: List[Tuple[float, float]] = [(x_low, y_values[0])]
    for index, y in enumerate(y_values):
        path.append((x_high if index % 2 == 0 else x_low, y))
        if index + 1 < len(y_values):
            path.append((path[-1][0], y_values[index + 1]))
    return path


def generate_bed_leveling(spec: BedLevelingSpec) -> BedLevelingResult:
    """Build a conservative Mach/GRBL-compatible inch G-code program."""
    path = raster_path(spec)
    cutting_distance = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                           for a, b in zip(path, path[1:]))
    plunge_distance = spec.depth + min(spec.safe_z, 0.05)
    estimated = cutting_distance / spec.feed_rate + plunge_distance / spec.plunge_rate
    rows = (len(path) + 1) // 2

    lines = [
        '%',
        '(PenguinCAM BED LEVELING - SPOILBOARD SURFACING)',
        f'(Area: {spec.width:.3f} x {spec.height:.3f} in)',
        f'(Cutter: {spec.tool_diameter:.3f} in; stepover: {spec.stepover_percent:g} percent)',
        f'(Cut depth: {spec.depth:.4f} in; rows: {rows})',
        '(G54 X0 Y0: lower-left corner of surfacing area)',
        '(G54 Z0: TOP OF SPOILBOARD before this cut)',
        '(Verify the cutter, work offset, hold-downs, and full travel before running.)',
        '',
        'G90 G94 G17 G40 G49  ; Absolute, feed/min, XY plane, compensation off',
        'G20  ; Inches',
        'G92.1  ; Cancel temporary coordinate offsets',
        'G54  ; Work coordinate system 1',
        'M5  ; Spindle off during setup check',
        f'G0 Z{spec.safe_z:.4f}  ; Safe Z',
        f'G0 X{path[0][0]:.4f} Y{path[0][1]:.4f}  ; First pass start',
        'M0  ; VERIFY Z0 ON SPOILBOARD TOP AND CLEAR FULL XY TRAVEL',
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

    filename = f'PenguinCAM_bed_level_{spec.width:g}x{spec.height:g}in.nc'
    return BedLevelingResult('\n'.join(lines), filename, path, rows,
                             cutting_distance, estimated)
