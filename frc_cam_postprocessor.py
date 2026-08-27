#!/usr/bin/env python3
"""
PenguinCAM - FRC Team 6238 CAM Post-Processor
Generates G-code from DXF files with predefined operations for:
- Circular holes (helical + spiral clearing)
- Pockets
- Perimeter with tabs
"""

# Standard library
import argparse
import datetime
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any

# Third-party
import ezdxf

import stroke_font
import yaml
from shapely import affinity
from shapely.geometry import Point, Polygon, LinearRing, MultiPolygon, LineString
from shapely.geometry import box as box_geom
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

# Local modules
import dxf_geometry
from dxf_geometry import entities_to_closed_paths, sample_spline
from team_config import TeamConfig


@dataclass
class PostProcessorResult:
    """Result from post-processor operations"""
    success: bool
    gcode: Optional[str] = None
    filename: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'success': self.success,
            'gcode': self.gcode,
            'filename': self.filename,
            'errors': self.errors,
            'warnings': self.warnings,
            'stats': self.stats
        }


# Material presets based on team 6238 feeds/speeds document
MATERIAL_PRESETS = {
    'plywood': {
        'name': 'Plywood',
        'feed_rate': 75.0,        # Cutting feed rate (IPM)
        'ramp_feed_rate': 50.0,   # Ramp feed rate (IPM)
        'plunge_rate': 35.0,      # Plunge feed rate (IPM) for tab Z moves
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 20.0,       # Ramp angle in degrees
        'ramp_start_clearance': 0.150,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.65,    # Radial stepover as fraction of tool diameter (65% for plywood)
        'helix_radius_multiplier': 0.75, # Helix entry radius as fraction of tool radius
        'max_slotting_depth': 0.4,      # Maximum depth per pass for perimeter slotting (inches)
        'corner_min_feed_scale': 0.7,   # Corner-slowdown feed floor (softer/heat-limited material)
        'tab_width': 0.25,        # Tab width (inches)
        'tab_height': 0.15,        # Tab height (inches)
        'description': 'Standard plywood settings - 18K RPM, 75 IPM cutting'
    },
    'aluminum': {
        # Derated 2026-08-24 after real 1/8" bits kept snapping: the old 55 IPM /
        # 0.2" slot numbers let a 1/8" plate be slotted full-thickness in one pass,
        # which no hobby-router aluminum practice supports. These match the
        # FRC/Omio-class consensus (~0.4 x D per pass, light chipload, dry).
        'name': 'Aluminum',
        'feed_rate': 30.0,        # Cutting feed rate (IPM) - 0.0017 in/tooth at 18K 1F
        'ramp_feed_rate': 19.0,   # Ramp feed rate (IPM)
        'plunge_rate': 15.0,      # Plunge feed rate (IPM) for tab Z moves - slower for aluminum
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 4.0,        # Ramp angle in degrees
        'ramp_start_clearance': 0.050,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.25,    # Radial stepover as fraction of tool diameter (25% conservative for aluminum)
        'helix_radius_multiplier': 0.5,  # Helix entry radius as fraction of tool radius (conservative for aluminum)
        'max_slotting_depth': 0.06,     # Maximum depth per pass for slotting (0.38 x reference diameter)
        'corner_min_feed_scale': 0.6,   # Corner-slowdown feed floor (see apply_material_preset)
        'tab_width': 0.25,        # Tab width (inches) - same as plywood
        'tab_height': 0.15,       # Tab height (inches) - same as plywood
        'description': 'Aluminum - 18K RPM, 30 IPM cutting, 0.06" max pass, 4° ramp'
    },
    'polycarbonate': {
        'name': 'Polycarbonate',
        'feed_rate': 75.0,        # Same as plywood
        'ramp_feed_rate': 50.0,   # Same as plywood
        'plunge_rate': 20.0,      # Same as plywood - matches Fusion 360
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 20.0,       # Same as plywood
        'ramp_start_clearance': 0.100,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.55,    # Radial stepover as fraction of tool diameter (55% moderate for polycarbonate)
        'helix_radius_multiplier': 0.75, # Helix entry radius as fraction of tool radius
        'max_slotting_depth': 0.25,     # Maximum depth per pass for perimeter slotting (inches)
        'corner_min_feed_scale': 0.7,   # Corner-slowdown feed floor (softer/heat-limited material)
        'tab_width': 0.25,        # Tab width (inches) - same as plywood
        'tab_height': 0.15,        # Tab height (inches) - same as plywood
        'description': 'Polycarbonate - same as plywood settings'
    }
}


def sanitize_filename_base(name: str, fallback: str = "output") -> str:
    """Make an arbitrary Onshape part / job name safe to use as a filename base.

    Onshape names can contain path separators and characters that are illegal in filenames
    or in the download's Content-Disposition header - e.g. a part named '1/4" plate', where
    the '/' otherwise makes os.path.join write into a nonexistent '1/' subdirectory (the
    write fails, silently to the UI) and the '"' breaks the Content-Disposition quoting.
    Replaces [/ \\ : * ? " < > |] with '-', collapses whitespace, trims stray leading/
    trailing dots and dashes, and falls back to `fallback` if nothing usable remains."""
    if not name:
        return fallback
    cleaned = re.sub(r'[/\\:*?"<>|]+', '-', name)   # path separators + filename/header-illegal chars
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()  # collapse whitespace runs
    cleaned = cleaned.strip('.-').strip()           # no leading/trailing dots or dashes
    return cleaned if cleaned else fallback


# Characters that must never reach a G-code comment. Parentheses would nest the comment
# and square brackets are read as expressions by some controllers, so both are turned into
# a dash rather than swapped for each other (see CLAUDE.md, "G-code Generation Rules").
_COMMENT_TRANSLITERATIONS = {
    '‘': "'", '’': "'", '“': '"', '”': '"',   # curly quotes
    '–': '-', '—': '-', '−': '-',                   # dashes / minus
    '°': ' deg', '″': '"', '′': "'",                # degree, inch/foot marks
    '×': 'x', '→': '->', 'µ': 'u',                  # times, arrow, micro
}


def sanitize_comment(text: str, fallback: str = '') -> str:
    """Make arbitrary text (a CAD part name, a tool name, a machine name) safe to drop
    inside a G-code `(...)` comment: pure ASCII, no parentheses, no square brackets.

    Both rules are hard requirements of the controllers this output runs on - nested
    comments and bracketed text produce unpredictable behaviour - and neither is fully
    covered by the unit tests, since much of the commentary is generated conditionally."""
    if not text:
        return fallback
    out = str(text)
    for src_ch, dst in _COMMENT_TRANSLITERATIONS.items():
        out = out.replace(src_ch, dst)
    out = out.encode('ascii', 'ignore').decode('ascii')   # drop anything still non-ASCII
    out = re.sub(r'[()\[\]]+', '-', out)                  # never nest or bracket a comment
    out = re.sub(r'\s+', ' ', out).strip(' -')
    return out if out else fallback


def build_output_filename(suggested_filename: str, timestamp: str, fallback: str = "output",
                          dry_run: bool = False) -> str:
    """Build the '<name>_<timestamp>.nc' output filename, sanitizing BOTH halves so the
    result is safe to write to disk and serve. Single chokepoint shared by every generator
    and the multi-part job assembler.

    The timestamp is client-supplied (the browser sends its local time so the filename
    matches the operator's clock), and it used to reach the path with only '-', ' ' and
    ':' stripped - so a value like '/../../escape' walked straight out of the output
    directory. Only digits and underscores can survive now; anything else falls back to
    server time, because a filename is not worth trusting input for."""
    base_name = sanitize_filename_base(suggested_filename, fallback)
    stamped = re.sub(r'[^0-9_]', '', str(timestamp).replace('-', '').replace(' ', '_')
                     .replace(':', ''))
    if not stamped.strip('_'):
        stamped = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base_name}{'_DRYRUN' if dry_run else ''}_{stamped}.nc"


_RESUME_CHECKPOINT_RE = re.compile(
    r'^\( === RESUME CHECKPOINT ([A-Za-z0-9_-]+) - (.+?) === \)$')


def build_resume_programs(gcode: str, main_filename: str) -> List[Dict[str, str]]:
    """Build standalone, tail-only programs for every manual tool-change boundary.

    Mach3's Run From Here reconstructs modal state by dummy-running earlier code and
    then proposes a preparatory move. A standalone tail is less ambiguous: its first
    executable block is the checkpoint's explicit modal reset and safe-Z move. The
    operator confirmation deliberately precedes that block, so loading a resume file
    cannot move the machine before the setup has been checked.
    """
    lines = (gcode or '').splitlines()
    material_header = next((line.lower() for line in lines
                            if line.startswith('(Material:')), '')
    aluminum_program = 'aluminum' in material_header
    aluminum_6063 = aluminum_program and '6063' in material_header
    uses_coolant = any(
        re.match(r'^\s*M0?[78]\b', re.sub(r'\(.*?\)', '', line).split(';')[0], re.I)
        for line in lines
    )
    stem = os.path.splitext(os.path.basename(main_filename or 'program.nc'))[0]
    programs = []
    for index, line in enumerate(lines):
        match = _RESUME_CHECKPOINT_RE.match(line.strip())
        if not match:
            continue
        checkpoint, description = match.groups()
        safe_description = sanitize_comment(description, 'tool change')
        filename = sanitize_filename_base(
            f'{stem}_RESUME_{checkpoint}', f'RESUME_{checkpoint}') + '.nc'
        preamble = [
            '(PENGUINCAM STANDALONE RESUME PROGRAM)',
            f'(Checkpoint {checkpoint} - {safe_description})',
            '(Use only at this tool boundary, never in the middle of an operation)',
            '(Reference or home the machine if position may have been lost)',
            '(Verify G54 X and Y still match the original job zero)',
            '(Install the named tool and re-zero G54 Z on the stated surface, not with G92)',
        ]
        if aluminum_program:
            preamble += [
                '(Confirm cutter is sharp, clean, aluminum-approved, and at minimum stickout)',
                '(Confirm clean collet, low runout, continuous directed air, and chip escape)',
            ]
            if aluminum_6063:
                preamble.append(
                    '(Confirm proven aluminum-compatible lubricant or MQL is ready for 6063)')
        if uses_coolant:
            preamble.append('M9  ; Keep coolant off during resume setup')
        preamble += [
            'M5  ; Keep spindle stopped during resume setup',
            '( Press CYCLE START only after every resume check is complete )',
            'M0  ; Confirm standalone resume setup',
            '',
        ]
        programs.append({
            'checkpoint': checkpoint,
            'description': safe_description,
            'filename': filename,
            'gcode': '\n'.join(preamble + lines[index:]) + '\n',
        })
    return programs


# Where the operator sets Z zero. 'board' (the default, and what every program this
# tool has ever produced used) means Z0 is the sacrifice board, so the stock top is at
# +thickness and a through-cut is a shallow negative. 'stock_top' means Z0 is the top
# face of the stock, so cutting is negative all the way down - the convention Fusion and
# most textbooks use, and the one to pick when the stock is held in a vise or the board
# is not a reliable reference.
#: Default label size. A cap height that reads across a workbench, and a cut deep enough
#: to survive handling without weakening the part. Both callers - the single-part route
#: and the multi-tool assembler - use these, so they live with the engraver rather than
#: with whichever route happened to need them first.
ENGRAVE_HEIGHT_IN = 0.18
ENGRAVE_DEPTH_IN = 0.01

Z_DATUM_BOARD = 'board'
Z_DATUM_STOCK_TOP = 'stock_top'
_Z_DATUM_ALIASES = {
    'board': Z_DATUM_BOARD, 'sacrifice': Z_DATUM_BOARD, 'sacrifice_board': Z_DATUM_BOARD,
    'sacrifice-board': Z_DATUM_BOARD, 'spoilboard': Z_DATUM_BOARD, 'bottom': Z_DATUM_BOARD,
    'top': Z_DATUM_STOCK_TOP, 'stock_top': Z_DATUM_STOCK_TOP, 'stock-top': Z_DATUM_STOCK_TOP,
    'material_top': Z_DATUM_STOCK_TOP, 'material-top': Z_DATUM_STOCK_TOP,
}


def normalize_z_datum(value, default: str = Z_DATUM_BOARD) -> str:
    """Map any accepted spelling of the Z datum onto 'board' or 'stock_top'.

    Empty/None means "unspecified" and returns the default. Anything else that is not
    recognised raises: a mistyped datum silently falling back would zero the program a
    material thickness away from where the operator set the tool, which is the one class
    of mistake this whole option exists to make explicit."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    key = str(value).strip().lower().replace(' ', '_')
    if key in _Z_DATUM_ALIASES:
        return _Z_DATUM_ALIASES[key]
    raise ValueError(f"Unknown Z datum {value!r}: expected 'board' (sacrifice board) "
                     f"or 'stock_top'")


class FRCPostProcessor:
    def __init__(self, material_thickness: float, tool_diameter: float, units: str = "inch",
                 config: Optional[TeamConfig] = None, z_datum: Optional[str] = None,
                 tool_flutes: int = 1):
        """
        Initialize the post-processor

        Args:
            material_thickness: Thickness of material in inches
            tool_diameter: Diameter of cutting tool in inches (e.g., 4mm = 0.157")
            tool_flutes: Number of cutting flutes. The aluminum safety checks need this;
                   omitting it retains the safe single-flute default.
            units: "inch" or "mm"
            config: Optional TeamConfig instance for team-specific settings.
                   If not provided, uses Team 6238 defaults.
            z_datum: Where the operator zeros Z - 'board' (sacrifice board, the default)
                   or 'stock_top'. None takes the team config's setting.
        """
        # Use provided config or create default (Team 6238 defaults)
        if config is None:
            config = TeamConfig()
        self.config = config

        # A non-positive or non-finite tool makes the pocket-clearing loop step OUTWARD
        # every pass, so neither of its exit conditions can ever fire and the request
        # hangs inside shapely forever. Rejected at construction: every toolpath in this
        # class divides by, offsets by, or steps over the tool, and none of them mean
        # anything for a tool of zero or negative width.
        if not math.isfinite(tool_diameter) or tool_diameter <= 0:
            raise ValueError(f'Tool diameter must be a positive finite number, '
                             f'got {tool_diameter!r}')
        if not math.isfinite(material_thickness) or material_thickness <= 0:
            raise ValueError(f'Material thickness must be a positive finite number, '
                             f'got {material_thickness!r}')
        if isinstance(tool_flutes, bool):
            raise ValueError(f'Tool flutes must be a whole number from 1 to 12, '
                             f'got {tool_flutes!r}')
        try:
            flute_number = float(tool_flutes)
            parsed_flutes = int(flute_number)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Tool flutes must be a whole number from 1 to 12, '
                             f'got {tool_flutes!r}') from exc
        if (not math.isfinite(flute_number) or parsed_flutes != flute_number
                or not 1 <= parsed_flutes <= 12):
            raise ValueError(f'Tool flutes must be a whole number from 1 to 12, '
                             f'got {tool_flutes!r}')

        self.material_thickness = material_thickness
        self.tool_diameter = tool_diameter
        self.tool_radius = tool_diameter / 2
        self.tool_flutes = parsed_flutes
        self.units = units

        # Corner slowdown for contour-parallel pocket clearing. At a sharp interior corner the
        # cutter wraps around two edges at once, so its engagement (and cutting force) spikes
        # well above the straight-edge stepover. We keep the toolpath geometry identical but
        # reduce the feed within `corner_slowdown_zone` of a sharp corner, down to
        # `corner_min_feed_scale` x feed at the sharpest corners, easing back to full feed on
        # the straights. Only collinear waypoints are added, so the path is unchanged.
        self.corner_slowdown_zone = tool_diameter        # reduced-feed distance on each side of a corner
        self.corner_min_feed_scale = 0.6                 # default; set per-material in apply_material_preset

        # Hole detection tolerance from config
        self.tolerance = config.hole_detection_tolerance

        # Minimum hole diameter that can be milled (must be > tool diameter for chip evacuation)
        # Holes smaller than this are skipped
        self.min_millable_hole = tool_diameter * config.min_millable_hole_multiplier

        # Tolerance for the "hole at least tool-sized" gate. Absorbs unit-conversion / DXF
        # rounding (e.g. a "4mm" -> 0.15748" tool vs a 0.157" hole) so an essentially
        # tool-sized hole is peck-drilled rather than rejected as "too small", and doubles
        # as the threshold below which a peck hole is a pure straight plunge with no lateral
        # clearing (which would otherwise emit a degenerate zero-radius arc).
        self.hole_size_tolerance = 0.002 if units == "inch" else 0.05

        # Multi-layer support
        self.layer_data = None  # Set by load_dxf for multi-layer DXFs

        # Z-axis reference. Which surface Z0 sits on is the operator's choice
        # (see normalize_z_datum); everything downstream is written against
        # material_top / cut_depth rather than against zero, so the datum only has
        # to be applied once, here.
        self.sacrifice_board_depth = config.sacrifice_board_depth  # How far to cut into sacrifice board (inches)
        self.clearance_height = config.clearance_height  # Clearance above material top for rapid moves (inches)
        self.z_datum = normalize_z_datum(z_datum if z_datum is not None else config.z_datum)
        # True only when the loaded cutter actually has a CONE at its tip - i.e. a twist
        # drill. It decides whether a through hole gets the point-length allowance that
        # lets the full diameter emerge. The single-tool flow knows nothing about tool
        # types (it is given a diameter and nothing else), so this is set by the paths
        # that do know: the drilled tube pattern below, and tooling.py's drill path,
        # which calls _generate_drill_gcode directly.
        self.tool_has_drill_point = False

        # Dry run: the same program, lifted clear of the work with the spindle off, for
        # proving a setup before committing to a cut. Zero means a real cutting program.
        # The lift is applied in _apply_z_frame, so it moves the three Z anchors and
        # every toolpath follows - the dry run is provably the same motion, raised.
        self.dry_run_lift = 0.0     # the lift ACTUALLY applied; see _apply_z_frame
        self.dry_run_request = 0.0  # what the caller asked for
        self._apply_z_frame()   # sets material_top, retract_height, cut_depth

        # True when the tool is parked at safe height (above the clearance plane) and the
        # next feature must first rapid down to the clearance plane before its slow plunge
        # feed. Set at job start and after any pause/park; consumed by _approach_ramp_start.
        self._pending_clearance_rapid = False

        # Cutting parameters (defaults - can be overridden by material presets)
        self.spindle_speed = 18000  # RPM
        self.feed_rate = 75.0 if units == "inch" else 1905  # Cutting feed rate (IPM or mm/min)
        self.ramp_feed_rate = 50.0 if units == "inch" else 1270  # Ramp feed rate (IPM or mm/min)
        self.plunge_rate = 35.0 if units == "inch" else 889  # Plunge feed rate (IPM or mm/min) for tab Z moves
        self.traverse_rate = 200.0 if units == "inch" else 5080  # Lateral moves above material (IPM or mm/min) - rapid moves
        self.approach_rate = 50.0 if units == "inch" else 1270  # Z approach to ramp start height (IPM or mm/min)
        self.ramp_angle = 20.0  # Ramp angle in degrees (for helical bores and perimeter ramps)
        self.ramp_start_clearance = 0.15 if units == "inch" else 3.8  # Clearance above material to start ramping
        self.stepover_percentage = 0.6  # Radial stepover as fraction of tool diameter (default 60%)

        # Tab parameters from config
        self.tabs_enabled = config.tabs_enabled  # Whether tabs are enabled
        self.tab_width = config.tab_width  # Width of tabs (inches)
        self.tab_height = config.tab_height  # How much material to leave in tab (inches)
        # Config states the spacing in inches; a mm program was placing a tab every 2 mm.
        self.tab_spacing = self._len(config.tab_spacing)

        # Fixturing preferences from config
        self.pause_before_perimeter = config.pause_before_perimeter  # Pause before perimeter for screw fixturing

        # Optional deburr / chamfer pass (standard 2D mode only). Set to the dict
        # parse_chamfer_spec() returns ({'width', 'bit_diameter', 'bit_angle',
        # 'targets'}) to append a V-bit edge-break pass after the profile cut, behind a
        # manual tool change. None (the default) leaves the program exactly as before.
        self.chamfer_pass = None
        # Engrave the part's name into its own face, so a nest of twelve similar
        # brackets is not a puzzle once it is off the machine. None = off; otherwise
        # {'text': str, 'height': in, 'depth': in}.
        self.engrave = None
        # Advice raised while building a toolpath, as opposed to self.errors which
        # refuses the program. A skipped engraving is the case that needs it: silence
        # would leave an operator expecting a label that is not there.
        self.warnings = []
        # Advice from reading the DXF (an unclosed outline, a bridged gap). Separate
        # from self.warnings because that one is cleared at generation time.
        self.geometry_warnings = []
        #: Chains that did not close, so identify_perimeter_and_pockets can tell a lost
        #: outer profile from a stray line.
        self.open_chains = []

        # Tube facing parameters
        self.tube_facing_offset = 0.0625  # Hole offset to align with faced surface at Y=+1/16" (inches)

        # Tube facing operation constants from config
        self.tube_facing_params = config.get_tube_facing_params()

        # Machine-specific constants from config. park_position is optional (a machine
        # coordinate tuple or None); when None, no G53 is emitted and the output is
        # portable across controllers.
        self.park_position = config.park_position  # (x, y, z) machine coords, or None
        self.safe_clearance_height = config.safe_clearance_height  # configured G54 ceiling, or None
        self.tool_change_height = config.tool_change_height  # roomy manual-change Z, or None
        self._resume_checkpoint_counter = 0
        # Work coordinate system for tube ops. 'G54' (default) = operator zeros G54 to the
        # tube per job (portable); an alternate fixed WCS (e.g. 'G55') is opt-in for a
        # permanently-fixtured jig so its zero persists alongside the flat-work G54 zero.
        self.tube_wcs = config.tube_work_coordinate_system

        # Team information from config
        self.team_number = config.team_number  # FRC team number
        self.team_name = config.team_name  # FRC team name
        self.machine_name = config.machine_name  # Machine name
        self.machine_controller = config.machine_controller  # Controller type
        self.machine_coolant = config.machine_coolant  # Coolant type

        # How deep one pass may cut. Every route applies a material preset, which sets
        # this from the material and then derates it for the tool; the default here only
        # exists so that a caller who generates WITHOUT a preset gets a conservative
        # single-pass depth rather than an AttributeError from inside a toolpath. The
        # flag, not hasattr, is what tells the derating code a preset has been applied -
        # the attribute now always exists.
        self.max_slotting_depth = MATERIAL_PRESETS['plywood']['max_slotting_depth']
        self._material_preset_applied = False

        # Helix entry radius multiplier (applied to tool diameter)
        # Overridden by material presets
        self.helix_radius_multiplier = 0.75  # Default 75% of tool radius

        # Error tracking
        self.errors = []  # Collect validation errors during processing

    def apply_material_preset(self, material: str, machine_id: Optional[str] = None):
        """
        Apply a material preset to set feeds, speeds, and ramp angles.

        Args:
            material: Material name ('plywood', 'aluminum', 'polycarbonate', or custom)
            machine_id: Optional machine ID for machine-specific settings
        """
        import feeds_speeds

        requested_material = material
        model_material = feeds_speeds.canonical_material_key(material)
        is_aluminum = feeds_speeds.is_aluminum_material(material)
        # 6061/6063 are grade identities in the feeds model; team configs intentionally
        # share one conservative aluminum preset. Resolve before TeamConfig can treat an
        # unknown grade spelling as plywood.
        preset_material = 'aluminum' if is_aluminum else material
        preset = self.config.get_material_preset(preset_material, machine_id)

        # The config returns an empty dict when nothing knows this material. That used
        # to fall through to plywood with a printed warning nobody reads on a web
        # request - and a wrong feed table in metal is a broken bit, not a cosmetic
        # problem. Refuse, and say what would have worked.
        if not preset:
            known = self.config.known_material_ids(machine_id)
            raise ValueError(
                f"Unknown material {material!r}. PenguinCAM has no feeds for it, and "
                f"guessing is how bits break. Known materials: {', '.join(known)}. "
                f"To machine something else, add it to the machine's materials block "
                f"in the team config.")

        # A material preset is shop-owned data and may be years older than this code.
        # The old PenguinCAM config template itself persisted 55 IPM / 0.200 in for
        # aluminum, so merely changing the built-in defaults did not protect any team
        # already carrying that file. Clamp the built-in aluminum id to the router
        # safety envelope at the point every normal G-code path shares. Lower team
        # values remain untouched; custom material ids remain fully configurable.
        safety_clamps = []
        if is_aluminum:
            preset = dict(preset)
            for key, ceiling in feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX.items():
                value = preset.get(key)
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = float('nan')
                if not math.isfinite(value) or value <= 0:
                    safety_clamps.append(f"{key} invalid->{ceiling:g}")
                    preset[key] = ceiling
                elif value > ceiling:
                    safety_clamps.append(f"{key} {value:g}->{ceiling:g}")
                    preset[key] = ceiling
                else:
                    preset[key] = value

            try:
                corner_floor = float(preset.get('corner_min_feed_scale', 0.6))
            except (TypeError, ValueError):
                corner_floor = float('nan')
            if not math.isfinite(corner_floor) or not 0.6 <= corner_floor <= 1.0:
                protected_floor = min(1.0, max(0.6, corner_floor)) if math.isfinite(corner_floor) else 0.6
                safety_clamps.append(
                    f"corner_min_feed_scale {preset.get('corner_min_feed_scale')!r}"
                    f"->{protected_floor:g}")
                preset['corner_min_feed_scale'] = protected_floor

            machine_key = machine_id if machine_id in feeds_speeds.MACHINES else 'omio_x8'
            machine = feeds_speeds.MACHINES[machine_key]
            try:
                configured_rpm = float(preset.get('spindle_speed'))
            except (TypeError, ValueError):
                configured_rpm = float('nan')
            if (not math.isfinite(configured_rpm)
                    or not machine['rpm_min'] <= configured_rpm <= machine['rpm_max']):
                safety_clamps.append(
                    f"spindle_speed {preset.get('spindle_speed')!r}->{15000}")
                preset['spindle_speed'] = 15000

        self._material_preset_applied = True
        self._tool_scaling_applied = False
        self.material_name = (feeds_speeds.MATERIALS[model_material]['name']
                              if model_material in ('aluminum_6061', 'aluminum_6063')
                              and str(requested_material).lower() not in
                              ('aluminum', 'aluminium', 'aluminum_tube', 'aluminium_tube')
                              else preset.get('name', str(material).capitalize()))
        self.material_id = model_material or material
        self.machine_preset_id = machine_id    # machine key, for the spindle-power clamp
        if safety_clamps:
            self.feed_scale_note = ('aluminum safety envelope clamped stale or unsafe '
                                    'config: ' + ', '.join(safety_clamps))

        # Preset values are defined in IPM - convert to mm/min if needed
        if self.units == 'mm':
            self.feed_rate = preset['feed_rate'] * 25.4
            self.ramp_feed_rate = preset['ramp_feed_rate'] * 25.4
            self.plunge_rate = preset['plunge_rate'] * 25.4
            self.ramp_start_clearance = preset['ramp_start_clearance'] * 25.4
        else:
            self.feed_rate = preset['feed_rate']
            self.ramp_feed_rate = preset['ramp_feed_rate']
            self.plunge_rate = preset['plunge_rate']
            self.ramp_start_clearance = preset['ramp_start_clearance']

        self.spindle_speed = preset['spindle_speed']
        self.ramp_angle = preset['ramp_angle']
        self.stepover_percentage = preset['stepover_percentage']

        # Max slotting depth (convert to mm if needed)
        if self.units == 'mm':
            self.max_slotting_depth = preset['max_slotting_depth'] * 25.4
        else:
            self.max_slotting_depth = preset['max_slotting_depth']

        # Tab sizes (convert to mm if needed)
        if self.units == 'mm':
            self.tab_width = preset['tab_width'] * 25.4
            self.tab_height = preset['tab_height'] * 25.4
        else:
            self.tab_width = preset['tab_width']
            self.tab_height = preset['tab_height']

        # Helix entry radius multiplier
        self.helix_radius_multiplier = preset['helix_radius_multiplier']

        # Peck drill depth (convert to mm if needed)
        if self.units == 'mm':
            self.peck_drill_depth = preset['peck_drill_depth'] * 25.4
        else:
            self.peck_drill_depth = preset['peck_drill_depth']

        # Corner-slowdown floor (material-aware, dimensionless). Aluminum is force-limited, so a
        # corner does want less feed - but below ~0.6 the chip stops shearing and the edge welds,
        # which is how bits die in 6061. 0.6 is the floor the aluminum safety envelope protects
        # above, and the RPM coordination in scale_feeds_to_tool is computed against it. Softer,
        # heat-limited materials keep even more feed (0.7). Falls back to the __init__ default.
        self.corner_min_feed_scale = preset.get('corner_min_feed_scale', self.corner_min_feed_scale)

        print(f"\nApplied material preset: {preset.get('name', material.capitalize())}")
        if 'description' in preset:
            print(f"  {preset['description']}")
        if self.units == 'mm':
            print(f"  Feed rate: {preset['feed_rate']} IPM ({self.feed_rate:.0f} mm/min)")
            print(f"  Ramp feed rate: {preset['ramp_feed_rate']} IPM ({self.ramp_feed_rate:.0f} mm/min)")
            print(f"  Plunge rate: {preset['plunge_rate']} IPM ({self.plunge_rate:.0f} mm/min)")
            print(f"  Ramp start clearance: {preset['ramp_start_clearance']}\" ({self.ramp_start_clearance:.1f} mm)")
        else:
            print(f"  Feed rate: {self.feed_rate} IPM")
            print(f"  Ramp feed rate: {self.ramp_feed_rate} IPM")
            print(f"  Plunge rate: {self.plunge_rate} IPM")
            print(f"  Ramp start clearance: {self.ramp_start_clearance}\"")
        print(f"  Ramp angle: {self.ramp_angle}°")
        print(f"  Stepover: {self.stepover_percentage*100:.0f}% of tool diameter")
        print(f"  Tab size: {preset['tab_width']}\" x {preset['tab_height']}\" (W x H)")
        # Aluminum safety must not depend on every caller remembering a second method.
        # The public scale method is idempotent, so existing explicit calls remain safe.
        if is_aluminum:
            self.scale_feeds_to_tool()

    #: Preset ids mapped onto the feeds_speeds material table (custom team materials
    #: have no entry there; they get the geometric scaling but no power clamp).
    _FEEDS_MODEL_MATERIALS = {'plywood': 'plywood', 'aluminum': 'aluminum_6063',
                              'aluminum_tube': 'aluminum_6063',
                              'aluminum_6061': 'aluminum_6061',
                              'aluminum_6063': 'aluminum_6063',
                              'polycarbonate': 'polycarbonate', 'hdpe': 'hdpe',
                              'srpp': 'srpp'}

    def scale_feeds_to_tool(self) -> List[str]:
        """Clamp the applied material preset's feeds and per-pass depths to the ACTUAL tool.

        The material presets are the feeds/speeds model evaluated at its 4 mm
        single-flute reference tool and frozen - which is exactly right for the default
        cutter and wrong for every other one. A 1/8 in end mill in aluminum was being
        run at the 4 mm tool's 55 IPM into a 0.2 in deep full-width slot: over its
        scaled chipload AND over its scaled depth at once, which is how small end mills
        snap. (The multi-tool path already derives feeds per tool in tooling.py; this
        brings the same physics to the single-tool flows.)

        Scaling is RELATIVE and only ever downward:
          feed factor  = (D / D_ref) ^ DIAMETER_EXPONENT   capped at 1.0
          depth factor =  D / D_ref                        capped at 1.0
        so the reference tool reproduces the tested preset exactly - including a team's
        own config overrides, which are tuned at that same reference - and a larger
        cutter keeps the tested numbers rather than being scaled up on the strength of
        a model (the same never-raise rule tooling.py follows). A larger cutter does
        get the spindle-power ceiling applied, because that is the one direction where
        bigger is more dangerous: a wide slot at full preset feed can demand more power
        than the spindle has, and a bogged router snaps the tool.

        Call after apply_material_preset. Returns the adjustments made (also kept on
        self.feed_scale_note for the G-code header, so the program tells the operator
        it derated itself and why).
        """
        import feeds_speeds

        if not self._material_preset_applied:
            # No preset applied yet - there is nothing tested to scale down from.
            return []
        if getattr(self, '_tool_scaling_applied', False):
            return list(getattr(self, '_tool_scale_notes', []))

        to_inch = (1.0 / 25.4) if self.units == 'mm' else 1.0
        diameter_in = self.tool_diameter * to_inch
        if diameter_in <= 0:
            return []
        d_ref = feeds_speeds.REFERENCE_TOOL['diameter']
        notes = []

        # High-RPM router slotting needs flute space to get gummy aluminum chips out.
        # The model has always warned about 3/4-flute tools, but the standard workflow
        # never even asked for flute count and still emitted a runnable program. Refuse
        # that known failure mode; use a purpose-made 1- or 2-flute aluminum cutter.
        material_key = (feeds_speeds.canonical_material_key(
            getattr(self, 'material_id', None)) or self._FEEDS_MODEL_MATERIALS.get(
                (getattr(self, 'material_id', None) or '').lower()))
        if material_key:
            flute_cap = feeds_speeds.MATERIALS[material_key].get('feed_flutes_max')
            if flute_cap and self.tool_flutes > flute_cap:
                raise ValueError(
                    f'{self.tool_flutes}-flute cutters are not supported for '
                    f'{feeds_speeds.MATERIALS[material_key]["name"]} on a router. '
                    f'Use a 1- or 2-flute aluminum end mill so chips can evacuate; '
                    f'packed chips weld to the tool and snap it.')

        def _floor_tenth(value: float) -> float:
            # Emitted verbatim as F words; floored so rounding can never nudge a
            # clamped feed back above its limit, and to a sane number of digits.
            return math.floor(value * 10.0) / 10.0

        feed_factor = min(1.0, (diameter_in / d_ref) ** feeds_speeds.DIAMETER_EXPONENT)
        if feed_factor < 1.0 - 1e-9:
            self.feed_rate = _floor_tenth(self.feed_rate * feed_factor)
            self.ramp_feed_rate = _floor_tenth(self.ramp_feed_rate * feed_factor)
            self.plunge_rate = _floor_tenth(self.plunge_rate * feed_factor)
            unit = 'mm/min' if self.units == 'mm' else 'ipm'
            notes.append(f"feed scaled to {self.feed_rate:.1f} {unit} for the "
                         f"{diameter_in:.3f} in tool, from the 4 mm reference")

        # The aluminum ceiling is a proven one-flute feed. With two flutes at the same
        # RPM each tooth receives half the chip, rubs, builds heat, and welds aluminum
        # to the edge. We deliberately do not raise the unproven feed; lower RPM just
        # enough to preserve the material's minimum chipload, but never below the
        # machine's spindle floor.
        if feeds_speeds.is_aluminum_material(material_key):
            minimum = feeds_speeds.MATERIALS[material_key]['chipload_min']
            machine_key = (self.machine_preset_id
                           if self.machine_preset_id in feeds_speeds.MACHINES
                           else 'omio_x8')
            spindle_floor = feeds_speeds.MACHINES[machine_key]['rpm_min']
            base_rpm_ceiling = ((self.feed_rate * to_inch)
                                / (self.tool_flutes * minimum))
            rpm_ceiling = ((self.feed_rate * to_inch) * self.corner_min_feed_scale
                           / (self.tool_flutes * minimum))
            if base_rpm_ceiling < spindle_floor - 1e-9:
                raise ValueError(
                    f'{diameter_in:.3f} in {self.tool_flutes}-flute cutter cannot make '
                    f'the minimum aluminum chip at the {spindle_floor} RPM spindle '
                    f'floor and the protected feed. Use a larger or 1-flute cutter.')
            protected_rpm = max(spindle_floor, min(self.spindle_speed,
                                                   math.floor(rpm_ceiling)))
            if protected_rpm < self.spindle_speed:
                old_rpm = self.spindle_speed
                self.spindle_speed = int(protected_rpm)
                notes.append(f"spindle reduced from {old_rpm} to {self.spindle_speed} RPM "
                             f"for {self.tool_flutes} flutes so straight and corner "
                             f"chipload stay above {minimum:.4f} in/tooth")

        # Pocket corners deliberately run below base_feed. Do not let that force
        # protection cross into rubbing: the lowest emitted F word must still make the
        # minimum chip at the selected RPM/flute count. This can soften or disable the
        # corner slowdown for a 2F cutter whose straight feed is already at the floor;
        # a 1F cutter retains more margin and is therefore still the preferred tool.
        if feeds_speeds.is_aluminum_material(material_key):
            minimum = feeds_speeds.MATERIALS[material_key]['chipload_min']
            min_feed_native = (self.spindle_speed * self.tool_flutes * minimum) / to_inch
            required_corner_scale = min(1.0, min_feed_native / self.feed_rate)
            if required_corner_scale > self.corner_min_feed_scale + 1e-9:
                old_scale = self.corner_min_feed_scale
                self.corner_min_feed_scale = required_corner_scale
                notes.append(
                    f"corner feed floor raised from {old_scale:.2f} to "
                    f"{required_corner_scale:.2f} so corner moves do not rub")

        depth_factor = min(1.0, diameter_in / d_ref)
        if depth_factor < 1.0 - 1e-9:
            self.max_slotting_depth *= depth_factor
            self.peck_drill_depth *= depth_factor
            unit = 'mm' if self.units == 'mm' else 'in'
            notes.append(f"max depth per pass scaled to "
                         f"{self.max_slotting_depth:.3f} {unit}")

        # Spindle-power ceiling. The chipload scaling above never binds for a cutter
        # LARGER than the reference, but power does: cutting power is MRR x unit power,
        # and a wide cutter slotting at full preset feed can ask for more than the
        # spindle delivers - it bogs, grabs, and snaps the tool.
        material_key = (feeds_speeds.canonical_material_key(
            getattr(self, 'material_id', None)) or self._FEEDS_MODEL_MATERIALS.get(
                (getattr(self, 'material_id', None) or '').lower()))
        machine_key = getattr(self, 'machine_preset_id', None)
        if material_key and machine_key in feeds_speeds.MACHINES:
            limit_in = feeds_speeds.max_depth_for_power(
                machine_key, material_key, diameter_in, self.feed_rate * to_inch)
            if limit_in is not None:
                limit = limit_in / to_inch   # back to native units
                if limit < self.max_slotting_depth - 1e-9:
                    self.max_slotting_depth = limit
                    unit = 'mm' if self.units == 'mm' else 'in'
                    notes.append(f"max depth per pass held to {limit:.3f} {unit} "
                                 f"by spindle power")

        # Refuse any remaining chipload violation. A runnable warning is not protection:
        # too little chip rubs/welds aluminum and too much overloads the edge.
        if material_key and self.spindle_speed > 0:
            chipload_min = feeds_speeds.MATERIALS[material_key].get('chipload_min')
            chipload_max = feeds_speeds.MATERIALS[material_key].get('chipload_max')
            chipload = ((self.feed_rate * to_inch)
                        / (self.spindle_speed * self.tool_flutes))
            if chipload_min and chipload < chipload_min:
                raise ValueError(
                    f'chipload {chipload:.4f} is below the material minimum '
                    f'{chipload_min:.4f}; use a larger tool, fewer flutes, or lower RPM.')
            if chipload_max and chipload > chipload_max:
                raise ValueError(
                    f'chipload {chipload:.4f} is above the material maximum '
                    f'{chipload_max:.4f}; raise RPM or lower feed.')

        if notes:
            existing = getattr(self, 'feed_scale_note', None)
            scaled = '; '.join(notes)
            self.feed_scale_note = f"{existing}; {scaled}" if existing else scaled
            for note in notes:
                print(f"  Tool-scaled: {note}")
        self._tool_scale_notes = list(notes)
        self._tool_scaling_applied = True
        return notes

    def apply_max_pass_depth(self, depth: float) -> None:
        """Operator ceiling on the depth of one contour pass, in this pp's units.

        Contour cuts (perimeter, pockets, interior cutouts) already split into
        ceil(total / max_slotting_depth) passes; this lowers that per-pass depth so the
        cut takes MORE, shallower passes - the standard way to baby a fragile or
        multi-flute cutter, trading cycle time for tool life. Clamp-only: a value
        above what is already in effect changes nothing, because the automatic value
        is itself a safety ceiling (tested preset, tool scaling, spindle power) and an
        operator setting must never override a limit upward.
        """
        if not (isinstance(depth, (int, float)) and math.isfinite(depth) and depth > 0):
            raise ValueError(f'Max depth per pass must be a positive number, '
                             f'got {depth!r}.')
        if not hasattr(self, 'max_slotting_depth') or depth >= self.max_slotting_depth:
            return
        self.max_slotting_depth = depth
        unit = 'mm' if self.units == 'mm' else 'in'
        note = f"max depth per pass limited to {depth:.3f} {unit} by operator"
        existing = getattr(self, 'feed_scale_note', None)
        self.feed_scale_note = f"{existing}; {note}" if existing else note
        print(f"  Operator limit: {note}")

    def validate_aluminum_cutting_parameters(self) -> None:
        """Final, post-override guard for a runnable aluminum milling program."""
        import feeds_speeds

        material_key = feeds_speeds.canonical_material_key(
            getattr(self, 'material_id', None))
        if not feeds_speeds.is_aluminum_material(material_key) or self.is_dry_run:
            return
        values = {
            'spindle speed': self.spindle_speed,
            'cutting feed': self.feed_rate,
            'ramp feed': self.ramp_feed_rate,
            'plunge feed': self.plunge_rate,
            'depth per pass': self.max_slotting_depth,
        }
        for name, value in values.items():
            if (not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value <= 0):
                raise ValueError(f'Aluminum {name} must be a positive finite number, '
                                 f'got {value!r}.')

        machine_key = (self.machine_preset_id
                       if self.machine_preset_id in feeds_speeds.MACHINES
                       else 'omio_x8')
        machine = feeds_speeds.MACHINES[machine_key]
        if not machine['rpm_min'] <= self.spindle_speed <= machine['rpm_max']:
            raise ValueError(
                f'Aluminum spindle speed {self.spindle_speed:g} RPM is outside '
                f'{machine["name"]} limits {machine["rpm_min"]:g}-'
                f'{machine["rpm_max"]:g} RPM.')

        if getattr(self, 'tool_has_drill_point', False):
            plunge_ipm = self.plunge_rate * ((1.0 / 25.4) if self.units == 'mm' else 1.0)
            if plunge_ipm > feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['plunge_rate'] + 1e-9:
                raise ValueError(
                    f'Aluminum drill feed {plunge_ipm:.1f} IPM exceeds the protected '
                    f'{feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX["plunge_rate"]:.1f} IPM.')
            return
        flute_cap = feeds_speeds.MATERIALS[material_key]['feed_flutes_max']
        if self.tool_flutes > flute_cap:
            raise ValueError(
                f'{self.tool_flutes}-flute cutters are not supported in aluminum on '
                f'this router; use a 1- or 2-flute aluminum end mill.')

        to_inch = (1.0 / 25.4) if self.units == 'mm' else 1.0
        diameter_in = self.tool_diameter * to_inch
        factor = min(1.0, (diameter_in / feeds_speeds.REFERENCE_TOOL['diameter'])
                     ** feeds_speeds.DIAMETER_EXPONENT)
        for attr, label in (('feed_rate', 'cutting feed'),
                            ('ramp_feed_rate', 'ramp feed'),
                            ('plunge_rate', 'plunge feed')):
            ceiling_key = {'feed_rate': 'feed_rate',
                           'ramp_feed_rate': 'ramp_feed_rate',
                           'plunge_rate': 'plunge_rate'}[attr]
            actual_ipm = getattr(self, attr) * to_inch
            ceiling = feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX[ceiling_key] * factor
            if actual_ipm > ceiling + 1e-9:
                raise ValueError(
                    f'Aluminum {label} {actual_ipm:.1f} IPM exceeds the '
                    f'diameter-scaled ceiling {ceiling:.1f} IPM.')

        chipload = ((self.feed_rate * to_inch)
                    / (self.spindle_speed * self.tool_flutes))
        minimum = feeds_speeds.MATERIALS[material_key]['chipload_min']
        maximum = feeds_speeds.MATERIALS[material_key]['chipload_max']
        if not minimum <= chipload <= maximum:
            raise ValueError(
                f'Aluminum chipload {chipload:.4f} in/tooth is outside the protected '
                f'{minimum:.4f}-{maximum:.4f} range.')
        corner_chipload = chipload * self.corner_min_feed_scale
        if corner_chipload + 1e-12 < minimum:
            raise ValueError(
                f'Aluminum corner chipload {corner_chipload:.4f} in/tooth is below '
                f'the {minimum:.4f} minimum.')

    def apply_twist_drill_feeds(self) -> None:
        """Apply the drilling model to a generated tube hole pattern."""
        import feeds_speeds

        material_key = (feeds_speeds.canonical_material_key(
            getattr(self, 'material_id', None)) or 'plywood')
        machine_key = (self.machine_preset_id
                       if self.machine_preset_id in feeds_speeds.MACHINES
                       else 'omio_x8')
        result = feeds_speeds.calculate_drill_feeds(
            machine_key, material_key, {'diameter': self.tool_diameter})
        to_native = 25.4 if self.units == 'mm' else 1.0
        plunge_ipm = result['plunge_feed']
        if feeds_speeds.is_aluminum_material(material_key):
            plunge_ipm = min(
                plunge_ipm,
                feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['plunge_rate'])
        self.spindle_speed = int(result['rpm'])
        self.feed_rate = plunge_ipm * to_native
        self.ramp_feed_rate = plunge_ipm * to_native
        self.plunge_rate = plunge_ipm * to_native
        self.peck_drill_depth = self.tool_diameter / 3.0

    def _distance_2d(self, p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
        """Calculate 2D Euclidean distance between two points"""
        return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)

    def _get_polygon_center(self, polygon) -> Tuple[float, float]:
        """
        Get approximate center of a Shapely polygon from its bounding box.

        Args:
            polygon: Shapely Polygon object

        Returns:
            (center_x, center_y) tuple
        """
        bounds = polygon.bounds  # (minx, miny, maxx, maxy)
        center_x = (bounds[0] + bounds[2]) / 2
        center_y = (bounds[1] + bounds[3]) / 2
        return center_x, center_y

    def _add_error(self, error_msg: str):
        """
        Add an error message to the error list and print it.

        Args:
            error_msg: Error message to add
        """
        print(f"  ❌ ERROR: {error_msg}")
        self.errors.append(error_msg)

    def _generate_pause_and_park_gcode(self, title: str, instructions: List[str],
                                       safe_z: float = None, *, tool_change: bool = False,
                                       resume_checkpoint: str = None,
                                       resume_description: str = None) -> List[str]:
        """
        Generate G-code for a safe pause-and-restart sequence with operator instructions.

        This standardized pause sequence:
        1. Moves to safe Z height (machine coordinates)
        2. Parks at machine park position
        3. Turns off air blast and spindle
        4. Displays operator instructions
        5. Pauses program (M0) waiting for operator to press CYCLE START
        6. Restarts spindle and air blast after CYCLE START
        7. Dwells for spindle spin-up

        Args:
            title: Title for the pause section (e.g., "PAUSE FOR TUBE FLIP")
            instructions: List of instruction lines to display to operator

        Returns:
            List of G-code lines for the complete pause-and-restart sequence
        """
        gcode = []
        gcode.append('')
        gcode.append(f'( === {title} === )')
        # Callers with a taller safe height (e.g. tube facing must clear the full tube)
        # pass safe_z. A manual tool change gets its own roomy work-coordinate height
        # when no verified G53 park exists. If a G53 park does exist, retract only to the
        # ordinary safe plane first; _park_gcode then raises in machine coordinates and
        # moves the gantry clear of the work.
        if safe_z is not None:
            z = safe_z
        elif tool_change and not self.park_position:
            z = self._tool_change_safe_z()
        else:
            z = self._safe_z()
        gcode.append(f'G0 Z{z:.4f}  ; Safe Z clearance')
        gcode.extend(self._park_gcode('Park'))  # G53 park only if configured
        coolant_off = self._coolant_off_gcode()
        if coolant_off:
            gcode.append(coolant_off)
        gcode.append('M5  ; Spindle off')
        gcode.append('G4 P5.0  ; 5 second dwell')
        gcode.append('')
        gcode.append('( *** OPERATOR ACTION REQUIRED *** )')
        for instruction in instructions:
            gcode.append(f'( {instruction} )')
        gcode.append('( Press CYCLE START to continue )')
        gcode.append('M0  ; Program pause')
        gcode.append('')
        if resume_checkpoint:
            checkpoint = sanitize_comment(resume_checkpoint, 'TC')
            description = sanitize_comment(resume_description or title, 'tool change')
            gcode.append(f'( === RESUME CHECKPOINT {checkpoint} - {description} === )')
            gcode.append('( Standalone resume: verify machine referenced, G54 X and Y unchanged, )')
            gcode.append(
                f'( correct tool installed, and G54 Z zeroed to {self.z_zero_surface()}, not G92 )')
            gcode.extend(self._resume_state_gcode())
        else:
            gcode.append('( === RESTART AFTER PAUSE === )')
            gcode.append('G90  ; Ensure absolute positioning mode')
            gcode.extend(self._spindle_start_gcode())
            coolant_on = None if self.is_dry_run else self._coolant_on_gcode()
            if coolant_on:
                gcode.append(coolant_on)
        # Lift before the next feature moves in XY. The pre-pause retract left Z safe,
        # but the operator has just had their hands in the envelope to flip or fixture the
        # work and jogging Z is the normal thing to do while there. Resuming into a
        # lateral rapid at whatever height they left drags the tool across the part - the
        # same hazard the program start already guards against.
        if safe_z is not None:
            gcode.append(f'G0 Z{safe_z:.4f}  ; Retract before any XY move after the pause')
        gcode.append('')
        # Tool resumes at safe height after the pause; the next feature must rapid down to
        # the clearance plane before its slow plunge feed (see _approach_ramp_start).
        self._pending_clearance_rapid = True
        return gcode

    def _next_resume_checkpoint(self) -> str:
        """Return a stable, visible checkpoint id for this generated program."""
        self._resume_checkpoint_counter += 1
        return f'TC{self._resume_checkpoint_counter:02d}'

    def _tool_change_safe_z(self) -> float:
        """Roomy work-coordinate Z for changing a manual tool.

        The configured value is a physical height over the sacrifice board; ``z_shift``
        expresses that same height in the selected G54 datum. Never descend below the
        ordinary collision-safe retract. A bad value is refused instead of becoming an
        unexpected machine move.
        """
        if self.tool_change_height is None:
            return self._safe_z()
        try:
            height = float(self.tool_change_height)
        except (TypeError, ValueError) as exc:
            raise ValueError('tool_change_height must be a number of inches') from exc
        if not math.isfinite(height) or height <= 0:
            raise ValueError('tool_change_height must be a positive finite number of inches')
        machine_z = getattr(self.config, 'machine_z_max', None)
        if machine_z and height > machine_z:
            raise ValueError(
                f'tool_change_height {height:.3f} in exceeds the configured machine Z '
                f'travel of {machine_z:.3f} in')
        return max(self._safe_z(), height + self.z_shift)

    def _resume_state_gcode(self) -> List[str]:
        """A complete modal reset at a tool-boundary restart point.

        These lines are intentionally sufficient when they are the first executable
        block in a standalone resume file. No prior G-code state is trusted.
        """
        lines = [
            'G90 G94 G91.1 G40 G49 G17  ; Reset positioning and cutting modes',
            'G20  ; Inches' if self.units == 'inch' else 'G21  ; Millimeters',
            'G92.1  ; Cancel any temporary coordinate offset',
            'G54  ; Restore job work coordinate system',
            f'G0 Z{self._safe_z():.4f}  ; Safe Z before resumed XY motion',
        ]
        lines.extend(self._spindle_start_gcode())
        coolant_on = None if self.is_dry_run else self._coolant_on_gcode()
        if coolant_on:
            lines.append(coolant_on)
        return lines

    def _parse_layer_depth(self, layer_name: str) -> Optional[float]:
        """
        Parse Z depth from layer name (e.g., "Z_-0p250" -> -0.25, "Z_0p000" -> 0)
        Returns None if layer name doesn't match the expected format
        """
        match = re.match(r'^Z_(-?\d+)p(\d+)$', layer_name)
        if not match:
            return None

        is_negative = match.group(1).startswith('-')
        int_part = int(match.group(1))
        frac_part = int(match.group(2))
        frac_value = frac_part / (10 ** len(match.group(2)))

        if is_negative:
            return int_part - frac_value
        else:
            return int_part + frac_value

    #: $INSUNITS codes this cares about. Every other value (0 = unitless, and the
    #: exotic ones: metres, feet, microns) is not worth second-guessing.
    _INSUNITS_NAMES = {1: ('inch', 'inches'), 4: ('mm', 'millimetres')}

    def _check_header_units(self, doc) -> None:
        """Cross-check the drawing's own declared units against the job's.

        $INSUNITS says what the drawing thinks its numbers mean. If it contradicts the
        units this job was set up in, one of the two is wrong by a factor of 25.4 -
        every coordinate, every thickness, every feed - and nothing else in the pipeline
        would ever notice. So it is worth saying out loud.

        WARN, never auto-convert. The header is unreliable in the wild: plenty of
        exporters leave it at 0 or set it to whatever the template had, so trusting it
        enough to rescale the geometry would break more parts than it saved.
        """
        try:
            code = int(doc.header.get('$INSUNITS', 0) or 0)
        except (TypeError, ValueError):
            return
        named = self._INSUNITS_NAMES.get(code)
        if named is None or named[0] == self.units:
            return
        drawing_units = named[1]
        job_units = 'inches' if self.units == 'inch' else 'millimetres'
        message = (
            f'The drawing says its units are {drawing_units} ($INSUNITS), but this job '
            f'is set up in {job_units}. If the drawing is right, every dimension in the '
            f'program is off by a factor of 25.4. Check the part size in the preview '
            f'before you cut, and re-export or change the job units if it is wrong.')
        print(f"  WARNING: {message}")
        self.geometry_warnings.append(message)

    def load_dxf(self, filename: str):
        """Load DXF file and extract geometry, organized by layer if multi-layer DXF"""
        print(f"Loading {filename}...")
        # Advice raised while READING the drawing, kept apart from self.warnings, which
        # every generate_* entry point clears before it builds a toolpath.
        self.geometry_warnings = []
        self.open_chains = []
        doc = ezdxf.readfile(filename)
        msp = doc.modelspace()
        self._check_header_units(doc)

        # Check for multi-layer structure
        layers_with_depths = {}
        for layer in doc.layers:
            depth = self._parse_layer_depth(layer.dxf.name)
            if depth is not None:
                layers_with_depths[layer.dxf.name] = depth

        if layers_with_depths:
            print(f"Detected multi-layer DXF with {len(layers_with_depths)} depth layers")
            self._load_multilayer_dxf(doc, msp, layers_with_depths)
        else:
            print("Processing as single-layer DXF")
            self._load_singlelayer_dxf(msp)

    def _load_singlelayer_dxf(self, msp):
        """Load geometry from single-layer DXF (existing logic)"""
        self.layer_data = None  # Mark as single-layer

        # Extract circles (holes)
        self.circles = []
        for entity in msp.query('CIRCLE'):
            center = (entity.dxf.center.x, entity.dxf.center.y)
            radius = entity.dxf.radius
            self.circles.append({'center': center, 'radius': radius, 'diameter': radius * 2})

        # Initialize geometry lists for transform_coordinates compatibility
        self.lines = []  # Individual lines (converted to polylines)
        self.arcs = []   # Individual arcs (converted to polylines)
        self.splines = []  # Individual splines (converted to polylines)

        # Extract polylines and lines (boundaries/pockets)
        self.polylines = []
        
        # Method 1: Look for LWPOLYLINE entities. polyline_points flattens any bulge
        # arcs - without it a slot with semicircular ends loaded as a rectangle.
        for entity in msp.query('LWPOLYLINE'):
            points = dxf_geometry.polyline_points(entity)
            if entity.closed and len(points) > 2:
                self.polylines.append(points)

        # Method 2: Look for POLYLINE entities
        for entity in msp.query('POLYLINE'):
            if entity.is_2d_polyline:
                points = dxf_geometry.polyline_points(entity)
                if entity.is_closed and len(points) > 2:
                    self.polylines.append(points)
        
        # Method 3: Collect individual LINE, ARC, SPLINE entities and try to form closed paths
        # This is needed for Onshape exports which use individual entities
        lines = list(msp.query('LINE'))
        arcs = list(msp.query('ARC'))
        splines = list(msp.query('SPLINE'))
        # Onshape uses ELLIPSE entities for curved perimeter transitions/fillets. The
        # shared stitcher handles both arcs (chained into a boundary) and full ellipses
        # (standalone closed loops), so pass them all through.
        ellipses = list(msp.query('ELLIPSE'))

        # Also collect unclosed LWPOLYLINEs - they may be part of a perimeter that needs stitching
        unclosed_lwpolylines = []
        for entity in msp.query('LWPOLYLINE'):
            if not entity.closed and len(list(entity.get_points('xy'))) > 1:
                unclosed_lwpolylines.append(entity)

        if lines or arcs or splines or unclosed_lwpolylines or ellipses:
            print(f"Found {len(lines)} lines, {len(arcs)} arcs, {len(splines)} splines, {len(ellipses)} ellipses, {len(unclosed_lwpolylines)} unclosed polylines - attempting to form closed paths...")
            closed_paths = self._chain_entities_to_paths(lines, arcs, splines, unclosed_lwpolylines, ellipses)
            self.polylines.extend(closed_paths)
        
        print(f"Found {len(self.circles)} circles and {len(self.polylines)} closed paths")

    def _path_as_circle(self, coords):
        """If a closed boundary path is circular, return its circle dict, else None.

        The Onshape 2.5D export represents everything as HATCH solid regions, so a
        drilled hole arrives as a many-sided circular boundary path, not a CIRCLE
        entity. Left as a polyline it gets machined as a pocket, which fails when the
        hole is barely larger than the tool (no room to spiral-clear). Recognizing it
        as a circle routes it through the hole classifier, which already picks the
        right strategy by size: peck-drill (tiny), helical+spiral (medium), or
        contour (large through-holes). Only the bottom-face/through path consumes
        these circles; blind pockets at depth layers are machined from `polygons`,
        which are built from circles+polylines identically, so this reclassification
        does not change how depth-layer pockets are cut.

        Returns a circle dict ({'center','radius','diameter'}) or None.
        """
        if len(coords) < 8:  # too few points to be a tessellated circle
            return None
        try:
            poly = Polygon(coords)
        except Exception:
            return None
        if not poly.is_valid or poly.is_empty or poly.length == 0:
            return None
        # Isoperimetric quotient: 1.0 for a circle, ~0.95 octagon, ~0.79 square. On its
        # own it is not enough - a stadium up to about 1.3:1 clears 0.97, so a
        # 0.20 x 0.26 adjustment slot was machined as a 0.235 round hole at its
        # centroid. A tessellated true circle measures ~0.998, so the bar can be much
        # higher without losing any real hole.
        circularity = 4 * math.pi * poly.area / (poly.length ** 2)
        if circularity < 0.99:
            return None
        # ...and every vertex has to be the same distance from the middle. This is what
        # separates a circle from a short slot: the slot's radius swings from its half
        # width at the flats to its half length at the ends, while a tessellated circle
        # holds its radius to a fraction of a percent.
        centroid = poly.centroid
        radii = [math.hypot(x - centroid.x, y - centroid.y) for x, y in coords]
        mean_radius = sum(radii) / len(radii)
        if mean_radius <= 0:
            return None
        if any(abs(r - mean_radius) > 0.015 * mean_radius for r in radii):
            return None
        diameter = 2 * math.sqrt(poly.area / math.pi)
        return {'center': (centroid.x, centroid.y),
                'radius': diameter / 2, 'diameter': diameter}

    def _load_multilayer_dxf(self, doc, msp, layers_with_depths):
        """Load geometry from multi-layer DXF, organized by depth"""
        # Initialize geometry lists for transform_coordinates compatibility
        self.lines = []
        self.arcs = []
        self.splines = []

        # Store layer information for multi-pass processing
        self.layer_data = {}

        # Sort layers by depth (shallowest first, but we'll process deepest first except perimeter)
        sorted_layers = sorted(layers_with_depths.items(), key=lambda x: x[1], reverse=True)

        for layer_name, depth in sorted_layers:
            print(f"  Processing layer {layer_name} (Z={depth:.4f}\")")

            # Extract entities for this layer
            layer_circles = []
            layer_polylines = []

            # PRIORITY 1: Extract HATCH entities (solid regions from new format)
            hatch_count = 0
            for entity in msp.query('HATCH'):
                if entity.dxf.layer == layer_name:
                    try:
                        # Each HATCH has multiple boundary paths
                        for path in entity.paths:
                            if hasattr(path, 'vertices') and path.vertices:
                                # Polyline path, bulge arcs flattened - a HATCH vertex
                                # carries a bulge just as an LWPOLYLINE one does.
                                coords = dxf_geometry.hatch_path_points(path)
                                if len(coords) >= 3:
                                    # Circular boundaries are holes, not pockets -
                                    # recover them as circles so the hole classifier
                                    # (peck/helical/contour by size) handles them
                                    # (see _path_as_circle).
                                    circle = self._path_as_circle(coords)
                                    if circle:
                                        layer_circles.append(circle)
                                    else:
                                        layer_polylines.append(coords)
                                    hatch_count += 1
                    except Exception as e:
                        print(f"      Warning: Could not parse HATCH entity: {e}")

            if hatch_count > 0:
                print(f"    Extracted {hatch_count} regions from HATCH entities (solid format)")

            # FALLBACK: Extract circles and polylines (old stroke format)
            # Only process if no HATCH entities found
            if hatch_count == 0:
                # Extract circles from this layer
                for entity in msp.query('CIRCLE'):
                    if entity.dxf.layer == layer_name:
                        center = (entity.dxf.center.x, entity.dxf.center.y)
                        radius = entity.dxf.radius
                        layer_circles.append({'center': center, 'radius': radius, 'diameter': radius * 2})

                # Extract polylines from this layer (same logic as single-layer)
                for entity in msp.query('LWPOLYLINE'):
                    if entity.dxf.layer == layer_name:
                        points = dxf_geometry.polyline_points(entity)
                        if entity.closed and len(points) > 2:
                            layer_polylines.append(points)

                for entity in msp.query('POLYLINE'):
                    if entity.is_2d_polyline and entity.dxf.layer == layer_name:
                        points = dxf_geometry.polyline_points(entity)
                        if entity.is_closed and len(points) > 2:
                            layer_polylines.append(points)

            # Collect individual entities for path stitching
            lines = [e for e in msp.query('LINE') if e.dxf.layer == layer_name]
            arcs = [e for e in msp.query('ARC') if e.dxf.layer == layer_name]
            splines = [e for e in msp.query('SPLINE') if e.dxf.layer == layer_name]
            unclosed_lwpolylines = [e for e in msp.query('LWPOLYLINE')
                                   if e.dxf.layer == layer_name and not e.closed
                                   and len(list(e.get_points('xy'))) > 1]
            ellipses = [e for e in msp.query('ELLIPSE') if e.dxf.layer == layer_name]

            if lines or arcs or splines or unclosed_lwpolylines or ellipses:
                closed_paths = self._chain_entities_to_paths(lines, arcs, splines, unclosed_lwpolylines, ellipses)
                layer_polylines.extend(closed_paths)

            # Convert geometry to Shapely Polygons for unified representation
            polygons = self._convert_to_shapely_polygons(layer_circles, layer_polylines)

            self.layer_data[layer_name] = {
                'depth': depth,
                'polygons': polygons,
                # Keep old format temporarily for compatibility during migration
                'circles': layer_circles,
                'polylines': layer_polylines
            }

            print(f"    Found {len(layer_circles)} circles and {len(layer_polylines)} closed paths at this depth")
            print(f"    Converted to {len(polygons)} Shapely Polygon(s)")

        # For compatibility, set top-level circles/polylines to COPIES of the shallowest layer
        # (This allows classify_loops to work as-is for single-layer operations)
        # IMPORTANT: Use copy() to avoid double-transformation when transform_coordinates is called
        if self.layer_data:
            top_layer = sorted_layers[0][0]  # Shallowest layer
            self.circles = [circle.copy() for circle in self.layer_data[top_layer]['circles']]
            self.polylines = [polyline[:] for polyline in self.layer_data[top_layer]['polylines']]

            # Derive stock thickness from the CAD layers themselves. In the Z convention
            # (Z=0 sacrifice board, top face at Z=thickness), the deepest layer depth IS
            # the material thickness. This makes 2.5D authoritative from geometry, so the
            # wizard doesn't ask the user for thickness in 2.5D mode. Also refresh the
            # thickness-derived heights that __init__ computed from the (placeholder) arg.
            max_depth = max((info['depth'] for info in self.layer_data.values()), default=0.0)
            if max_depth > 0:
                self.material_thickness = max_depth
                self._apply_z_frame()
                print(f"  Derived stock thickness from CAD layers: {max_depth:.4f}\"")

    #: A bridged gap bigger than this moves a real edge, so the operator hears about it.
    #: Below it, the difference is CAD endpoint noise and saying so would be chatter.
    GAP_REPORT_THRESHOLD = 0.02

    def _chain_entities_to_paths(self, lines, arcs, splines, unclosed_polylines=None, ellipses=None):
        """Stitch individual LINE/ARC/ELLIPSE/SPLINE and unclosed LWPOLYLINE entities into
        closed boundary paths. Delegates to the shared dxf_geometry stitcher (also used by
        the 2.5D DXF reconstruction) so sampling + stitching live in exactly one place.

        A chain that does NOT close is recorded rather than dropped in silence. That
        silence is what let a part with an unclosed outer profile promote its biggest
        POCKET to perimeter and profile through the middle of the part, with tabs.
        """
        def note_open(coords, gap):
            self.open_chains.append({'coords': coords, 'gap': gap})
            start, end = coords[0], coords[-1]
            self.geometry_warnings.append(
                f'A boundary in the drawing does not close: a {gap:.4f}" gap between '
                f'({start[0]:.3f}, {start[1]:.3f}) and ({end[0]:.3f}, {end[1]:.3f}). '
                f'That outline was not machined.')

        def note_weld(coords, gap):
            if gap <= self.GAP_REPORT_THRESHOLD:
                return
            start = coords[0]
            self.geometry_warnings.append(
                f'A boundary was closed across a {gap:.4f}" gap near '
                f'({start[0]:.3f}, {start[1]:.3f}). The cut edge there is PenguinCAM\'s '
                f'straight line, not something you drew.')

        return entities_to_closed_paths(
            lines=lines, arcs=arcs, ellipses=ellipses or [],
            splines=splines, polylines=unclosed_polylines or [],
            on_open_loop=note_open, on_welded_gap=note_weld)

    def _mirror_geometry_x(self):
        """Mirror all loaded geometry across the X axis (x -> -x), for a part 'flipped
        over' to machine its reverse side. Applied before rotate/normalize so toolpaths
        are regenerated fresh from the mirrored geometry (preserving helical/spiral/climb
        safety) rather than mangling generated G-code. Splines/arcs are sampled into
        polylines at load, so mirroring the point geometry is exact for the output."""
        for c in self.circles:
            c['center'] = (-c['center'][0], c['center'][1])
        for ln in self.lines:
            ln['start'] = (-ln['start'][0], ln['start'][1])
            ln['end'] = (-ln['end'][0], ln['end'][1])
        for a in self.arcs:
            a['center'] = (-a['center'][0], a['center'][1])
        self.polylines = [[(-x, y) for (x, y) in pl] for pl in self.polylines]
        if self.layer_data:
            for info in self.layer_data.values():
                for c in info.get('circles', []):
                    c['center'] = (-c['center'][0], c['center'][1])
                info['polylines'] = [[(-x, y) for (x, y) in pl] for pl in info.get('polylines', [])]
                if 'polygons' in info:
                    info['polygons'] = [affinity.scale(p, xfact=-1, yfact=1, origin=(0, 0))
                                        for p in info['polygons']]

    def transform_coordinates(self, origin_corner: str, rotation_angle: int,
                              placement_offset: Tuple[float, float] = (0.0, 0.0),
                              enforce_bounds: bool = True, mirror: bool = False):
        """
        Transform all coordinates based on origin corner and rotation.

        Args:
            origin_corner: 'bottom-left', 'bottom-right', 'top-left', 'top-right'
            rotation_angle: 0, 90, 180, 270 degrees clockwise
            placement_offset: (dx, dy) added after the corner is normalized to (0,0).
                Used by multi-part job layout to place this part on a shared sheet.
                Defaults to (0,0), which leaves single-part output unchanged.
            enforce_bounds: when True (default, single-part), error if the part is
                larger than the machine envelope. Multi-part jobs pass False and rely
                on job-level validation (validate_job_layout) against the stock sheet.
            mirror: when True, mirror the part across X (flip it over) before rotating
                and normalizing. The corner-normalize step re-places it into positive
                space, so it composes with rotation and placement_offset.
        """
        # Flip-over mirror is applied first, then rotate/normalize proceed on the
        # mirrored geometry.
        if mirror:
            self._mirror_geometry_x()

        # First, find bounding box of ALL entities
        all_x = []
        all_y = []
        
        # Collect all X,Y coordinates
        for circle in self.circles:
            cx, cy = circle['center']
            r = circle.get('radius') or (circle.get('diameter', 0) / 2)
            # Include circle bounds (center ± radius)
            all_x.extend([cx - r, cx + r])
            all_y.extend([cy - r, cy + r])
        
        for line in self.lines:
            all_x.extend([line['start'][0], line['end'][0]])
            all_y.extend([line['start'][1], line['end'][1]])
        
        for arc in self.arcs:
            all_x.append(arc['center'][0])
            all_y.append(arc['center'][1])
            # Approximate arc bounds
            radius = arc['radius']
            all_x.extend([arc['center'][0] - radius, arc['center'][0] + radius])
            all_y.extend([arc['center'][1] - radius, arc['center'][1] + radius])
        
        for spline in self.splines:
            points = sample_spline(spline)
            for x, y in points:
                all_x.append(x)
                all_y.append(y)

        for polyline in self.polylines:
            for x, y in polyline:
                all_x.append(x)
                all_y.append(y)

        # Also collect coordinates from multi-layer geometry if present
        if self.layer_data:
            for layer_name, layer_info in self.layer_data.items():
                for circle in layer_info['circles']:
                    cx, cy = circle['center']
                    r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                    # Include circle bounds (center ± radius)
                    all_x.extend([cx - r, cx + r])
                    all_y.extend([cy - r, cy + r])
                for polyline in layer_info['polylines']:
                    for x, y in polyline:
                        all_x.append(x)
                        all_y.append(y)

        if not all_x or not all_y:
            print("Warning: No geometry found for transformation")
            return
        
        minX, maxX = min(all_x), max(all_x)
        minY, maxY = min(all_y), max(all_y)
        centerX = (minX + maxX) / 2
        centerY = (minY + maxY) / 2
        
        print(f"\nApplying transformation:")
        print(f"  Origin corner: {origin_corner}")
        print(f"  Rotation: {rotation_angle}°")
        print(f"  Original bounds: X=[{minX:.3f}, {maxX:.3f}], Y=[{minY:.3f}, {maxY:.3f}]")
        
        # Step 1: Rotate around center if needed
        if rotation_angle != 0:
            angle_rad = -math.radians(rotation_angle)  # Negative for clockwise
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)
            
            def rotate_point(x, y):
                # Translate to origin
                x -= centerX
                y -= centerY
                # Rotate
                new_x = x * cos_a - y * sin_a
                new_y = x * sin_a + y * cos_a
                # Translate back
                new_x += centerX
                new_y += centerY
                return new_x, new_y
            
            # Rotate all entities
            for circle in self.circles:
                circle['center'] = rotate_point(*circle['center'])
            
            for line in self.lines:
                line['start'] = rotate_point(*line['start'])
                line['end'] = rotate_point(*line['end'])
            
            for arc in self.arcs:
                arc['center'] = rotate_point(*arc['center'])
                # Update angles for rotation
                arc['start_angle'] = (arc['start_angle'] - rotation_angle) % 360
                arc['end_angle'] = (arc['end_angle'] - rotation_angle) % 360
            
            for spline in self.splines:
                # For splines, we need to recreate - for now, skip
                # This is a limitation but rarely matters for FRC parts
                pass

            for i, polyline in enumerate(self.polylines):
                self.polylines[i] = [rotate_point(x, y) for x, y in polyline]

            # Also transform multi-layer geometry if present
            if self.layer_data:
                for layer_name, layer_info in self.layer_data.items():
                    # Rotate circles
                    for circle in layer_info['circles']:
                        circle['center'] = rotate_point(*circle['center'])

                    # Rotate polylines
                    for i, polyline in enumerate(layer_info['polylines']):
                        layer_info['polylines'][i] = [rotate_point(x, y) for x, y in polyline]

                    # Rotate Shapely Polygons. NOTE: rotation_angle is degrees CLOCKWISE
                    # (matching rotate_point above, which uses -radians(rotation_angle)), but
                    # shapely's affinity.rotate treats a positive angle as COUNTER-clockwise.
                    # Negate so polygons rotate the SAME direction as circles/polylines -
                    # otherwise a partial-depth pocket ends up mirrored 180deg from the rest
                    # of the part (overlapping other features).
                    if 'polygons' in layer_info:
                        rotated_polygons = []
                        for poly in layer_info['polygons']:
                            # Rotate around center point (clockwise, to match rotate_point)
                            rotated = affinity.rotate(poly, -rotation_angle, origin=(centerX, centerY), use_radians=False)
                            rotated_polygons.append(rotated)
                        layer_info['polygons'] = rotated_polygons

            # Recalculate bounds after rotation
            all_x = []
            all_y = []
            for circle in self.circles:
                cx, cy = circle['center']
                r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                all_x.extend([cx - r, cx + r])
                all_y.extend([cy - r, cy + r])
            for line in self.lines:
                all_x.extend([line['start'][0], line['end'][0]])
                all_y.extend([line['start'][1], line['end'][1]])
            for arc in self.arcs:
                all_x.append(arc['center'][0])
                all_y.append(arc['center'][1])
                radius = arc['radius']
                all_x.extend([arc['center'][0] - radius, arc['center'][0] + radius])
                all_y.extend([arc['center'][1] - radius, arc['center'][1] + radius])

            for polyline in self.polylines:
                for x, y in polyline:
                    all_x.append(x)
                    all_y.append(y)

            # Include multi-layer geometry in bounds calculation
            if self.layer_data:
                for layer_name, layer_info in self.layer_data.items():
                    for circle in layer_info['circles']:
                        cx, cy = circle['center']
                        r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                        all_x.extend([cx - r, cx + r])
                        all_y.extend([cy - r, cy + r])
                    for polyline in layer_info['polylines']:
                        for x, y in polyline:
                            all_x.append(x)
                            all_y.append(y)

            minX, maxX = min(all_x), max(all_x)
            minY, maxY = min(all_y), max(all_y)
        
        # Step 2: Translate based on origin corner
        # We want the selected corner to become (0, 0)
        if origin_corner == 'bottom-left':
            offsetX, offsetY = -minX, -minY
        elif origin_corner == 'bottom-right':
            offsetX, offsetY = -maxX, -minY
        elif origin_corner == 'top-left':
            offsetX, offsetY = -minX, -maxY
        elif origin_corner == 'top-right':
            offsetX, offsetY = -maxX, -maxY
        else:
            # No else meant offsetX was simply never assigned, and the next line raised
            # UnboundLocalError - a 500 reading "cannot access local variable 'offsetX'"
            # for what is really "I don't know that corner". Every other corner-shaped
            # input in this program is whitelisted; this one wasn't.
            raise ValueError(
                f"Unknown origin corner {origin_corner!r}: expected 'bottom-left', "
                f"'bottom-right', 'top-left' or 'top-right'")

        # Apply caller-supplied placement offset (multi-part job layout). After the
        # selected corner is normalized to (0,0), shift the whole part to its sheet
        # position. Translation does not affect arc IJK (incremental, G91.1).
        offsetX += placement_offset[0]
        offsetY += placement_offset[1]

        def translate_point(x, y):
            return x + offsetX, y + offsetY
        
        # Translate all entities
        for circle in self.circles:
            circle['center'] = translate_point(*circle['center'])
        
        for line in self.lines:
            line['start'] = translate_point(*line['start'])
            line['end'] = translate_point(*line['end'])
        
        for arc in self.arcs:
            arc['center'] = translate_point(*arc['center'])

        for i, polyline in enumerate(self.polylines):
            self.polylines[i] = [translate_point(x, y) for x, y in polyline]

        # Also transform multi-layer geometry if present
        if self.layer_data:
            for layer_name, layer_info in self.layer_data.items():
                # Translate circles
                for circle in layer_info['circles']:
                    circle['center'] = translate_point(*circle['center'])

                # Translate polylines
                for i, polyline in enumerate(layer_info['polylines']):
                    layer_info['polylines'][i] = [translate_point(x, y) for x, y in polyline]

                # Translate Shapely Polygons
                if 'polygons' in layer_info:
                    translated_polygons = []
                    for poly in layer_info['polygons']:
                        translated = affinity.translate(poly, xoff=offsetX, yoff=offsetY)
                        translated_polygons.append(translated)
                    layer_info['polygons'] = translated_polygons

        # Calculate new bounds
        all_x = []
        all_y = []
        for circle in self.circles:
            cx, cy = circle['center']
            r = circle.get('radius') or (circle.get('diameter', 0) / 2)
            all_x.extend([cx - r, cx + r])
            all_y.extend([cy - r, cy + r])
        for line in self.lines:
            all_x.extend([line['start'][0], line['end'][0]])
            all_y.extend([line['start'][1], line['end'][1]])
        for polyline in self.polylines:
            for x, y in polyline:
                all_x.append(x)
                all_y.append(y)

        # Include multi-layer geometry in final bounds
        if self.layer_data:
            for layer_name, layer_info in self.layer_data.items():
                for circle in layer_info['circles']:
                    cx, cy = circle['center']
                    r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                    all_x.extend([cx - r, cx + r])
                    all_y.extend([cy - r, cy + r])
                for polyline in layer_info['polylines']:
                    for x, y in polyline:
                        all_x.append(x)
                        all_y.append(y)

        new_minX, new_maxX = min(all_x), max(all_x)
        new_minY, new_maxY = min(all_y), max(all_y)

        print(f"  Transformed bounds: X=[{new_minX:.3f}, {new_maxX:.3f}], Y=[{new_minY:.3f}, {new_maxY:.3f}]")
        print(f"  New origin (0,0) is at the {origin_corner} corner\n")

        # Check if part fits within machine bounds
        part_width = new_maxX - new_minX
        part_height = new_maxY - new_minY
        machine_x_max = self.config.machine_x_max
        machine_y_max = self.config.machine_y_max

        if enforce_bounds and (part_width > machine_x_max or part_height > machine_y_max):
            error_msg = (f"Part dimensions ({part_width:.2f}\" × {part_height:.2f}\") exceed machine bounds "
                        f"({machine_x_max:.1f}\" × {machine_y_max:.1f}\"). "
                        f"Try rotating 90° or reduce part size.")
            self._add_error(error_msg)
            print(f"  ❌ {error_msg}")

    def bounding_box(self) -> Optional[Tuple[float, float, float, float]]:
        """Return (minX, minY, maxX, maxY) of all current geometry, or None if empty.
        Reflects the current (already-transformed) coordinates, so multi-part job
        layout can read each placed part's footprint for validation and rendering."""
        all_x = []
        all_y = []
        for circle in self.circles:
            cx, cy = circle['center']
            r = circle.get('radius') or (circle.get('diameter', 0) / 2)
            all_x.extend([cx - r, cx + r])
            all_y.extend([cy - r, cy + r])
        for line in self.lines:
            all_x.extend([line['start'][0], line['end'][0]])
            all_y.extend([line['start'][1], line['end'][1]])
        for arc in self.arcs:
            r = arc['radius']
            all_x.extend([arc['center'][0] - r, arc['center'][0] + r])
            all_y.extend([arc['center'][1] - r, arc['center'][1] + r])
        for polyline in self.polylines:
            for x, y in polyline:
                all_x.append(x)
                all_y.append(y)
        if self.layer_data:
            for layer_info in self.layer_data.values():
                for circle in layer_info['circles']:
                    cx, cy = circle['center']
                    r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                    all_x.extend([cx - r, cx + r])
                    all_y.extend([cy - r, cy + r])
                for polyline in layer_info['polylines']:
                    for x, y in polyline:
                        all_x.append(x)
                        all_y.append(y)
        if not all_x or not all_y:
            return None
        return (min(all_x), min(all_y), max(all_x), max(all_y))

    def placed_polygon(self):
        """Return a Shapely Polygon of this part's outer perimeter in current
        (already-transformed) coordinates, for multi-part overlap tests. Uses the
        identified perimeter when available; otherwise falls back to the bounding
        rectangle so disjoint parts still get a real-geometry distance check."""
        if self.perimeter and len(self.perimeter) >= 3:
            try:
                poly = Polygon(self.perimeter)
                if poly.is_valid and not poly.is_empty:
                    return poly
            except Exception:
                pass
        bbox = self.bounding_box()
        if not bbox:
            return None
        minx, miny, maxx, maxy = bbox
        return Polygon([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)])

    def classify_holes(self):
        """Classify holes by diameter"""
        # Classify all circles as holes (apply size check)
        self.holes = []

        for circle in self.circles:
            diameter = circle['diameter']
            center = circle['center']

            # Reject only holes genuinely smaller than the tool (with a tolerance so a hole
            # that is essentially the tool diameter - within unit-conversion/DXF rounding -
            # is drilled, not rejected). A hole at the tool size is made by plunging straight
            # down (peck drill), not milling.
            if diameter < self.tool_diameter - self.hole_size_tolerance:
                error_msg = f"Hole at ({center[0]:.3f}, {center[1]:.3f}) has diameter {diameter:.3f}\" which is too small for {self.tool_diameter:.3f}\" tool"
                self._add_error(error_msg)
                continue

            # Determine machining strategy based on hole size
            if diameter < self.min_millable_hole:
                # Hole is tool-sized up to (but not big enough for) helical entry: peck drill
                # straight down, then spiral-clear at the bottom if there is material to clear
                # (a hole exactly the tool size is a pure plunge with no clearing).
                self.holes.append({'center': center, 'diameter': diameter, 'needs_peck_drill': True})
                print(f"  Hole (d={diameter:.3f}\") at ({center[0]:.3f}, {center[1]:.3f}) - using peck drill + spiral")
            else:
                # Hole is large enough for helical entry
                self.holes.append({'center': center, 'diameter': diameter, 'needs_peck_drill': False})
                print(f"  Hole (d={diameter:.3f}\") at ({center[0]:.3f}, {center[1]:.3f}) - using helical + spiral")

        print(f"\nIdentified {len(self.holes)} millable holes")
        if self.errors:
            print(f"  ❌ {len(self.errors)} error(s) found during hole classification")

        # Sort holes to minimize travel time
        self._sort_holes()

    def load_tube_pattern(self, face_width: float, tube_length: float,
                          mode: str = 'holes', hole_diameter: float = None,
                          spacing: float = None):
        """Load a pre-designed tube pattern instead of a DXF.

        mode='holes' drills mounting holes - three per column on a 2" face, one on a 1"
        face. mode='lightening' mills truss triangles and no holes. The two never appear
        together: see tube_patterns.MODES for why.

        HOLES ARE DRILLED, NOT MILLED. That is not a separate toolpath - it falls out of
        sizing the cutter to the hole. classify_holes marks a hole at tool size as
        needs_peck_drill, and _generate_peck_drill_and_spiral_gcode then emits straight
        pecks with NO lateral clearing, which is the only motion a twist drill can make.
        So a holes pattern REQUIRES the tool to be a drill of the hole's diameter, and
        this method refuses rather than quietly milling with whatever is loaded: an end
        mill fed sideways through 141 holes is exactly the class of bug this project has
        shipped before.

        The generated circles go through classify_holes() rather than being turned into
        holes here, so a pattern hole gets the same too-small-for-the-tool rejection and
        the same peck-vs-helical decision as a drawn one.

        Returns the pattern's warnings, which are advice rather than errors.
        """
        import tube_patterns

        # INCH ONLY. Every constant in tube_patterns is inches, and the tube program
        # hard-codes G20 while apply_material_preset has already converted the feeds to
        # mm/min. A metric run therefore produced inch-mode G-code holding millimetre
        # coordinates: a 610 mm tube became a 610 INCH tube (3657 holes), and a 1.6 mm
        # wall made the Z offset negative, commanding the drill 0.6" below the top of the
        # jig on every hole. Refused rather than scaled, because the rest of the tube
        # G-code path is inch-only too and a partial conversion would be worse.
        if getattr(self, 'units', 'inch') != 'inch':
            raise ValueError(
                f'Pre-designed tube patterns are inch-only, but this job is in '
                f'{self.units}. Run the tube pattern in inches, or draw the pattern in '
                f'CAD and use the DXF path.')

        kwargs = {'mode': mode}
        if hole_diameter is not None:
            kwargs['hole_diameter'] = hole_diameter
        if spacing is not None:
            kwargs['spacing'] = spacing
        # The material's own helix entry radius, not the module default: pockets have to
        # be sized against what this job's cutter will actually sweep going in.
        kwargs['helix_radius_multiplier'] = getattr(
            self, 'helix_radius_multiplier',
            tube_patterns.DEFAULT_HELIX_RADIUS_MULTIPLIER)
        pattern = tube_patterns.generate(face_width, tube_length, self.tool_diameter,
                                         **kwargs)

        if mode == 'holes':
            wanted = hole_diameter if hole_diameter is not None else tube_patterns.HOLE_DIAMETER
            if abs(self.tool_diameter - wanted) > self.hole_size_tolerance:
                raise ValueError(
                    f'A drilled hole pattern needs a {wanted:.4f}" twist drill, but the '
                    f'tool is {self.tool_diameter:.4f}". A tool narrower than the hole '
                    f'would mill each hole out sideways instead of drilling it.')
            # Checking tool_diameter alone was not enough. classify_holes decides
            # drill-vs-mill from min_millable_hole, which is derived from the tool at
            # CONSTRUCTION - so a caller that set .tool_diameter afterwards, or a team
            # config with min_millable_multiplier at 1.0, sailed past the check above and
            # got helical entries with the header still reading "twist drill". Verify the
            # decision this pattern actually depends on, not a proxy for it.
            if not (self.min_millable_hole > wanted):
                raise ValueError(
                    f'A drilled hole pattern needs every hole to peck straight down, but '
                    f'this job would mill a {wanted:.4f}" hole (min_millable_hole is '
                    f'{self.min_millable_hole:.4f}"). Build the post-processor with the '
                    f'drill as its tool, and check min_millable_multiplier in the team '
                    f'config is above 1.0.')

        # Stand in for load_dxf's parsed geometry. No perimeter: the tube face IS the
        # boundary and the tube flow passes skip_perimeter=True, so inventing one here
        # would only risk something downstream trying to cut the tube in half.
        self.circles = pattern['circles']
        self.lines = []
        self.arcs = []
        self.polylines = []
        self.splines = []
        self.layer_data = None
        self.perimeter = None
        self.pockets = [list(ring) for ring in pattern['pockets']]
        # Errors are per-load, not per-object. Without this, a pattern that failed
        # validation left its errors behind and the NEXT, perfectly valid pattern loaded
        # into the same post-processor was refused - citing pockets that no longer exist.
        self.errors = []

        self.classify_holes()
        self._sort_pockets()

        # Remembered so the program header can say "drill" rather than "end mill", and so
        # the preview can draw the right thing.
        self.tube_pattern_mode = mode
        # A drilled pattern is cut with a twist drill (the generator refuses to combine
        # it with any milling operation), so its through holes need the point allowance.
        self.tool_has_drill_point = (mode == 'holes')
        if self.tool_has_drill_point:
            self.apply_twist_drill_feeds()

        print(f"\nLoaded tube pattern ({mode}): {len(self.holes)} holes, "
              f"{len(self.pockets)} lightening pockets")
        for warning in pattern['warnings']:
            print(f"  \u26a0 {warning}")
        return pattern['warnings']

    def load_tube_design(self, design: dict, face_width: float, tube_length: float):
        """Load a CUSTOM tube face - features the user placed themselves - instead of a
        DXF or one of the two fixed patterns.

        This is deliberately the DXF path, not the drilled one. A design can mix a
        0.1695" clearance hole, a 0.2656" one and a 1.125" bearing bore, which no single
        twist drill can make, and this program has no tool change; the one tool that can
        cut all three is the end mill already in the spindle. Everything after resolution
        is therefore identical to a drawn face: classify_holes() picks peck-plunge for a
        hole at tool size and helical entry for a big one, _sort_pockets() orders the
        rings, and generate_tube_pattern_gcode() mirrors the whole thing onto face 2.

        Because the tool IS an end mill here, square_end and cut_to_length stay allowed -
        the refusal in generate_tube_pattern_gcode keys on mode == 'holes', which is the
        only mode that puts a drill in the spindle.

        Raises ValueError if the design cannot be machined as drawn. That is the same
        stance load_tube_pattern takes for an unusable tool: a refusal the operator can
        act on beats a program that is well-formed and wrong.

        Returns the design's warnings, which are advice rather than errors.
        """
        import tube_designer

        # INCH ONLY, for exactly the reasons load_tube_pattern is: every constant in
        # tube_designer is inches and the tube program hard-codes G20, so a metric run
        # would emit inch-mode G-code holding millimetre numbers.
        if getattr(self, 'units', 'inch') != 'inch':
            raise ValueError(
                f'Custom tube designs are inch-only, but this job is in {self.units}. '
                f'Run the tube job in inches, or draw the face in CAD and use the DXF '
                f'path.')

        resolved = tube_designer.resolve(
            design, face_width, tube_length, self.tool_diameter,
            helix_radius_multiplier=getattr(
                self, 'helix_radius_multiplier',
                tube_designer.DEFAULT_HELIX_RADIUS_MULTIPLIER),
            hole_size_tolerance=self.hole_size_tolerance)
        if resolved['errors']:
            raise ValueError('This design cannot be machined as drawn. '
                             + ' '.join(resolved['errors']))

        # Stand in for load_dxf's parsed geometry, exactly as load_tube_pattern does. No
        # perimeter: the tube face IS the boundary and the tube flow passes
        # skip_perimeter=True.
        self.circles = [{'center': c['center'], 'radius': c['radius'],
                         'diameter': c['diameter']} for c in resolved['circles']]
        self.lines = []
        self.arcs = []
        self.polylines = []
        self.splines = []
        self.layer_data = None
        self.perimeter = None
        self.pockets = [list(ring) for ring in resolved['pockets']]
        # Errors are per-load, not per-object: a design that failed validation must not
        # leave its errors behind to condemn the next one loaded into this processor.
        self.errors = []

        self.classify_holes()
        self._sort_pockets()
        self.tube_pattern_mode = 'custom'

        print(f"\nLoaded custom tube design: {len(self.holes)} holes, "
              f"{len(self.pockets)} pockets")
        for warning in resolved['warnings']:
            print(f"  \u26a0 {warning}")
        return resolved['warnings']

    def _optimize_route(self, items, item_type="items"):
        """
        Generic route optimization using nearest neighbor + 2-opt algorithm.

        This uses a two-phase approach:
        1. Nearest neighbor: Build initial route by always going to closest unvisited item
        2. 2-opt: Optimize route by eliminating crossed paths

        Args:
            items: List of dicts with 'center' key containing (x, y) coordinates
            item_type: String describing the item type for logging (e.g., "holes", "pockets")

        Returns:
            Tuple of (optimized_route, total_distance, num_iterations)
        """
        if len(items) <= 1:
            return items, 0.0, 0

        # Phase 1: Nearest Neighbor Algorithm
        # Start at origin (0, 0) and build route by always going to nearest unvisited item
        unvisited = items.copy()
        route = []
        current_pos = (0, 0)  # Start at origin

        while unvisited:
            # Find nearest unvisited item
            nearest_idx = 0
            nearest_dist = self._distance_2d(current_pos, unvisited[0]['center'])

            for i in range(1, len(unvisited)):
                dist = self._distance_2d(current_pos, unvisited[i]['center'])
                if dist < nearest_dist:
                    nearest_dist = dist
                    nearest_idx = i

            # Add nearest item to route and remove from unvisited
            nearest_item = unvisited.pop(nearest_idx)
            route.append(nearest_item)
            current_pos = nearest_item['center']

        # Phase 2: 2-opt Optimization
        # Try swapping edge pairs to reduce total distance
        improved = True
        max_iterations = 100
        iteration = 0

        while improved and iteration < max_iterations:
            improved = False
            iteration += 1

            for i in range(len(route) - 1):
                for j in range(i + 2, len(route)):
                    # 2-opt: reverse segment from i to j-1
                    # Changes edges: (i-1)→(i) and (j-1)→(j)
                    # Into edges: (i-1)→(j-1) and (i)→(j)

                    # Get the points before and after the segment to reverse
                    if i == 0:
                        point_before = (0, 0)  # Origin
                    else:
                        point_before = route[i - 1]['center']

                    if j < len(route):
                        point_after = route[j]['center']
                    else:
                        point_after = None  # No point after (j is at end)

                    point_i = route[i]['center']
                    point_j_minus_1 = route[j - 1]['center']

                    # Calculate distance before swap
                    dist_before = self._distance_2d(point_before, point_i)
                    if point_after is not None:
                        dist_before += self._distance_2d(point_j_minus_1, point_after)

                    # Calculate distance after swap (reversing segment i to j-1)
                    dist_after = self._distance_2d(point_before, point_j_minus_1)
                    if point_after is not None:
                        dist_after += self._distance_2d(point_i, point_after)

                    # If swap improves distance, do it
                    if dist_after < dist_before:
                        # Reverse the segment from i to j-1
                        route[i:j] = reversed(route[i:j])
                        improved = True

        # Calculate total travel distance
        total_dist = self._distance_2d((0, 0), route[0]['center'])
        for i in range(len(route) - 1):
            total_dist += self._distance_2d(route[i]['center'], route[i + 1]['center'])

        print(f"Optimized {len(route)} {item_type} - total travel: {total_dist:.2f}\" ({iteration} 2-opt iterations)")

        return route, total_dist, iteration

    def _sort_holes(self):
        """Sort holes to minimize tool travel time using nearest neighbor + 2-opt."""
        if len(self.holes) <= 1:
            return

        self.holes, _, _ = self._optimize_route(self.holes, "holes")

    def _sort_pockets(self):
        """
        Sort pockets to minimize tool travel time using nearest neighbor + 2-opt.

        Uses pocket centroids as the position for distance calculations.
        """
        if len(self.pockets) <= 1:
            return

        # Calculate centroid for each pocket (used for distance calculations)
        pocket_data = []
        for pocket_points in self.pockets:
            # Calculate centroid
            sum_x = sum(p[0] for p in pocket_points)
            sum_y = sum(p[1] for p in pocket_points)
            centroid = (sum_x / len(pocket_points), sum_y / len(pocket_points))
            pocket_data.append({'points': pocket_points, 'center': centroid})

        # Optimize route using generic algorithm
        optimized_route, _, _ = self._optimize_route(pocket_data, "pockets")

        # Extract optimized pocket points list
        self.pockets = [p['points'] for p in optimized_route]

    def identify_perimeter_and_pockets(self):
        """Identify the outer perimeter and any inner pockets"""
        # Collect all closed paths as perimeter candidates: polylines AND circles.
        # Circles must be candidates even when polylines are present, because a
        # round part's OUTER boundary is a circle while its interior features
        # (bore, slots, lightening cuts) are polylines - the perimeter is the
        # largest boundary of EITHER kind. (Non-perimeter circles still stay holes,
        # not pockets; see the pocket assignment below.) Previously circles were
        # only considered when NO polylines existed, so a round part with any
        # interior polyline had its circular perimeter ignored and mis-detected.
        all_paths = []
        circle_to_path_map = {}  # Track which paths came from circles (path idx -> circle idx)

        # Add existing polylines
        polyline_count = 0
        if self.polylines:
            all_paths.extend(self.polylines)
            polyline_count = len(self.polylines)

        # Add circles as candidates too (tessellated to polylines)
        if hasattr(self, 'circles') and self.circles:
            for i, circle in enumerate(self.circles):
                try:
                    cx, cy = circle['center']
                    r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                    if r <= 0:
                        continue
                    # Create polyline from circle (50 points)
                    points = self._tessellate_circle(cx, cy, r)
                    circle_to_path_map[len(all_paths)] = i
                    all_paths.append(points)
                except (KeyError, TypeError):
                    # Skip circles with missing/invalid data
                    continue

        if not all_paths:
            self.perimeter = None
            self.pockets = []
            return

        # Convert to Shapely polygons, tracking path index
        polygons = []
        for path_idx, points in enumerate(all_paths):
            try:
                poly = Polygon(points)
                if poly.is_valid:
                    polygons.append((poly, points, path_idx))
            except Exception:
                pass

        if not polygons:
            self.perimeter = None
            self.pockets = []
            return

        # Find the largest polygon (perimeter)
        polygons.sort(key=lambda x: x[0].area, reverse=True)
        candidate_perimeter = polygons[0][1]  # Get the original points
        candidate_poly = polygons[0][0]
        perimeter_path_idx = polygons[0][2]

        # Validate that the perimeter is reasonable
        # If we have holes, the perimeter should be significantly larger than the bounding box of holes
        if hasattr(self, 'circles') and self.circles:
            xs = [c['center'][0] for c in self.circles]
            ys = [c['center'][1] for c in self.circles]
            bbox_width = max(xs) - min(xs)
            bbox_height = max(ys) - min(ys)
            bbox_area = bbox_width * bbox_height

            # If the candidate perimeter is < 10% of the bounding box area, it's probably not the real perimeter
            perimeter_area = candidate_poly.area
            if perimeter_area < 0.1 * bbox_area:
                # Only report as error if we had actual polylines (not just converted circles)
                # If we only converted circles and the largest isn't big enough, silently skip perimeter
                if polyline_count > 0:
                    error_msg = f"Perimeter too small ({perimeter_area:.2f} sq in) compared to part bounding box ({bbox_area:.2f} sq in). DXF may be missing perimeter outline geometry."
                    print(f"\n❌ ERROR: {error_msg}")
                    self.errors.append(error_msg)
                self.perimeter = None
                self.pockets = []
                return

        self.perimeter = candidate_perimeter

        # Pockets are polyline-derived regions only (excluding whichever one is the
        # perimeter). Circles that aren't the perimeter remain holes, never pockets -
        # so a washer's inner circle and a round part's bolt holes stay holes.
        self.pockets = [points for (poly, points, path_idx) in polygons[1:]
                        if path_idx not in circle_to_path_map]

        # If the perimeter itself came from a circle (round part / washer), drop that
        # circle from self.circles so it isn't also machined as a hole.
        if perimeter_path_idx in circle_to_path_map:
            perimeter_circle_idx = circle_to_path_map[perimeter_path_idx]
            self.circles = [c for i, c in enumerate(self.circles) if i != perimeter_circle_idx]
            print(f"  Removed 1 circle that was identified as the perimeter")

        print(f"\nIdentified perimeter and {len(self.pockets)} pockets")

        self._check_open_chains_against_perimeter(candidate_perimeter)

        # Sort pockets to minimize travel time
        self._sort_pockets()

    def _check_open_chains_against_perimeter(self, perimeter_points) -> None:
        """Refuse the part when a DROPPED chain is bigger than the perimeter we chose.

        An outer profile that fails to close is discarded, and the biggest remaining
        closed loop - a POCKET - gets promoted to perimeter. The program then profiles
        through the middle of the part, with tabs, and looks perfectly ordinary. The
        signature is unmistakable: something we threw away was larger than the outline
        we kept. A smaller open chain is a stray line and only earns the warning it
        already has.
        """
        if not getattr(self, 'open_chains', None) or not perimeter_points:
            return

        def extents(points):
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return (max(xs) - min(xs), max(ys) - min(ys))

        perimeter_w, perimeter_h = extents(perimeter_points)
        for chain in self.open_chains:
            coords = chain['coords']
            if len(coords) < 2:
                continue
            width, height = extents(coords)
            if width * height <= perimeter_w * perimeter_h + 1e-6:
                continue
            start, end = coords[0], coords[-1]
            self._add_error(
                f'The outer profile did not close: a {chain["gap"]:.4f}" gap between '
                f'({start[0]:.3f}, {start[1]:.3f}) and ({end[0]:.3f}, {end[1]:.3f}). '
                f'That outline is bigger than the boundary PenguinCAM would otherwise '
                f'profile, so the program would cut through the middle of the part. '
                f'Close the outline in CAD and export again.')
            return

    def _generate_interior_gcode(self, emit_contour_pauses: bool) -> List[str]:
        """Generate the interior-feature toolpath (holes + pockets) for this part.

        This is phase "A" of a part: all circular holes (helical/peck + spiral clearing,
        or contouring for large through-holes) followed by all pockets (fully cleared or
        contoured). Returns only the toolpath lines - no header/footer/perimeter.

        Args:
            emit_contour_pauses: when True, emit the standalone "PAUSE FOR FIXTURING"
                sequence before contoured holes/pockets (single-part behavior, gated by
                pause_before_perimeter). The multi-part job assembler passes False because
                it emits a single shared pause between all interiors and all perimeters.
        """
        gcode = []

        # Holes (all circular features - helical entry + spiral clearing, or contouring for large holes)
        if self.holes:
            gcode.append("(===== HOLES =====)")

            # Only contour through-cuts; partial-depth features must be fully cleared
            is_through_cut = self.is_through_cut()

            # Separate holes into contoured and cleared based on size
            contoured_holes = []
            cleared_holes = []

            for i, hole in enumerate(self.holes, 1):
                center = hole['center']
                diameter = hole['diameter']
                needs_peck = hole.get('needs_peck_drill', False)

                # Calculate hole area: π × r²
                hole_area = math.pi * (diameter / 2) ** 2

                threshold_area = self._contour_threshold_area()

                # Only contour if it's a through-cut AND exceeds size threshold
                if is_through_cut and hole_area > threshold_area:
                    contoured_holes.append((i, hole, hole_area))
                    gcode.append(f"(Hole {i} - {diameter:.3f}\" diameter, {hole_area:.3f} sq in > {threshold_area:.3f} sq in threshold - will contour through-cut)")
                else:
                    cleared_holes.append((i, hole, needs_peck))
                    strategy = "peck + spiral" if needs_peck else "helical + spiral"
                    reason = "(partial depth)" if not is_through_cut else ""
                    gcode.append(f"(Hole {i} - {diameter:.3f}\" diameter, {hole_area:.3f} sq in - {strategy} {reason})")

            # Process cleared holes first
            if cleared_holes:
                gcode.append("")
                gcode.append("(--- Cleared holes ---)")
                for i, hole, needs_peck in cleared_holes:
                    center = hole['center']
                    diameter = hole['diameter']
                    gcode.extend(self._clear_in_depth_levels(
                        lambda c=center, d=diameter, pk=needs_peck:
                        self._generate_hole_gcode(c[0], c[1], d, needs_peck_drill=pk)))
                    gcode.append("")

            # Process contoured holes (with optional pause for fixturing)
            if contoured_holes:
                # Optional pause before contoured holes for teams using screw fixturing
                # Same logic as perimeter/pockets - any operation with tabs needs secure fixturing
                if emit_contour_pauses:
                    gcode.extend(self._generate_pause_and_park_gcode(
                        'PAUSE FOR FIXTURING',
                        [
                            'Cleared holes complete',
                            'Install screws through holes into sacrifice board',
                            'Fixture part securely before contouring large holes'
                        ]
                    ))

                gcode.append("")
                gcode.append("(--- Contoured holes - manual removal required ---)")
                for i, hole, area in contoured_holes:
                    center = hole['center']
                    diameter = hole['diameter']

                    # Generate circular points for contouring (50-segment tessellation)
                    circle_points = self._tessellate_circle(center[0], center[1], diameter / 2)

                    gcode.append(f"(Hole {i} - {diameter:.3f}\" dia, {area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(circle_points))
                    gcode.append("")

        # Pockets
        if self.pockets:
            gcode.append("(===== POCKETS =====)")

            # Only contour through-cuts; partial-depth features must be fully cleared
            is_through_cut = self.is_through_cut()

            # Separate pockets into contoured and fully cleared based on size
            contoured_pockets = []
            cleared_pockets = []

            for i, pocket in enumerate(self.pockets, 1):
                pocket_poly = Polygon(pocket)
                pocket_area = pocket_poly.area

                threshold_area = self._contour_threshold_area()

                # Only contour if it's a through-cut AND exceeds size threshold
                if is_through_cut and pocket_area > threshold_area:
                    contoured_pockets.append((i, pocket, pocket_area))
                    gcode.append(f"(Pocket {i}: {pocket_area:.3f} sq in > {threshold_area:.3f} sq in threshold - will contour through-cut)")
                else:
                    cleared_pockets.append((i, pocket, pocket_area))
                    reason = "- partial depth" if not is_through_cut else "- below threshold"
                    gcode.append(f"(Pocket {i}: {pocket_area:.3f} sq in - will fully clear {reason})")

            # Process fully cleared pockets first
            if cleared_pockets:
                gcode.append("")
                gcode.append("(--- Fully cleared pockets ---)")
                for i, pocket, area in cleared_pockets:
                    gcode.append(f"(Pocket {i} - {area:.3f} sq in)")
                    gcode.extend(self._clear_in_depth_levels(
                        lambda pocket=pocket: self._generate_pocket_gcode(pocket)))
                    gcode.append("")

            # Process contoured pockets (with optional pause for fixturing)
            if contoured_pockets:
                # Optional pause before pocket contours for teams using screw fixturing
                # Same logic as perimeter - any operation with tabs needs secure fixturing
                if emit_contour_pauses:
                    gcode.extend(self._generate_pause_and_park_gcode(
                        'PAUSE FOR FIXTURING',
                        [
                            'Cleared pockets complete',
                            'Install screws through holes into sacrifice board',
                            'Fixture part securely before contouring large pockets'
                        ]
                    ))

                gcode.append("")
                gcode.append("(--- Contoured pockets - manual removal required ---)")
                for i, pocket, area in contoured_pockets:
                    gcode.append(f"(Pocket {i} - {area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(pocket))
                    gcode.append("")

        return gcode

    def generate_gcode(self, suggested_filename: str = None, timestamp: str = None,
                       include_header_footer: bool = True) -> PostProcessorResult:
        """
        Generate complete G-code for standard plate operations (single or multi-layer)

        Args:
            suggested_filename: Optional filename (without timestamp, will be added)
            include_header_footer: when False, return only the feature toolpath body
                (no header/footer). Defaults to True (normal single-part output).
                (Multi-part jobs no longer use this - they call generate_part_phases and
                assemble_job_gcode collates the phases across parts.)

        Returns:
            PostProcessorResult with gcode string and stats
        """
        try:
            self.validate_aluminum_cutting_parameters()
        except ValueError as exc:
            return PostProcessorResult(success=False, errors=[str(exc)])

        # Check for validation errors first
        if self.errors:
            print(f"\n❌ Cannot generate G-code: {len(self.errors)} validation error(s) found")
            for error in self.errors:
                print(f"   - {error}")
            return PostProcessorResult(
                success=False,
                errors=self.errors.copy()
            )

        # Multi-layer processing
        if self.layer_data:
            if self.chamfer_pass:
                return PostProcessorResult(
                    success=False,
                    errors=['The deburr / chamfer pass supports 2D parts only; this part '
                            'has multiple depth layers (2.5D).'])
            return self._generate_multilayer_gcode(suggested_filename, timestamp)

        # Use provided timestamp (from client's timezone) or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # The deburr / chamfer pass is validated and generated FIRST, so a refused pass
        # (wider than the bit can cut, through the stock, part too narrow for the break)
        # fails the whole program here rather than surfacing as errors recorded on an
        # otherwise "successful" result.
        self.warnings = []
        chamfer_body = []
        if self.chamfer_pass:
            self._deferred_tab_positions = []
            chamfer_body = self._chamfer_pass_body()
            if self.errors:
                return PostProcessorResult(success=False, errors=self.errors.copy())

        # Generate header (skipped for job-body mode; assemble_job_gcode adds one shared header)
        chamfer_tools = self._chamfer_pass_tool_table() if chamfer_body else None
        gcode = (self._generate_gcode_header(timestamp, is_multilayer=False,
                                             tool_table=chamfer_tools)
                 if include_header_footer else [])
        warnings = []
        if self.chamfer_pass and not chamfer_body:
            warnings.append('The deburr / chamfer pass matched no edges on this part '
                            'and was skipped.')

        # Engraved part name, cut into the face while the stock is still whole. After
        # the profile the part is held by tabs alone, and a label is exactly the kind of
        # light, chattery cut that would break one.
        if self.engrave:
            gcode.extend(self._engrave_body())

        # Interior features (holes + pockets). Extracted to a shared helper so the
        # multi-part job assembler can collate interiors across parts. In single-part
        # output the contoured-feature fixturing pauses still fire when
        # pause_before_perimeter is set (unchanged behavior).
        gcode.extend(self._generate_interior_gcode(emit_contour_pauses=self.pause_before_perimeter))

        # Perimeter (with optional pause for screw fixturing)
        if self.perimeter:
            # Optional pause before perimeter for teams using screw fixturing
            if self.pause_before_perimeter:
                gcode.extend(self._generate_pause_and_park_gcode(
                    'PAUSE FOR FIXTURING',
                    [
                        'Internal features complete',
                        'Install screws through holes into sacrifice board',
                        'Fixture part securely before perimeter cutting'
                    ]
                ))

            # Generate perimeter header (tabs may or may not be present depending on config)
            if self.tabs_enabled:
                gcode.append("(===== PERIMETER WITH TABS =====)")
            else:
                gcode.append("(===== PERIMETER (NO TABS) =====)")

            # With a chamfer pass coming, tab removal is deferred past it: the tabs are
            # the only thing holding the part while the V-bit runs.
            gcode.extend(self._generate_perimeter_gcode(
                self.perimeter, defer_tab_removal=bool(chamfer_body)))
            gcode.append("")

        # Deburr / chamfer pass: swap to the V-bit, break the edges, and - when the
        # machine removes tabs - swap back to the end mill for the deferred tab pass.
        if chamfer_body:
            gcode.extend(self._chamfer_tool_change_gcode(to_vbit=True))
            gcode.extend(chamfer_body)
            if self.config.remove_tabs and self._deferred_tab_positions:
                gcode.extend(self._chamfer_tool_change_gcode(to_vbit=False))
                gcode.extend(self._generate_tab_removal_gcode(self._deferred_tab_positions))

        # Errors raised while BUILDING the toolpath - a part too small to hold three
        # tabs, a hole no tool in the job can make - are as fatal as the ones raised
        # during validation. They used to land on self.errors and be discarded, so the
        # route reported success and handed over a program with the feature missing.
        if self.errors:
            return PostProcessorResult(success=False, errors=self.errors.copy())

        # Footer (skipped for job-body mode; assemble_job_gcode adds one shared footer)
        if include_header_footer:
            gcode.extend(self._generate_gcode_footer())

        # Calculate estimated cycle time
        time_estimate = self._estimate_cycle_time(gcode)

        # Add cycle time to header (insert after the operations section)
        self._insert_cycle_time_comment(gcode, time_estimate)

        # Generate filename with timestamp (name sanitized for safe disk write + download)
        filename = build_output_filename(suggested_filename, timestamp, "output", dry_run=self.is_dry_run)

        # Return result
        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=(warnings
                      + [w for w in self.geometry_warnings if w not in warnings]
                      + [w for w in self.warnings if w not in warnings]),
            stats={
                'num_holes': len(self.holes) if hasattr(self, 'holes') else 0,
                'num_pockets': len(self.pockets) if hasattr(self, 'pockets') else 0,
                'has_perimeter': bool(self.perimeter) if hasattr(self, 'perimeter') else False,
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell'])
            }
        )

    def generate_part_phases(self) -> dict:
        """Produce this part's toolpath split into the three job phases.

        Used by the multi-part job assembler (assemble_job_gcode) so it can collate
        phases across all parts: all interiors, then one shared refixturing pause, then
        all perimeters, then all tab removals. For a single-part job this reproduces the
        normal single-part order.

        No per-feature fixturing pauses are emitted here - the job assembler emits a
        single shared pause between the interior and perimeter phases. Assumes a
        single-layer part (multi-part jobs are 2D standard mode; 2.5D is single-part).

        Returns a dict:
            {'interior': [str], 'perimeter': [str], 'tab_removal': [str], 'errors': [str]}
        Coordinates are already in absolute job space (transform_coordinates applied the
        placement offset), so phases from different parts can be freely interleaved.
        """
        # Fail fast on pre-existing validation errors (same guard as generate_gcode).
        if self.errors:
            return {'interior': [], 'perimeter': [], 'chamfer': [], 'tab_removal': [],
                    'errors': self.errors.copy()}

        if self.layer_data:
            return {'interior': [], 'perimeter': [], 'chamfer': [], 'tab_removal': [],
                    'errors': ['Multi-part jobs support single-layer (2D) parts only; '
                               'this part has multiple depth layers (2.5D).']}

        self._deferred_tab_positions = []

        # Each phase is assembled under its own "Safe Z between parts" move (see
        # _emit_phase), so the tool starts each phase up at safe height: flag the first
        # feature of each to rapid down to the clearance plane before its plunge feed.

        # Phase A: interiors (holes + pockets), no per-feature pauses, preceded by the
        # engraved part name - cut while the stock is still whole, because afterwards the
        # part hangs on tabs and a light chattery label cut is exactly what breaks one.
        self._pending_clearance_rapid = True
        self.warnings = []
        interior = self._engrave_body() if self.engrave else []
        if interior:
            self._pending_clearance_rapid = True
        interior = interior + self._generate_interior_gcode(emit_contour_pauses=False)

        # Phase C: perimeter cut, deferring tab removal to phase D.
        perimeter = []
        if self.perimeter:
            self._pending_clearance_rapid = True
            if self.tabs_enabled:
                perimeter.append("(===== PERIMETER WITH TABS =====)")
            else:
                perimeter.append("(===== PERIMETER (NO TABS) =====)")
            perimeter.extend(self._generate_perimeter_gcode(self.perimeter, defer_tab_removal=True))

        # Phase C2: this part's deburr / chamfer body, V-bit toolpath only. The job
        # assembler emits ONE shared tool change before the first chamfer body and (when
        # needed) one change back before the tab-removal phase, so the body here carries
        # no pause of its own. Refusals land on self.errors and fail the part.
        chamfer = []
        if self.chamfer_pass:
            chamfer = self._chamfer_pass_body()

        # Phase D: tab removal, using the positions captured during the perimeter pass.
        tab_removal = []
        if self.config.remove_tabs and self._deferred_tab_positions:
            tab_removal = self._generate_tab_removal_gcode(self._deferred_tab_positions)

        return {'interior': interior, 'perimeter': perimeter, 'chamfer': chamfer,
                'tab_removal': tab_removal, 'errors': list(self.errors),
                'warnings': list(self.geometry_warnings) + list(self.warnings)}

    # ---- Portability helpers: work-coordinate safe moves + optional coolant/park -------
    # Everything below emits G54 work-coordinate G-code by default. Machine-coordinate
    # (G53) motion appears ONLY when a park_position is configured, and coolant M-codes
    # ONLY when a coolant type is configured - so the default output runs on GRBL, Easel,
    # WinCNC, Mach, etc.

    # ---- Z datum ---------------------------------------------------------------
    # Three numbers place the whole program on the Z axis: where the stock's top face
    # is, where a through-cut ends, and how high a retract goes. Every toolpath in this
    # class is written relative to those, so changing the datum is a matter of moving
    # all three together - and the two datums then differ by exactly the stock
    # thickness, move for move (tests/test_unit.py asserts that).

    def _len(self, inches: float) -> float:
        """A length written in inches, in whatever units this program works in.

        Feeds and several preset lengths were already converted for millimetre mode, but
        a handful of Z-frame constants were used verbatim - and read as millimetres they
        are absurd: 0.5 mm of traverse clearance over the stock, a 0.008 mm through-cut
        overcut (the part stays attached), 0.1 mm of peck chip-clearance. Every inch
        literal that is a LENGTH goes through here.
        """
        return inches * 25.4 if self.units == 'mm' else inches

    #: How far above the last peck the tool rapids back to before cutting again. Enough
    #: to clear chips left in the flute, small enough not to waste the cycle. Inches.
    PECK_RETURN_CLEARANCE_IN = 0.02

    #: Chip-clearing retract for a drilled hole: just above the stock, not the full safe
    #: height - retracting to safe Z between every peck spends the cycle in rapids.
    DRILL_RETRACT_CLEARANCE_IN = 0.1

    #: Where a spot drill rapids down to before its feed move. Inches.
    SPOT_APPROACH_CLEARANCE_IN = 0.05

    #: Extra room a dry run leaves above the stock, over and above the clearance height.
    DRY_RUN_EXTRA_CLEARANCE_IN = 0.25

    @property
    def peck_return_clearance(self) -> float:
        return self._len(self.PECK_RETURN_CLEARANCE_IN)

    @property
    def drill_retract_clearance(self) -> float:
        return self._len(self.DRILL_RETRACT_CLEARANCE_IN)

    @property
    def spot_approach_clearance(self) -> float:
        return self._len(self.SPOT_APPROACH_CLEARANCE_IN)

    def _apply_z_frame(self):
        """Place material_top / retract_height / cut_depth for the current datum and
        stock thickness. Called from __init__ and again whenever the thickness or the
        sacrifice depth changes (2.5D derives thickness from the CAD layers)."""
        # A dry run has to clear the WORK, not a fixed number of inches. Two inches
        # over a 2.5 in block still put the cutter half an inch into it - a
        # non-rotating end mill fed through plywood at 50 ipm, under a banner
        # promising the program does not cut anything. The requested lift is a
        # minimum; the stock decides the rest.
        # The config states these in inches whatever the program's units are, so they
        # are converted here rather than at every use.
        clearance = self._len(self.clearance_height)
        overcut = self._len(self.sacrifice_board_depth)
        self.dry_run_lift = (max(self.dry_run_request,
                                 self.material_thickness + clearance
                                 + self._len(self.DRY_RUN_EXTRA_CLEARANCE_IN))
                             if self.dry_run_request else 0.0)
        top = 0.0 if self.z_datum == Z_DATUM_STOCK_TOP else self.material_thickness
        top += self.dry_run_lift          # 0 for a real program
        self.material_top = top                                  # top face of the stock
        self.retract_height = top + clearance                    # rapid height over it
        self.cut_depth = top - self.material_thickness - overcut

    @property
    def stock_bottom(self) -> float:
        """Z of the stock's bottom face - the sacrifice board surface. Zero on the board
        datum, -thickness on the stock-top datum."""
        return self.material_top - self.material_thickness

    def is_through_cut(self, z: float = None) -> bool:
        """Does a cut at this Z reach the bottom face of the stock?

        A through-cut can be contoured (the offcut falls away); anything shallower has to
        be cleared out completely, and a drilled hole only gets the point-length
        break-through allowance when it is going all the way. Written against the bottom
        face rather than against Z=0, which is the same thing ONLY on the board datum -
        on the stock-top datum every cut is negative and this read as "always through",
        which would have left partial-depth pockets contoured but never cleared."""
        return (self.cut_depth if z is None else z) <= self.stock_bottom + 1e-9

    def set_dry_run(self, lift: float = 2.0):
        """Raise the whole program clear of the work and stop the spindle turning.

        For proving a program before it touches material: the tool traces the exact same
        path in the air above the part, so a wrong origin, an oversized nest or a
        forgotten clamp shows up while nothing is at stake. Everything is derived from
        the three Z anchors, so lifting those lifts every move - the dry run and the real
        program differ by exactly this number and nothing else.
        """
        if not math.isfinite(lift) or lift < 0:
            raise ValueError(f'Dry-run lift must be a positive number of inches, got {lift!r}')
        self.dry_run_request = float(lift)
        self._apply_z_frame()
        # Raising every retract by the lift can push the top of the program past the
        # machine's Z travel, which is a soft-limit alarm mid-program rather than a
        # crash - but the operator should hear it from us, not from the controller.
        z_max = getattr(self.config, 'machine_z_max', None)
        if z_max and self._safe_z() > z_max:
            self.warnings.append(
                f'Dry run retracts to Z{self._safe_z():.3f} in, above this machine\'s '
                f'{z_max:.3f} in of Z travel. Lower the stock or the clearance height.')

    @property
    def is_dry_run(self) -> bool:
        return self.dry_run_lift > 0

    def _dry_run_banner(self) -> List[str]:
        """The block that tells an operator this program does not cut. Emitted by every
        header - plate, job and tube - because a program that cuts air is dangerous
        exactly when someone believes it is the real one."""
        if not self.is_dry_run:
            return []
        return ['(*************************************************)',
                '(* DRY RUN - THIS PROGRAM DOES NOT CUT ANYTHING   *)',
                f'(* Every move is raised {self.dry_run_lift:.2f} in above the work *)',
                '(* and the spindle is never started.              *)',
                '(* Regenerate with dry run OFF to cut for real.   *)',
                '(*************************************************)',
                '']

    def _spindle_start_gcode(self, detail: str = '') -> List[str]:
        """Spindle-on lines, or the dry run's refusal to turn it."""
        if self.is_dry_run:
            return ['M5  ; DRY RUN - spindle stays off',
                    'G4 P1  ; brief pause so the operator can confirm it is not turning']
        return [f'S{self.spindle_speed} M3  ; Spindle on{detail}',
                'G4 P2  ; Wait 2 seconds for spindle to reach speed']

    def set_z_datum(self, datum):
        """Switch which surface Z0 sits on, re-placing the Z frame around it."""
        self.z_datum = normalize_z_datum(datum)
        self._apply_z_frame()

    @property
    def z_shift(self) -> float:
        """How far this program's Z zero sits below the sacrifice-board zero: 0 for the
        board datum, -thickness for the stock-top datum. Applies to the few heights that
        are configured as absolute board-frame numbers rather than derived from the
        stock."""
        return self.material_top - self.material_thickness

    def z_zero_surface(self) -> str:
        """What the operator touches off on, in operator words."""
        return ('the top of the stock' if self.z_datum == Z_DATUM_STOCK_TOP
                else 'the sacrifice board surface')

    def _safe_z(self) -> float:
        """Work-coordinate (G54) safe retract height. Uses the configured machine ceiling
        when set, but never below the material + clearance so a thick part can't collide
        on the retract. The ceiling is configured as a height over the sacrifice board,
        so it is shifted with the datum - both datums then retract to the same physical
        height, not to the same number."""
        floor = self.retract_height  # material_thickness + clearance (or max_depth + clearance in 2.5D)
        # z_shift already carries the dry-run lift (it is measured from material_top),
        # so adding the lift again here charged it twice.
        ceiling = (self.safe_clearance_height or 0.0) + self.z_shift
        return max(floor, ceiling)

    def _coolant_on_gcode(self):
        """Coolant-start line for the configured coolant type, or None if no coolant is set.
        Air/Mist -> M7 (mist output, e.g. air blast), Flood -> M8."""
        coolant = (self.machine_coolant or '').strip().lower()
        code = 'M7' if coolant in ('air', 'mist') else ('M8' if coolant == 'flood' else None)
        return (f'{code}  ; Coolant on, {sanitize_comment(self.machine_coolant)}'
                if code else None)

    def _coolant_off_gcode(self):
        """Coolant-stop line (M9), or None if no coolant is configured."""
        coolant = (self.machine_coolant or '').strip().lower()
        return 'M9  ; Coolant off' if coolant in ('air', 'mist', 'flood') else None

    def _aluminum_preflight_gcode(self, tube_reach: float = None) -> List[str]:
        """Mandatory operator acknowledgement for hazards CAM cannot measure."""
        import feeds_speeds

        material_key = feeds_speeds.canonical_material_key(
            getattr(self, 'material_id', None))
        if not feeds_speeds.is_aluminum_material(material_key) or self.is_dry_run:
            return []
        lines = [
            '( === REQUIRED ALUMINUM PREFLIGHT === )',
            '( Fresh aluminum-specific 1 or 2 flute cutter; inspect cutting edges )',
            '( Clean collet; shortest practical stickout; verify low runout at cutter )',
            '( Stock and spoilboard rigidly clamped; toolpath and clamps clear )',
        ]
        coolant = (self.machine_coolant or '').strip().lower()
        if coolant in ('air', 'mist', 'flood'):
            lines.append(
                f'( Verify configured {sanitize_comment(self.machine_coolant)} flow is aimed and chips can escape )')
        else:
            lines.append('( Start and verify continuous manual air blast before cutting )')
        if material_key == 'aluminum_6063':
            lines.append('( 6063 requires proven aluminum-compatible lubricant or MQL )')
        else:
            lines.append('( Aluminum-compatible lubricant or MQL is strongly recommended )')
        if tube_reach is not None:
            lines.append(
                f'( Verify usable flute length and tool reach exceed {tube_reach:.3f} in )')
        lines.extend([
            '( Run a supervised coupon first after any tool, alloy, fixture, or setup change )',
            'M0  ; Confirm aluminum preflight before spindle start',
            '',
        ])
        return lines

    def _park_gcode(self, comment: str = 'Park'):
        """G53 machine-coordinate park (raise Z, then move the gantry to the fixed park
        spot) - ONLY when park_position is configured. Returns [] otherwise, keeping the
        program G54-only and portable. Callers should already be at a safe work Z."""
        if not self.park_position:
            return []
        px, py, pz = self.park_position
        return [
            f'G53 G0 Z{pz:.4f}  ; {comment}: raise to safe machine Z',
            f'G53 G0 X{px:.4f} Y{py:.4f}  ; {comment}: move gantry to park position',
        ]

    def _force_board_datum_for_tube(self):
        """Tube programs are zeroed to the tube in its jig, not to a sheet on a spoilboard.
        Their Z frame is built by lifting the plate toolpath by (tube_height - wall
        thickness), which only works from the board datum - so a stock-top setting is
        dropped here, loudly, instead of shifting every tube program by a wall thickness."""
        if self.z_datum != Z_DATUM_BOARD:
            self.set_z_datum(Z_DATUM_BOARD)
            print("  Note: tube jobs are zeroed to the tube in its jig; the stock-top Z "
                  "datum does not apply and was not used.")

    # ---- engraving ------------------------------------------------------------

    #: The font's tightest feature is the E/F/H crossbar spacing, 0.48 x cap height.
    #: Separated strokes need that gap to exceed the cutter, so a legible letter needs
    #: a cap height of at least this multiple of the tool diameter. Below it the swept
    #: cuts merge and the label is a solid blob - which the old 1.2x gate allowed for
    #: every common nesting cutter.
    ENGRAVE_MIN_HEIGHT_PER_TOOL = 2.1

    def _engrave_available_area(self):
        """Where a label may go: inside the outline, clear of the edge, and clear of
        every hole and pocket that will be machined out later.

        Engraving runs BEFORE the interiors, so anything placed over a bore is cut away
        with the slug and the part ships blank - which was the first version's behaviour
        on any part with a central bearing bore.
        """
        poly = Polygon(self.perimeter)
        margin = self.tool_radius + self._len(0.05)
        area = poly.buffer(-margin)
        if area.is_empty:
            return area
        for hole in (self.holes or []):
            cx, cy = hole['center']
            area = area.difference(Point(cx, cy).buffer(hole['diameter'] / 2.0 + margin))
        for pocket in (self.pockets or []):
            try:
                area = area.difference(Polygon(pocket).buffer(margin))
            except (ValueError, TypeError):
                continue        # a pocket shapely cannot read is not a placement hazard
        return area

    def _engrave_placement(self, area, text, height):
        """Find somewhere the whole label actually fits inside `area`.

        Returns (origin_x, origin_y, height, strokes) or None. The old version took the
        bounding-box centre of the available area, which for any L, U or C outline is
        the notch - so a part's name was engraved into whatever was nested beside it.
        Nothing is placed without proving the text's own box is contained.
        """
        if area.is_empty:
            return None
        minx, miny, maxx, maxy = area.bounds
        floor = self.tool_diameter * self.ENGRAVE_MIN_HEIGHT_PER_TOOL
        # Sized to the space rather than stepped down a fixed ladder: the text's width
        # is linear in cap height, so the height that exactly spans the available width
        # is arithmetic, and a coarse ladder just refuses names that would have fitted.
        unit = stroke_font.text_width(text, 1.0)
        span = maxx - minx
        by_width = (span / unit) if unit > 0 else height
        heights = []
        for h in (height, by_width * 0.98, by_width * 0.85, by_width * 0.7, floor):
            h = min(h, height)
            if h >= floor - 1e-9 and not any(abs(h - k) < 1e-6 for k in heights):
                heights.append(h)
        heights.sort(reverse=True)
        for h in heights:
            strokes, _ = stroke_font.text_strokes(text, h)
            pts = [p for stroke in strokes for p in stroke]
            if not pts:
                return None
            # Measure the strokes themselves rather than assuming a 0..h em box: a
            # comma hangs below the baseline and a dollar sign above the cap, and a
            # label is only proved inside the part if what is measured is what is cut.
            tx0, tx1 = min(p[0] for p in pts), max(p[0] for p in pts)
            ty0, ty1 = min(p[1] for p in pts), max(p[1] for p in pts)
            w, tall = tx1 - tx0, ty1 - ty0
            if w > (maxx - minx) or tall > (maxy - miny):
                continue
            # Coarse search: the centre first (where a label belongs when it fits),
            # then a grid. Bounded work - at most 4 heights x 26 positions.
            span_x, span_y = (maxx - minx) - w, (maxy - miny) - tall
            candidates = [(minx + span_x / 2.0, miny + span_y / 2.0)]
            steps = 4
            for i in range(steps + 1):
                for j in range(steps + 1):
                    candidates.append((minx + span_x * i / steps,
                                       miny + span_y * j / steps))
            for cx, cy in candidates:
                if area.contains(box_geom(cx, cy, cx + w, cy + tall)):
                    # Shift so the strokes' own extent starts at the proven corner.
                    return cx - tx0, cy - ty0, h, strokes
        return None

    def _engrave_body(self) -> List[str]:
        """Cut the part's name into its own face, shallow, with the loaded tool.

        Placed inside the part's own outline, clear of its holes and pockets, and only
        where the whole label provably fits - so the label travels with the part rather
        than into its neighbour or into a bore that gets machined away. Skipped with a
        warning, never silently: an operator who ticked the box is expecting a label.
        """
        spec = self.engrave or {}
        raw_text = str(spec.get('text') or '')
        text = sanitize_comment(raw_text, fallback='')
        if not text:
            self.warnings.append(
                f'{raw_text!r} has no characters that can be engraved; no name was cut.')
            return []
        # Characters with no glyph become a visible dash rather than a mark that reads
        # as some other character - a label that silently changes is worse than one
        # that is visibly incomplete.
        engraved, dropped = [], []
        for ch in text.upper():
            if ch in stroke_font.GLYPHS:
                engraved.append(ch)
            else:
                engraved.append('-')
                dropped.append(ch)
        text = ''.join(engraved)
        if dropped:
            self.warnings.append(
                f'{text}: {"".join(sorted(set(dropped)))} cannot be engraved and became '
                f'dashes.')
        if self.perimeter is None:
            self.warnings.append('Nothing to engrave on: this part has no outline.')
            return []

        try:
            height = float(spec.get('height') or 0.18)
            depth = float(spec.get('depth') or 0.01)
        except (TypeError, ValueError):
            self.warnings.append('The engraving height and depth must be numbers; skipped.')
            return []
        if not (math.isfinite(height) and math.isfinite(depth)) or height <= 0 or depth <= 0:
            self.warnings.append('The engraving height and depth must be positive; skipped.')
            return []
        if depth >= self.material_thickness:
            self.warnings.append(
                f'An engraving {depth:.3f} in deep would go through {self.material_thickness:.3f} in '
                f'stock; skipped.')
            return []

        min_height = self.tool_diameter * self.ENGRAVE_MIN_HEIGHT_PER_TOOL
        area = self._engrave_available_area()
        placed = self._engrave_placement(area, text, max(height, min_height))
        if placed is None:
            # Name the real obstacle. Blaming the geometry when the cutter is the
            # problem sends someone looking for space they already have.
            if height < min_height:
                self.warnings.append(
                    f'{text}: a {self.tool_diameter:.4f} in cutter cannot write letters '
                    f'{height:.3f} in tall - it needs at least '
                    f'{min_height:.3f} in. Use a finer bit or a taller name; skipped.')
            else:
                self.warnings.append(
                    f'{text}: no clear space on this part for a legible name; skipped.')
            return []
        ox, oy, height, strokes = placed

        cut_z = self.material_top - depth
        # Lateral moves between letters go at the same height everything else in this
        # program traverses at, not a hard-coded one - a team that raised the clearance
        # for hold-down hardware gets that clearance here too.
        clear_z = self.retract_height
        gcode = ['', '(===== ENGRAVE PART NAME =====)',
                 f'(Text: {text})',
                 f'(Cap height {height:.3f} in, {depth:.3f} in deep, '
                 f'{self.tool_diameter:.4f} in tool)',
                 f'G0 Z{self._safe_z():.4f}  ; Safe Z clearance']
        for stroke in strokes:
            if len(stroke) < 2:
                continue
            x0, y0 = ox + stroke[0][0], oy + stroke[0][1]
            gcode.append(f'G0 X{x0:.4f} Y{y0:.4f}')
            gcode.append(f'G0 Z{clear_z:.4f}')
            gcode.append(f'G1 Z{cut_z:.4f} F{self.plunge_rate:.1f}  ; Down to engraving depth')
            for x, y in stroke[1:]:
                gcode.append(f'G1 X{ox + x:.4f} Y{oy + y:.4f} F{self.feed_rate:.1f}')
            gcode.append(f'G0 Z{clear_z:.4f}')
        gcode.append(f'G0 Z{self._safe_z():.4f}  ; Safe Z clearance')
        return gcode

    def _tube_wcs_activate_gcode(self) -> str:
        """The work-coordinate-system line that opens a tube program. Default G54 (the
        operator zeros it to the tube for this job); an alternate fixed WCS (e.g. G55) is
        opt-in for a permanently-fixtured jig whose zero persists in its own system."""
        if self.tube_wcs == 'G54':
            return f'{self.tube_wcs}  ; Work coordinate system, zeroed at the tube origin'
        return f'{self.tube_wcs}  ; Use fixed jig work coordinate system'

    def _tube_wcs_reset_gcode(self):
        """Reset back to the standard G54 WCS at program end - only needed when the tube job
        actually switched to an alternate fixed WCS. Returns None for the G54 default (there
        was nothing to switch away from), keeping that output minimal."""
        if self.tube_wcs == 'G54':
            return None
        return 'G54  ; Reset to standard work coordinate system'

    def _tube_wcs_setup_instruction(self) -> str:
        """Plain-text instruction for how the operator establishes the tube origin, matched
        to the configured WCS. Shared by the G-code header comment and the UI setup list."""
        if self.tube_wcs == 'G54':
            return 'Zero G54 at the tube origin for this job'
        return f'Verify {self.tube_wcs} is set to the fixed jig origin'

    def _tube_wcs_setup_comment(self) -> str:
        """The numbered G-code header comment wrapping the WCS setup instruction."""
        return f'( 2. {self._tube_wcs_setup_instruction()} )'

    def _approach_ramp_start(self, ramp_start_height: float) -> List[str]:
        """Emit the Z approach down to the ramp-start height (just above the stock), where
        the helical/ramp entry begins.

        Between features the tool is already parked at the clearance plane
        (retract_height), so this is a single slow feed covering only the small air gap
        down to ramp start. But at job start (and after any pause/park) the tool sits up at
        safe height, potentially several inches up; feeding that whole gap at approach_rate
        wastes time. In that case, first rapid (G0) down to the clearance plane so the slow
        feed only covers the last bit of air above the stock."""
        lines = []
        if self._pending_clearance_rapid:
            lines.append(f"G0 Z{self.retract_height:.4f}  ; Rapid down to clearance plane")
            self._pending_clearance_rapid = False
        lines.append(f"G1 Z{ramp_start_height:.4f} F{self.approach_rate}  ; Approach to ramp start height")
        return lines

    def _generate_gcode_header(self, timestamp: str = None, is_multilayer: bool = False,
                               is_job: bool = False, job_part_count: int = None,
                               tool_table: List[str] = None,
                               operations_override: str = None,
                               entry_notes: List[str] = None) -> List[str]:
        """Generate common G-code header (comments + initialization).

        is_job: emit a single shared header for a multi-part job (one spindle start,
        one WCS, one safe-Z) instead of a per-part header. job_part_count is shown
        in the comments. Multi-part jobs are single-layer (2.5D is single-part).

        tool_table: for a multi-tool program, one already-sanitized description line per
        tool (see tooling.build_tool_table). Replaces the single "(Tool: ...)" line with
        a listed table plus the manual-tool-change warning, since no one tool describes
        the program. operations_override replaces the derived operations summary, which
        is likewise per-tool rather than per-part in a multi-tool program."""
        gcode = []

        # Use provided timestamp or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # request.form.get('timestamp') is whatever the browser sent. Slicing to 16
        # characters shortens it; it does not make it safe for a comment.
        timestamp_display = sanitize_comment(timestamp[:16], 'unknown')

        # Title
        gcode.append(f"({sanitize_comment(self.team_name, 'PenguinCAM').upper()} - Team {self.team_number})")
        if is_multilayer:
            gcode.append("(PenguinCAM CNC Post-Processor - MULTI-LAYER)")
        elif is_job:
            gcode.append("(PenguinCAM CNC Post-Processor - MULTI-PART JOB)")
        else:
            gcode.append("(PenguinCAM CNC Post-Processor)")

        if hasattr(self, 'user_name'):
            # A Google/Onshape display name reads like "Trent Fox (Mentor) Jose" -
            # a nested paren and a non-ASCII byte, both forbidden, on line 3 of every
            # program the hosted app produces.
            gcode.append(f"(Generated by: {sanitize_comment(self.user_name, 'unknown')} on {timestamp_display})")
        else:
            gcode.append(f"(Generated on: {timestamp_display})")
        gcode.append("")

        # Machine info (sanitized: comments must be ASCII with no parens or brackets)
        machine_name = sanitize_comment(self.machine_name, 'Unknown')
        controller = sanitize_comment(self.machine_controller, 'Unknown')

        gcode.append(f"(Machine: {machine_name})")
        gcode.append(f"(Controller: {controller})")

        if not is_multilayer:
            gcode.append(f"(Machine bounds: X={self.config.machine_x_max:.1f}\" Y={self.config.machine_y_max:.1f}\" Z={self.config.machine_z_max:.1f}\")")

        gcode.append(f"(Units: {'Inches' if self.units == 'inch' else 'Millimeters'}" +
                    (f" - {'G20' if self.units == 'inch' else 'G21'})" if not is_multilayer else ")"))

        if not is_multilayer:
            gcode.append("(Coordinate system: G54)")
            gcode.append("(Plane: G17 - XY)")
            gcode.append("(Arc centers: Incremental - G91.1)")

        gcode.append("")

        # Material and tool
        material_info = f"{self.material_thickness}\""
        if hasattr(self, 'material_name'):
            material_info = f"{sanitize_comment(self.material_name, 'material')} - {material_info} thick"
        else:
            material_info = f"{material_info} thick"

        gcode.append(f"(Material: {material_info})")
        if tool_table:
            gcode.append(f"(Tools: {len(tool_table)} - MANUAL TOOL CHANGES REQUIRED)")
            for line in tool_table:
                gcode.append(f"(  {line})")
            gcode.append("(The program pauses and parks at each change - swap the tool,)")
            gcode.append(
                f"(re-zero G54 Z to {self.z_zero_surface()}, then press CYCLE START.)")
            gcode.append("(** Do NOT touch the X or Y zero between tools **)")
        else:
            gcode.append(f"(Tool: {self.tool_diameter}\" diam {self.tool_flutes}-flute Flat End Mill)")
            gcode.append(f"(Spindle: {self.spindle_speed} RPM)")
        if getattr(self, 'feed_scale_note', None):
            # The program derated itself for this tool; the operator deserves to know
            # the numbers in the moves below are deliberate, not a stale preset.
            gcode.append(f"({sanitize_comment(self.feed_scale_note)})")

        if is_job and job_part_count is not None:
            gcode.append(f"(Parts in job: {job_part_count})")

        if is_multilayer:
            if hasattr(self, 'layer_data'):
                gcode.append(f"(Layers: {len(self.layer_data)} depths)")
        else:
            gcode.append(f"(Coolant: {sanitize_comment(self.machine_coolant, 'None')})")

        gcode.append("")

        if not is_multilayer:
            # Z-axis info (only for single-layer)
            gcode.append(f"(ZMIN: {self.cut_depth:.4f}\")")
            gcode.append(f"(Retract Z: {self.retract_height:.4f}\")")
            gcode.append("")

            # Operations
            if operations_override is not None:
                operations_str = operations_override
            elif is_job:
                # Per-part operations are listed in each PART section; the job-level
                # header just records that this is a multi-part program. No nested parens
                # in the comment (CNC controllers choke on them).
                operations_str = f"Multi-part job - {job_part_count} parts"
            else:
                operations = []
                if self.holes:
                    operations.append("Holes")
                if self.pockets:
                    operations.append("Pockets")
                if self.perimeter:
                    operations.append("Profile")
                operations_str = ", ".join(operations) if operations else "None"

            gcode.append(f"(Operations: {operations_str})")
            # "No straight plunges" is a promise about how the tool enters the work, and
            # it is false the moment a twist drill is involved - drilling IS a straight
            # plunge. A program that tells the operator otherwise is worse than one that
            # says nothing, so a drilling job replaces these lines rather than adding to
            # them. See tooling.assemble_job, which supplies them.
            for note in (entry_notes if entry_notes is not None
                         else [f"(Helical entry angle: ~{int(self.ramp_angle)} deg)",
                               "(No straight plunges)"]):
                gcode.append(note)
            gcode.append("")

        # The Z reference goes in EVERY program, layered or not: it is the one thing the
        # operator has to match on the machine, and getting it wrong puts every cut a
        # material thickness out.
        if is_multilayer:
            gcode.append(f"(ZMIN: {self.cut_depth:.4f}\")")
            gcode.append(f"(Retract Z: {self.retract_height:.4f}\")")
            gcode.append("")
        gcode.extend(self._dry_run_banner())

        gcode.append("(Z-AXIS REFERENCE:)")
        gcode.append("(  Z=0 is at " + ("TOP OF STOCK" if self.z_datum == Z_DATUM_STOCK_TOP
                                        else "SACRIFICE BOARD surface") + ")")
        gcode.append(f"(  Material top: Z={self.material_top:.4f}\")")
        gcode.append(f"(  Cuts {self.sacrifice_board_depth:.4f}\" into sacrifice board)")
        gcode.append(f"(  ** ZERO Z TO {self.z_zero_surface().upper()}, VERIFY BEFORE RUNNING **)")
        gcode.append("")

        # Modal G-code setup
        gcode.append("G90 G94 G91.1 G40 G49 G17")
        gcode.append("G92.1  ; Cancel any temporary coordinate offset")

        if not is_multilayer:
            gcode.append("(G90=Absolute, G94=Feed/min, G91.1=Arc centers incremental - IJK relative to start point, G40=Cutter comp cancel, G49=Tool length comp cancel, G17=XY plane)")

        # Units
        if self.units == "inch":
            gcode.append("G20  ; Inches")
        else:
            gcode.append("G21  ; Millimeters")

        # Ensure absolute positioning mode
        gcode.append("G90  ; Absolute positioning mode")
        gcode.append("")

        gcode.extend(self._aluminum_preflight_gcode())

        # Spindle on
        gcode.extend(self._spindle_start_gcode(
            '' if is_multilayer else f' at {self.spindle_speed} RPM'))
        coolant_on = None if self.is_dry_run else self._coolant_on_gcode()
        if coolant_on:
            gcode.append(coolant_on)
        gcode.append("")

        # Set work coordinate system
        gcode.append("G54  ; " + ("Work coordinate system" if is_multilayer else "Use work coordinate system 1"))
        gcode.append("")

        # Initial positioning: retract to a safe height in WORK coordinates (G54) so this
        # is portable across controllers - no G53 machine move (which assumes machine Z=0
        # is a safe high position, an assumption that breaks on GRBL/Easel/WinCNC).
        gcode.append(f"G0 Z{self._safe_z():.4f}  ; Safe Z clearance")
        gcode.append("G0 X0 Y0  ; " + ("Origin" if is_multilayer else "Rapid to work origin"))
        gcode.append("")

        # Tool is parked at safe height; the first feature must rapid down to the
        # clearance plane before its slow plunge feed (see _approach_ramp_start).
        self._pending_clearance_rapid = True

        return gcode

    def _generate_gcode_footer(self) -> List[str]:
        """Generate common G-code footer (safe moves + shutdown)"""
        gcode = []
        gcode.append("(===== FINISH =====)")
        gcode.append(f"G0 Z{self._safe_z():.4f}  ; Safe Z clearance")
        coolant_off = self._coolant_off_gcode()
        if coolant_off:
            gcode.append(coolant_off)
        gcode.append("M5  ; Spindle off")
        gcode.extend(self._park_gcode('Park for part access'))  # G53 park only if configured
        gcode.append("M30  ; Program end")
        gcode.append("")
        return gcode

    def _convert_to_shapely_polygons(self, circles, polylines):
        """
        Convert circles and polylines to Shapely Polygon objects.
        Handles HATCH entities (multiple boundaries = polygon with holes).
        Detects concentric circles and creates ring polygons.

        Args:
            circles: List of circle dicts with 'center' and 'radius'
            polylines: List of polyline coordinate lists

        Returns:
            List of Shapely Polygon objects (may have interior holes)
        """
        polygons = []
        # Simple (single-loop) shapes that still need containment/nesting resolution.
        # Concentric-circle rings are resolved directly into `polygons` below.
        simple_polys = []

        # Detect concentric circles (same center, different radii)
        # These should become ring polygons (donut shapes with holes)
        used_circles = set()

        for i, circle1 in enumerate(circles):
            if i in used_circles:
                continue

            center1 = circle1['center']
            radius1 = circle1['radius']

            # Look for concentric circles
            concentric_group = [circle1]
            for j, circle2 in enumerate(circles):
                if i == j or j in used_circles:
                    continue

                center2 = circle2['center']
                radius2 = circle2['radius']

                # Check if centers are the same (within tolerance)
                dx = abs(center1[0] - center2[0])
                dy = abs(center1[1] - center2[1])
                if dx < 0.001 and dy < 0.001:
                    # Concentric!
                    concentric_group.append(circle2)
                    used_circles.add(j)

            used_circles.add(i)

            # Create polygon(s) from this group
            if len(concentric_group) == 1:
                # Single circle - simple filled polygon.
                # Defer to nesting so a circle that sits inside a polygonal pocket
                # (or vice versa) becomes an island/hole rather than an overlapping solid.
                poly = Point(center1).buffer(radius1)
                simple_polys.append(poly)
            else:
                # Multiple concentric circles - create ring with holes
                # Sort by radius (largest first)
                concentric_group.sort(key=lambda c: c['radius'], reverse=True)

                # Outer boundary is the largest circle
                outer_circle = concentric_group[0]
                outer_poly = Point(outer_circle['center']).buffer(outer_circle['radius'])

                # Interior holes are the other circles
                holes = []
                for inner_circle in concentric_group[1:]:
                    inner_poly = Point(inner_circle['center']).buffer(inner_circle['radius'])
                    # Get exterior coords as hole
                    hole_coords = list(inner_poly.exterior.coords)
                    holes.append(hole_coords)

                # Create polygon with holes
                outer_coords = list(outer_poly.exterior.coords)
                ring_poly = Polygon(outer_coords, holes=holes)
                if ring_poly.is_valid:
                    polygons.append(ring_poly)
                    print(f"      Detected concentric circles: outer r={outer_circle['radius']:.3f}\", {len(holes)} inner hole(s)")

        # Add polyline loops to the simple-shape pool.
        for polyline in polylines:
            if len(polyline) >= 3:
                try:
                    poly = Polygon(polyline)
                    if poly.is_valid and not poly.is_empty:
                        simple_polys.append(poly)
                except Exception:
                    pass

        # Resolve containment across all simple loops at once: an enclosed loop
        # becomes an interior hole of its parent, and a loop enclosed by a hole
        # becomes a solid island. Handles HATCH faces with any number of boundaries
        # (e.g. a pocket containing two raised bosses), which the old 2-loop
        # special case flattened into overlapping solids.
        polygons.extend(self._nest_polygons(simple_polys))

        return polygons

    def _nest_polygons(self, polys):
        """Resolve a flat list of single-loop polygons into solids-with-holes using
        even/odd containment depth.

        Loops at even nesting depth (0, 2, ...) are solid regions; loops at odd
        depth are holes in their enclosing solid. A solid's holes are its direct
        children; grandchildren (islands within a hole) are emitted as their own
        solids. Falls back to treating a loop as a separate solid if its nested
        construction is invalid.

        Args:
            polys: list of valid Shapely Polygons (exterior loops only)

        Returns:
            List of Shapely Polygons, some carrying interior holes.
        """
        candidates = [p for p in polys if p is not None and p.is_valid and not p.is_empty]
        if not candidates:
            return []

        # Largest first so a polygon's potential parents are already indexed.
        candidates.sort(key=lambda p: p.area, reverse=True)

        n = len(candidates)
        parent = [None] * n  # index of immediate (smallest) enclosing polygon
        for i in range(n):
            inner = candidates[i]
            # representative_point is guaranteed inside the polygon, robust for containment
            probe = inner.representative_point()
            best_parent = None
            best_area = None
            for j in range(n):
                if j == i:
                    continue
                outer = candidates[j]
                if outer.area <= inner.area:
                    continue
                if outer.contains(probe):
                    if best_area is None or outer.area < best_area:
                        best_area = outer.area
                        best_parent = j
            parent[i] = best_parent

        # Nesting depth = number of ancestors.
        def depth_of(idx):
            d = 0
            cur = parent[idx]
            while cur is not None:
                d += 1
                cur = parent[cur]
            return d

        depth = [depth_of(i) for i in range(n)]

        results = []
        for i in range(n):
            if depth[i] % 2 != 0:
                # Odd depth -> this loop is a hole, consumed by its even-depth parent.
                continue
            # Even depth -> solid. Its holes are direct children (odd depth).
            hole_rings = [
                list(candidates[c].exterior.coords)
                for c in range(n)
                if parent[c] == i
            ]
            exterior = list(candidates[i].exterior.coords)
            if hole_rings:
                try:
                    solid = Polygon(exterior, holes=hole_rings)
                    if solid.is_valid and not solid.is_empty:
                        results.append(solid)
                        continue
                except Exception:
                    pass
                # Fallback: emit the exterior solid without holes rather than lose it.
                results.append(Polygon(exterior))
            else:
                results.append(candidates[i])

        return results

    def _subtract_geometry(self, circles, polylines, cut_geometry):
        """
        Subtract geometry from circles and polylines.
        Used to remove areas that will be (or have been) cut by other operations.
        Returns new lists with subtracted geometry.
        """
        if cut_geometry is None or cut_geometry.is_empty:
            return circles, polylines

        new_circles = []
        new_polylines = []

        # Process circles
        for circle in circles:
            center = circle['center']
            radius = circle['radius']
            circle_geom = Point(center).buffer(radius)

            # Subtract already cut areas
            result = circle_geom.difference(cut_geometry)

            # If circle is completely covered by cut geometry, skip it
            if result.is_empty or result.area < 0.0001:
                print(f"    Circle at {center} fully removed by subtraction - skipping")
                continue

            # If circle remains mostly intact (>90% area), keep it as-is
            if result.area / circle_geom.area > 0.9:
                new_circles.append(circle)
            else:
                # Circle partially overlaps - convert remainder to polyline(s)
                print(f"    Circle at {center} partially overlaps - converting to polyline")
                if isinstance(result, Polygon):
                    coords = list(result.exterior.coords)[:-1]
                    if len(coords) >= 3:
                        new_polylines.append(coords)
                elif isinstance(result, MultiPolygon):
                    for poly in result.geoms:
                        coords = list(poly.exterior.coords)[:-1]
                        if len(coords) >= 3:
                            new_polylines.append(coords)

        # Process polylines
        for polyline in polylines:
            if len(polyline) < 3:
                continue

            try:
                poly = Polygon(polyline)
                if not poly.is_valid:
                    continue

                # Subtract already cut areas
                result = poly.difference(cut_geometry)

                # If completely covered by cut geometry, skip
                if result.is_empty or result.area < 0.0001:
                    print(f"    Polyline fully removed by subtraction - skipping")
                    continue

                # Extract remaining geometry
                if isinstance(result, Polygon):
                    coords = list(result.exterior.coords)[:-1]
                    if len(coords) >= 3:
                        new_polylines.append(coords)
                    # Also add holes as separate polylines
                    for interior in result.interiors:
                        coords = list(interior.coords)[:-1]
                        if len(coords) >= 3:
                            new_polylines.append(coords)
                elif isinstance(result, MultiPolygon):
                    for poly in result.geoms:
                        coords = list(poly.exterior.coords)[:-1]
                        if len(coords) >= 3:
                            new_polylines.append(coords)
                        for interior in poly.interiors:
                            coords = list(interior.coords)[:-1]
                            if len(coords) >= 3:
                                new_polylines.append(coords)
            except Exception as e:
                # If subtraction fails, keep original polyline
                print(f"    Warning: Could not subtract from polyline: {e}")
                new_polylines.append(polyline)

        print(f"    Before: {len(circles)} circles, {len(polylines)} polylines")
        print(f"    After:  {len(new_circles)} circles, {len(new_polylines)} polylines")

        return new_circles, new_polylines

    def _dissolve_thin_islands(self, polygon, min_wall, deeper_geom):
        """Drop interior islands that leave a too-thin wall AND are removed by a deeper pass.

        An island in a sliced layer is one of two things:
        (a) a region carved out by subtracting a DEEPER layer - that material is
            removed by the deeper pass regardless, so keeping the island is purely an
            efficiency choice (avoid re-cutting what a deeper pass clears); or
        (b) a native designed hole at THIS depth (e.g. a real ring groove) whose
            interior is kept material.
        Only (a) is safe to dissolve. When keeping an (a) island would leave a wall
        too thin for the tool, dissolve it: the shallow pass mills across it and the
        deeper pass still removes it, so the finished part is unchanged. A too-thin
        (b) island is a genuine "groove too narrow" error and must be preserved.
        `deeper_geom` is the deeper-cut region actually subtracted during slicing (or
        None); an island counts as (a) only if it lies within that region.
        """
        if not polygon.interiors or deeper_geom is None:
            return polygon
        interiors = list(polygon.interiors)
        kept = []
        for i, ring in enumerate(interiors):
            wall = polygon.exterior.distance(ring)
            for j, other in enumerate(interiors):
                if i != j:
                    wall = min(wall, ring.distance(other))
            island = Polygon(ring)
            removed_deeper = (island.area > 0 and
                              deeper_geom.intersection(island).area >= 0.99 * island.area)
            if wall < min_wall and removed_deeper:
                print(f"    Dissolving island (area={island.area:.3f} sq in): wall {wall:.4f}\" "
                      f"< tool {min_wall:.4f}\" and removed by a deeper pass anyway")
            else:
                kept.append(ring)
        if len(kept) == len(interiors):
            return polygon
        return Polygon(polygon.exterior.coords, [list(r.coords) for r in kept])

    def _generate_multilayer_gcode(self, suggested_filename: str = None, timestamp: str = None) -> PostProcessorResult:
        """Generate G-code for multi-layer DXF (2.5D machining)"""
        print("\n" + "="*70)
        print("MULTI-LAYER PROCESSING")
        print("="*70)

        # Sort layers: deepest first
        sorted_layers = sorted(self.layer_data.items(), key=lambda x: x[1]['depth'])

        # Find the bottom face layer
        # NEW COORDINATE SYSTEM: Z=0 is sacrifice board surface
        # Bottom face should be at Z ≈ 0, top face at Z ≈ material_thickness
        expected_bottom_depth = 0.0
        tolerance = 0.01  # 0.01" tolerance for matching bottom face

        bottom_layer = None
        bottom_layer_name = None

        # Look for a layer at the expected bottom depth (Z ≈ 0)
        for layer_name, layer_info in self.layer_data.items():
            if abs(layer_info['depth'] - expected_bottom_depth) < tolerance:
                bottom_layer = (layer_name, layer_info)
                bottom_layer_name = layer_name
                print(f"✓ Found bottom face layer: {layer_name} at Z={layer_info['depth']:.4f}\" (expected {expected_bottom_depth:.4f}\")")
                break

        if not bottom_layer:
            # No bottom face found - use lowest Z layer for perimeter extraction only
            # ALL layers (including lowest) are treated as depth layers for pockets/holes
            lowest_layer = min(self.layer_data.items(), key=lambda x: x[1]['depth'])
            bottom_layer = lowest_layer
            bottom_layer_name = lowest_layer[0]
            print(f"⚠️  No bottom face at Z={expected_bottom_depth:.4f}\" found in DXF")
            print(f"   Using lowest layer {bottom_layer_name} at Z={lowest_layer[1]['depth']:.4f}\" for perimeter outline")
            print(f"   All layers (including lowest) will be processed as depth layers")

        # Separate layers: pocket layers (excluding bottom face and top surface)
        # - Bottom face layer (at Z ≈ 0): used for perimeter + through-holes/pockets
        # - Top surface layer (at Z ≈ material_thickness): reference geometry, not machined
        # - Middle layers (0 < Z < material_thickness): actual pockets/grooves to machine at specified depth
        # - If no true bottom face exists, ALL layers become depth layers
        has_true_bottom = abs(bottom_layer[1]['depth'] - expected_bottom_depth) < tolerance

        if has_true_bottom:
            # Exclude bottom face from depth layers
            # Process layers where 0 < Z < material_thickness (intermediate pockets)
            depth_layers = [
                item for item in sorted_layers
                if item[0] != bottom_layer_name and 0.01 < item[1]['depth'] < self.material_thickness - 0.01
            ]
        else:
            # Include all valid layers as depth layers
            # Process layers where 0 < Z < material_thickness
            depth_layers = [
                item for item in sorted_layers
                if 0.01 < item[1]['depth'] < self.material_thickness - 0.01
            ]

        print(f"\nProcessing order:")
        for i, (layer_name, layer_info) in enumerate(depth_layers, 1):
            if has_true_bottom or layer_name != bottom_layer_name:
                print(f"  {i}. {layer_name} (Z={layer_info['depth']:.4f}\") - pocket/groove at specified depth")

        # Report skipped layers
        for layer_name, layer_info in sorted_layers:
            # Skip if it's the bottom layer (handled separately) OR if it's at/above material thickness (top reference)
            if layer_name != bottom_layer_name and layer_info['depth'] >= self.material_thickness - 0.01:
                print(f"  → Skipping {layer_name} (Z={layer_info['depth']:.4f}\") - top surface reference geometry")

        if has_true_bottom:
            print(f"  {len(depth_layers) + 1}. {bottom_layer_name} (Z={bottom_layer[1]['depth']:.4f}\") - PERIMETER + through-holes/pockets (last)")
        else:
            print(f"  {len(depth_layers) + 1}. {bottom_layer_name} - PERIMETER OUTLINE ONLY (already processed pockets at specified depth)")

        # Generate timestamp if not provided
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Generate header
        gcode = self._generate_gcode_header(timestamp, is_multilayer=True)
        warnings = []

        # Track total features across all layers
        total_holes = 0
        total_pockets = 0

        # Process each depth layer (deepest to shallowest, excluding top/perimeter)
        for layer_name, layer_info in depth_layers:
            depth = layer_info['depth']
            # The DXF's layer depths are heights above the sacrifice board (the deepest
            # is the stock thickness - that is where load_dxf gets the thickness from),
            # so they are Z coordinates in the BOARD frame. Move them into whichever
            # frame this program is being written in; z_shift is 0 unless the operator
            # is zeroing on the stock top, in which case every layer drops by the stock
            # thickness. Without this the layers were cut at their board-frame numbers -
            # a full thickness above the material, i.e. in the air over it.
            layer_z = depth + self.z_shift
            print(f"\nGenerating toolpaths for {layer_name} at Z={layer_z:.4f}\"")

            gcode.append(f"(===== LAYER: {layer_name} | DEPTH: Z={layer_z:.4f}\" =====)")

            # === SHAPELY POLYGON APPROACH ===
            # Get Shapely Polygons for this layer
            current_polygons = layer_info['polygons']
            print(f"  Current layer: {len(current_polygons)} polygon(s)")

            # === PROPER 3D SLICING LOGIC ===
            # Find the next deeper layer
            next_deeper_layer = None
            next_deeper_depth = None
            for check_name, check_info in self.layer_data.items():
                check_depth = check_info['depth']
                if check_depth < depth - 0.001:  # Deeper than current
                    if next_deeper_depth is None or check_depth > next_deeper_depth:
                        next_deeper_layer = check_name
                        next_deeper_depth = check_depth

            # If no explicit deeper layer, check if bottom face exists and is deeper
            if has_true_bottom and bottom_layer[1]['depth'] < depth - 0.001:
                if next_deeper_depth is None or bottom_layer[1]['depth'] > next_deeper_depth:
                    next_deeper_layer = bottom_layer_name
                    next_deeper_depth = bottom_layer[1]['depth']

            # Region removed by ALL strictly-deeper passes, so a thin-wall island is
            # dissolved only when that material is genuinely cut away later. The
            # bottom/through face is stored as the KEEP plate (octagon-with-holes), so
            # its removed material is its HOLES (interiors); deeper pocket layers store
            # the removed pocket directly, so use those polygons as-is.
            deeper_removed_geoms = []
            for _lname, _linfo in self.layer_data.items():
                if _linfo['depth'] < depth - 0.001:
                    if has_true_bottom and _lname == bottom_layer_name:
                        for _p in _linfo['polygons']:
                            deeper_removed_geoms.extend(Polygon(r) for r in _p.interiors)
                    else:
                        deeper_removed_geoms.extend(_linfo['polygons'])
            deeper_removed = unary_union(deeper_removed_geoms) if deeper_removed_geoms else None

            # Get geometry at next deeper layer and perform slicing
            if next_deeper_layer:
                next_layer_info = self.layer_data[next_deeper_layer]
                deeper_polygons = next_layer_info['polygons']
                print(f"  Next deeper layer: {next_deeper_layer} at Z={next_deeper_depth:.4f}\"")
                print(f"    Deeper geometry: {len(deeper_polygons)} polygon(s)")

                # For bottom face: exclude perimeter from subtraction
                # The perimeter is the outermost boundary (part outline), not cleared material
                if next_deeper_layer == bottom_layer_name and has_true_bottom:
                    # Identify which polygon is the perimeter (largest area)
                    if deeper_polygons:
                        perimeter_polygon = max(deeper_polygons, key=lambda p: p.area)
                        # Exclude perimeter, keep only interior features (holes/pockets)
                        deeper_polygons_for_subtraction = [p for p in deeper_polygons if p != perimeter_polygon]
                        print(f"    Excluding perimeter from subtraction (area={perimeter_polygon.area:.3f} sq in)")
                        print(f"    Using {len(deeper_polygons_for_subtraction)} interior feature(s) for subtraction")
                    else:
                        deeper_polygons_for_subtraction = []
                else:
                    deeper_polygons_for_subtraction = deeper_polygons

                # SLICING: material_to_machine = current_solid - deeper_solid
                if not deeper_polygons_for_subtraction:
                    # No geometry to subtract
                    result = unary_union(current_polygons) if len(current_polygons) > 1 else (current_polygons[0] if current_polygons else None)
                else:
                    current_union = unary_union(current_polygons) if len(current_polygons) > 1 else current_polygons[0]
                    deeper_union = unary_union(deeper_polygons_for_subtraction) if len(deeper_polygons_for_subtraction) > 1 else deeper_polygons_for_subtraction[0]
                    result = current_union.difference(deeper_union)

                # Convert result back to list of polygons
                if result.is_empty:
                    sliced_polygons = []
                    print(f"  Result: No material to machine (fully covered by deeper layer)")
                elif isinstance(result, Polygon):
                    sliced_polygons = [result]
                    print(f"  Result: 1 polygon to machine")
                elif isinstance(result, MultiPolygon):
                    sliced_polygons = list(result.geoms)
                    print(f"  Result: {len(sliced_polygons)} polygons to machine")
                else:
                    # Other geometry types (unlikely but handle gracefully)
                    sliced_polygons = []
                    print(f"  Result: Unexpected geometry type {result.geom_type}")
            else:
                # No deeper layer - use all polygons as-is
                sliced_polygons = current_polygons
                print(f"  No deeper geometry to subtract - using all features as-is")

            # Convert Shapely Polygons back to circles/polylines for existing toolpath generation
            # Store Polygons with islands separately for special handling
            self.circles = []
            self.polylines = []
            self.pocket_polygons = []  # NEW: Store Polygon objects for island-aware machining

            for poly in sliced_polygons:
                if not poly.is_valid or poly.is_empty:
                    continue

                # Drop islands whose wall is too thin to machine IF a deeper pass
                # removes that material anyway (see _dissolve_thin_islands).
                poly = self._dissolve_thin_islands(poly, self.tool_diameter, deeper_removed)

                # Check if polygon has interior holes (islands)
                if len(poly.interiors) > 0:
                    # Store the complete Polygon object for island-aware machining
                    self.pocket_polygons.append(poly)
                    print(f"    Polygon with {len(poly.interiors)} interior hole(s) - will be machined as ring/pocket with islands")
                else:
                    # Simple polygon - extract as polyline
                    exterior_coords = list(poly.exterior.coords)[:-1]  # Remove duplicate last point
                    if len(exterior_coords) >= 3:
                        self.polylines.append(exterior_coords)

            print(f"  Converted to {len(self.circles)} circles, {len(self.polylines)} polylines, {len(self.pocket_polygons)} island-aware pockets")

            # Classify geometry (holes, pockets) - reuses existing methods
            self.classify_holes()

            # For depth layers: treat ALL polylines as pockets (no perimeter)
            self.perimeter = None
            self.pockets = self.polylines.copy() if self.polylines else []

            # The DXF layer height, expressed in this program's Z frame (see layer_z
            # above). A pocket on the DXF's Z=0.050" layer is 0.050" above the board
            # whichever surface the operator zeroed on; only the number changes.
            saved_cut_depth = self.cut_depth
            self.cut_depth = layer_z

            # Generate toolpaths at this depth
            # Apply same contouring logic as 2D mode
            threshold_area = self._contour_threshold_area()

            # A through-cut is one that reaches the bottom face of the stock - which is
            # Z=0 only in the board frame. Only through-cuts are contoured; a
            # partial-depth layer has to be cleared out completely.
            is_through_cut = self.is_through_cut()

            if self.holes:
                gcode.append(f"(Layer {layer_name}: {len(self.holes)} holes)")
                total_holes += len(self.holes)

                # Separate holes by size (only contour through-cuts)
                contoured_holes = []
                cleared_holes = []
                for hole in self.holes:
                    hole_area = math.pi * (hole['diameter'] / 2) ** 2
                    if is_through_cut and hole_area > threshold_area:
                        contoured_holes.append(hole)
                    else:
                        cleared_holes.append(hole)

                # Process cleared holes
                for hole in cleared_holes:
                    center = hole['center']
                    diameter = hole['diameter']
                    needs_peck = hole.get('needs_peck_drill', False)
                    gcode.extend(self._clear_in_depth_levels(
                        lambda c=center, d=diameter, pk=needs_peck:
                        self._generate_hole_gcode(c[0], c[1], d, needs_peck_drill=pk)))

                # Process contoured holes
                for hole in contoured_holes:
                    center = hole['center']
                    diameter = hole['diameter']
                    # Generate circular points for contouring (50-segment tessellation)
                    circle_points = self._tessellate_circle(center[0], center[1], diameter / 2)
                    gcode.append(f"(Large hole {diameter:.3f}\" dia - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(circle_points))

            if self.pockets:
                gcode.append(f"(Layer {layer_name}: {len(self.pockets)} pockets)")
                total_pockets += len(self.pockets)

                # Separate pockets by size (only contour through-cuts)
                contoured_pockets = []
                cleared_pockets = []
                for pocket in self.pockets:
                    pocket_poly = Polygon(pocket)
                    pocket_area = pocket_poly.area
                    if is_through_cut and pocket_area > threshold_area:
                        contoured_pockets.append(pocket)
                    else:
                        cleared_pockets.append(pocket)

                # Process cleared pockets
                for pocket in cleared_pockets:
                    gcode.extend(self._clear_in_depth_levels(
                        lambda pocket=pocket: self._generate_pocket_gcode(pocket)))

                # Process contoured pockets
                for pocket in contoured_pockets:
                    pocket_poly = Polygon(pocket)
                    gcode.append(f"(Large pocket {pocket_poly.area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(pocket))

            # Process island-aware pockets (Polygons with interior holes)
            if hasattr(self, 'pocket_polygons') and self.pocket_polygons:
                gcode.append(f"(Layer {layer_name}: {len(self.pocket_polygons)} island-aware pockets)")
                total_pockets += len(self.pocket_polygons)

                for pocket_poly in self.pocket_polygons:
                    # For now, these are always cleared (no contouring for rings/grooves)
                    # In the future, could add size threshold check here too
                    gcode.append(f"(Ring/groove pocket with {len(pocket_poly.interiors)} islands)")
                    gcode.extend(self._clear_in_depth_levels(
                        lambda pocket_poly=pocket_poly: self._generate_pocket_gcode_from_polygon(pocket_poly)))

            # Restore original cut depth
            self.cut_depth = saved_cut_depth
            gcode.append("")

        # Process bottom face for perimeter
        layer_name, layer_info = bottom_layer
        depth = layer_info['depth']
        print(f"\nGenerating perimeter from {layer_name}")
        bottom_z = depth + self.z_shift
        if has_true_bottom:
            print(f"  Bottom face is at Z={bottom_z:.4f}\" - cutting perimeter through material")
        else:
            print(f"  Using deepest layer Z={bottom_z:.4f}\" for perimeter outline only")

        gcode.append(f"(===== LAYER: {layer_name} | PERIMETER =====)")

        # Use bottom face geometry
        self.circles = layer_info['circles']
        self.polylines = layer_info['polylines']

        # Identify perimeter from bottom face (must come before classify_holes to remove perimeter circles)
        self.identify_perimeter_and_pockets()

        # Classify remaining circles as holes (after perimeter circles removed)
        self.classify_holes()

        # Generate holes and pockets ONLY if this is a true bottom face (through-cuts)
        # Otherwise they were already processed as depth layers
        if has_true_bottom:
            gcode.append(f"(Bottom face at Z={bottom_z:.4f}\" - through-holes and through-pockets)")

            # Apply contouring logic to bottom face (same as depth layers)
            threshold_area = self._contour_threshold_area()

            # Bottom face is always through-cut (Z=0)
            is_through_cut = True

            if self.holes:
                gcode.append("(===== HOLES =====)")
                total_holes += len(self.holes)

                # Separate holes by size (only contour through-cuts)
                contoured_holes = []
                cleared_holes = []
                for hole in self.holes:
                    hole_area = math.pi * (hole['diameter'] / 2) ** 2
                    if is_through_cut and hole_area > threshold_area:
                        contoured_holes.append(hole)
                    else:
                        cleared_holes.append(hole)

                # Process cleared holes
                for i, hole in enumerate(cleared_holes, 1):
                    center = hole['center']
                    diameter = hole['diameter']
                    needs_peck = hole.get('needs_peck_drill', False)
                    gcode.append(f"(Hole {i} - {diameter:.3f}\" diameter)")
                    gcode.extend(self._clear_in_depth_levels(
                        lambda c=center, d=diameter, pk=needs_peck:
                        self._generate_hole_gcode(c[0], c[1], d, needs_peck_drill=pk)))
                    gcode.append("")

                # Process contoured holes
                for i, hole in enumerate(contoured_holes, 1):
                    center = hole['center']
                    diameter = hole['diameter']
                    # Generate circular points for contouring (50-segment tessellation)
                    circle_points = self._tessellate_circle(center[0], center[1], diameter / 2)
                    gcode.append(f"(Hole {len(cleared_holes) + i} - {diameter:.3f}\" diameter - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(circle_points))
                    gcode.append("")

            if self.pockets:
                gcode.append("(===== POCKETS =====)")
                total_pockets += len(self.pockets)

                # Separate pockets by size (only contour through-cuts)
                contoured_pockets = []
                cleared_pockets = []
                for pocket in self.pockets:
                    pocket_poly = Polygon(pocket)
                    pocket_area = pocket_poly.area
                    if is_through_cut and pocket_area > threshold_area:
                        contoured_pockets.append(pocket)
                    else:
                        cleared_pockets.append(pocket)

                # Process cleared pockets
                for i, pocket in enumerate(cleared_pockets, 1):
                    gcode.append(f"(Pocket {i})")
                    gcode.extend(self._clear_in_depth_levels(
                        lambda pocket=pocket: self._generate_pocket_gcode(pocket)))
                    gcode.append("")

                # Process contoured pockets
                for i, pocket in enumerate(contoured_pockets, 1):
                    pocket_poly = Polygon(pocket)
                    gcode.append(f"(Pocket {len(cleared_pockets) + i} - {pocket_poly.area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(pocket))
                    gcode.append("")
        else:
            gcode.append(f"(Perimeter outline from deepest layer - holes/pockets already cut at depth)")

        # Perimeter cut at full depth
        if self.perimeter:
            if self.pause_before_perimeter:
                gcode.extend(self._generate_pause_and_park_gcode(
                    'PAUSE FOR FIXTURING',
                    ['Internal features complete', 'Install fixturing', 'Secure part before perimeter']
                ))

            if self.tabs_enabled:
                gcode.append("(Perimeter with tabs)")
            else:
                gcode.append("(Perimeter - no tabs)")

            gcode.extend(self._generate_perimeter_gcode(self.perimeter))

        gcode.append("")

        # Footer
        gcode.extend(self._generate_gcode_footer())

        # Calculate estimated cycle time
        time_estimate = self._estimate_cycle_time(gcode)

        # Add cycle time to header (insert after the operations section)
        self._insert_cycle_time_comment(gcode, time_estimate)

        # Check for errors that occurred during generation
        if self.errors:
            return PostProcessorResult(
                success=False,
                errors=self.errors.copy()
            )

        # Generate filename (name sanitized for safe disk write + download)
        filename = build_output_filename(suggested_filename, timestamp, "output", dry_run=self.is_dry_run)

        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=warnings,
            stats={
                'num_holes': total_holes,
                'num_pockets': total_pockets,
                'has_perimeter': bool(self.perimeter) if hasattr(self, 'perimeter') else False,
                'num_layers': len(self.layer_data),
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell'])
            }
        )

    def _calculate_helical_passes(self, toolpath_radius: float, target_angle_deg: float = None, ramp_start_height: float = None) -> Tuple[int, float]:
        """
        Calculate number of helical passes needed for a safe plunge angle.

        Args:
            toolpath_radius: Radius of the circular toolpath
            target_angle_deg: Target plunge angle in degrees (default uses self.ramp_angle)
            ramp_start_height: Z height to start ramping from (default uses material_top + ramp_start_clearance)

        Returns:
            Tuple of (number_of_passes, depth_per_pass)
        """
        # Use material-specific ramp angle if not specified
        if target_angle_deg is None:
            target_angle_deg = self.ramp_angle

        # Use ramp start height if specified, otherwise use material_top + clearance
        if ramp_start_height is None:
            ramp_start_height = self.material_top + self.ramp_start_clearance

        # Total depth to cut (from ramp start height down to cut depth)
        total_depth = ramp_start_height - self.cut_depth

        # Circumference of one revolution
        circumference = 2 * math.pi * toolpath_radius

        # For target angle: depth_per_rev = circumference * tan(angle)
        target_depth_per_rev = circumference * math.tan(math.radians(target_angle_deg))

        # Number of passes needed
        num_passes = max(1, int(math.ceil(total_depth / target_depth_per_rev)))
        depth_per_pass = total_depth / num_passes

        return num_passes, depth_per_pass

    # ---- True drilling (twist drill) --------------------------------------------------
    # Everything else in this file assumes an end mill: it enters helically, feeds
    # sideways, and steps over to open a bore. A twist drill can do none of that. It has
    # no side cutting edges worth the name and no way to clear a lateral cut, so the only
    # motion it may ever make under load is straight down its own axis. These two methods
    # are the drilling path; nothing here emits an X or Y feed move.

    #: Included point angle of a standard twist drill, degrees. 118 is the general-purpose
    #: HSS grind; 135 is the split-point/harder-material grind.
    DEFAULT_DRILL_POINT_ANGLE = 118.0

    def _clear_in_depth_levels(self, emit):
        """Run a clearing toolpath once per depth level, never biting deeper than the
        depth-per-pass limit.

        Contour and tab-removal passes step down; pocket and bore CLEARING did not. The
        helix descended to full depth and the sweep then crossed the whole floor in
        virgin stock, so a 0.5" pocket took a 0.5" axial bite however small the operator
        set "max depth per pass" - the same full-thickness bite in a sibling function
        that snapped a 1/8" cutter in the tabs on 2026-08-24, and the exact thing
        docs/quick-reference-card.md promises this setting prevents for "profiles AND
        pockets".

        Rather than teach four different clearing strategies to step down, this hands
        each of them a THIN SLAB: material_top and cut_depth are narrowed to one level,
        so every generator's own arithmetic - helix start, ramp angle, clearing Z,
        re-entry - lands on that slab with no change inside it. `emit` is called once
        per level and must return a list of G-code lines.
        """
        total_depth = self.material_top - self.cut_depth
        # `max_slotting_depth` arrives with the material preset. Every route applies one,
        # but a caller that builds a post-processor and generates without one used to
        # reach this line and crash with an AttributeError rather than cut conservatively.
        limit = getattr(self, 'max_slotting_depth', None)
        if not limit or not math.isfinite(limit) or limit <= 0:
            limit = total_depth       # one pass: no ceiling was ever configured
        levels = self.passes_for_depth(total_depth, limit)
        if levels <= 1:
            return emit()

        saved_top, saved_bottom = self.material_top, self.cut_depth
        step = total_depth / levels
        gcode = [f"(Depth levels: {levels} at {step:.4f}\" each, "
                 f"max {limit:.4f}\" per pass)"]
        try:
            for level in range(1, levels + 1):
                # Each level is its own slab: the previous floor is this pass's "top", so
                # the entry ramp descends one step through air it has already cut.
                self.material_top = saved_top - (level - 1) * step
                self.cut_depth = saved_top - level * step
                gcode.append(f"(Depth level {level}/{levels} - cutting to "
                             f"Z{self.cut_depth:.4f})")
                gcode.extend(emit())
        finally:
            self.material_top, self.cut_depth = saved_top, saved_bottom
        return gcode

    @staticmethod
    def passes_for_depth(total: float, limit: float) -> int:
        """How many passes of at most `limit` cover `total`, tolerant of float dust.

        Plain ceil() on a value built from the Z frame flips between the two Z datums:
        `material_top - cut_depth` is exact arithmetic on one and a rounded subtraction
        on the other, so a depth sitting exactly on a multiple of the limit can come out
        one ULP over and buy a whole extra pass. The tolerance is far below the four
        decimals G-code carries, so a genuine overshoot still rounds up."""
        if total <= 0 or limit <= 0:
            return 1
        return max(1, int(math.ceil(total / limit - 1e-9)))

    @staticmethod
    def drill_point_length(diameter: float, point_angle: float = DEFAULT_DRILL_POINT_ANGLE) -> float:
        """Axial length of a twist drill's conical point.

        The tip reaches full depth before the drill's flutes do, so a through hole has to
        be driven this much deeper than the stock or the bottom of the hole is a cone
        rather than a full-diameter opening. For a 118 degree point that is about 0.3 x
        diameter - on a 1/4 inch drill, 0.075 inch, which is ten times the 0.008 inch
        spoilboard overcut a milled hole gets away with.
        """
        half_angle = math.radians(max(1.0, min(179.0, point_angle)) / 2.0)
        return (diameter / 2.0) / math.tan(half_angle)

    def _emit_peck_cycle(self, gcode: List[str], cx: float, cy: float,
                         final_depth: float, retract_plane: float,
                         peck_depth: float, feed: float) -> None:
        """Peck down to `final_depth` as explicit moves, not a G83 canned cycle.

        This is what a G83 does - cut a peck, retract to the R plane to clear chips, rapid
        back down, repeat - written out. Expanding it is worth the extra lines for four
        separate reasons:

        * **GRBL does not implement canned cycles.** G81-G89 are not in GRBL 1.1, so a
          G83 is `error:20 Unsupported command` and the program stops dead. ASSUMPTIONS.md
          lists GRBL as a target controller, and the Onshape-era code emitted G83 anyway.
        * The cycle-time estimator only understands G0/G1/G2/G3, so every G83 counted as
          zero seconds - a 12-hole plate under-reported by the entire drilling operation.
        * The 3D preview matches /^(G[0-3])/, so drilled holes did not appear at all.
        * The heightmap simulator in the test harness likewise never saw the material come
          out.

        Fixing those three consumers separately would leave the GRBL problem, and would
        have to be repeated for every future consumer of the G-code.
        """
        depth_remaining = retract_plane - final_depth
        if depth_remaining <= 0:
            return
        pecks = max(1, int(math.ceil(depth_remaining / max(peck_depth, 1e-6))))
        previous = retract_plane

        for peck in range(1, pecks + 1):
            target = max(final_depth, retract_plane - peck_depth * peck)
            if peck == pecks:
                target = final_depth
            if previous < retract_plane:
                # Coming back after a chip-clearing retract: rapid down to just above
                # where the last peck stopped, then cut from there.
                gcode.append(f"G0 Z{previous + self.peck_return_clearance:.4f}  "
                             f"; Rapid back to just above the last peck")
            gcode.append(f"G1 Z{target:.4f} F{feed:.1f}  ; Peck {peck} of {pecks}")
            if peck < pecks:
                gcode.append(f"G0 Z{retract_plane:.4f}  ; Retract to clear chips")
            previous = target

    def _generate_drill_gcode(self, cx: float, cy: float, diameter: float,
                              point_angle: float = None,
                              through: bool = True) -> List[str]:
        """One hole, drilled. Rapid over, peck to depth on the axis, retract. Nothing else.

        Depth accounts for the drill point when the hole goes through (see
        drill_point_length), so the exit side is full diameter. A blind hole is measured
        to the tip as the operator would expect from the drawing.
        """
        gcode = []
        point_angle = point_angle or self.DEFAULT_DRILL_POINT_ANGLE
        point = self.drill_point_length(diameter, point_angle)

        # Chip-clearing retract: just above the stock, not the full safe height - a G83
        # that retracts to safe Z between every peck spends the whole cycle in rapids.
        retract_plane = self.material_top + self.drill_retract_clearance
        if through:
            final_depth = self.cut_depth - point
            note = (f"(Through hole: {self.material_thickness:.4f} in stock plus "
                    f"{point:.4f} in for the {point_angle:.0f} deg point)")
        else:
            final_depth = self.cut_depth
            note = "(Blind hole: depth measured to the drill tip)"

        gcode.append(f"(Drill {diameter:.4f} in dia at X{cx:.3f} Y{cy:.3f})")
        gcode.append(note)
        gcode.append(f"G0 X{cx:.4f} Y{cy:.4f}  ; Rapid over hole centre")
        gcode.append(f"G0 Z{retract_plane:.4f}  ; Down to the retract plane")
        self._emit_peck_cycle(gcode, cx, cy, final_depth, retract_plane,
                              self.peck_drill_depth, self.plunge_rate)
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
        return gcode

    def spot_drill_depth(self, spot_depth: float = None) -> float:
        """How deep a centre/spot drill goes: enough to leave a cone the following drill
        can seat its point in, and no more. A quarter of the tool diameter is the usual
        rule; never deep enough to matter to the part."""
        return spot_depth if spot_depth else max(0.02, self.tool_diameter / 4.0)

    def generate_drill_operation_gcode(self, holes: List[Dict[str, Any]],
                                       point_angle: float = None,
                                       spot_only: bool = False,
                                       spot_depth: float = None) -> List[str]:
        """The whole drilling operation: every hole in `holes`, worked on its axis.

        `spot_only` turns this into a centre-drilling pass: a shallow dimple at each hole
        centre to locate a later drill, rather than the hole itself. It never goes through
        the stock, so it takes no drill-point allowance and leaves the part otherwise
        untouched.
        """
        if not holes:
            return []

        if spot_only:
            depth = self.spot_drill_depth(spot_depth)
            gcode = ["(===== CENTRE DRILLING =====)",
                     f"(Spotting tool {self.tool_diameter:.4f} in diameter)",
                     f"(Locating dimples {depth:.4f} in deep - these are NOT the holes)",
                     "(Axial plunge only - no lateral cutting moves)",
                     ""]
            for i, hole in enumerate(holes, 1):
                cx, cy = hole['center']
                drawn = hole.get('drawn_diameter', hole['diameter'])
                gcode.append(f"(Spot {i} of {len(holes)} - for a {drawn:.4f} in hole)")
                gcode.extend(self._generate_spot_gcode(cx, cy, depth))
                gcode.append("")
            return gcode

        gcode = ["(===== DRILLING =====)",
                 f"(Twist drill {self.tool_diameter:.4f} in diameter, "
                 f"{(point_angle or self.DEFAULT_DRILL_POINT_ANGLE):.0f} deg point)",
                 "(Axial plunge only - no lateral cutting moves)",
                 ""]
        through = self.is_through_cut()
        for i, hole in enumerate(holes, 1):
            cx, cy = hole['center']
            gcode.append(f"(Hole {i} of {len(holes)})")
            gcode.extend(self._generate_drill_gcode(cx, cy, hole['diameter'],
                                                    point_angle=point_angle, through=through))
            gcode.append("")
        return gcode

    def _generate_spot_gcode(self, cx: float, cy: float, depth: float) -> List[str]:
        """One centre-drill dimple. Shallow enough not to need pecking."""
        target = self.material_top - depth
        return [
            f"G0 X{cx:.4f} Y{cy:.4f}  ; Rapid over hole centre",
            f"G0 Z{self.material_top + self.spot_approach_clearance:.4f}  "
            f"; Down to just above the stock",
            f"G1 Z{target:.4f} F{self.plunge_rate:.1f}  ; Spot",
            f"G0 Z{self.retract_height:.4f}  ; Retract",
        ]

    def _generate_peck_drill_and_spiral_gcode(self, cx: float, cy: float, diameter: float, final_toolpath_radius: float) -> List[str]:
        """
        Generate G-code for small holes using G83 peck drilling + spiral clearing.

        For holes that are larger than the tool but too small to helical entry into,
        we peck drill straight down to full depth, then do a single spiral clearing
        pass at the bottom to open the hole to the final diameter.

        Args:
            cx, cy: Hole center coordinates
            diameter: Hole diameter (from CAD)
            final_toolpath_radius: Target toolpath radius for spiral clearing
        """
        gcode = []

        # Peck drilling parameters (from material settings)
        peck_depth = self.peck_drill_depth
        # Just above the stock for chip clearing, not a full retract.
        retract_plane = self.material_top + self.drill_retract_clearance
        final_depth = self.cut_depth  # Bottom of cut (negative value)

        # A hole at (essentially) the tool diameter has no material to clear beyond the
        # drilled bore: it's a pure straight peck drill. Skip the spiral + finishing arcs
        # entirely (a zero-radius arc I0 J0 is degenerate and errors on many controllers).
        pure_drill = final_toolpath_radius <= self.hole_size_tolerance

        if pure_drill and self.is_through_cut(final_depth):
            # A twist drill cuts a CONE, not a flat bottom. Stopping the tip at cut_depth
            # leaves the hole full diameter only where the point has fully emerged, so a
            # 0.201 in hole through a 1/16 in wall exited as a 0.027 in pinhole - nothing
            # a #10 screw could pass, which is the whole purpose of the pattern.
            # _generate_drill_gcode has always done this; this path had not, and this path
            # is the one the tube patterns use.
            # ...but only for a tool that HAS a point. An end mill cuts flat, so the
            # allowance would be 0.075 in of gratuitous depth into the spoilboard - and
            # tooling.py's ZMIN only accounts for it on a drill, so the header would
            # under-report the program's own deepest move.
            point = self.drill_point_length(diameter) if self.tool_has_drill_point else 0.0
            final_depth = final_depth - point
            gcode.append(f"(Peck drill straight down - hole is tool-sized, no lateral clearing)")
            if point:
                gcode.append(f"(Through hole: plus {point:.4f} in so the point clears "
                             f"and the exit is full diameter)")
            else:
                gcode.append("(Through hole: flat-bottomed cutter, no point allowance)")
        elif pure_drill:
            gcode.append(f"(Peck drill straight down - hole is tool-sized, no lateral clearing)")
            gcode.append(f"(Blind hole: depth measured to the drill tip)")
        else:
            gcode.append(f"(Peck drill at center, then spiral clear to {diameter:.3f}\" diameter)")

        # Rapid to hole center above material
        gcode.append(f"G0 X{cx:.4f} Y{cy:.4f}  ; Rapid to hole center")
        gcode.append(f"G0 Z{retract_plane:.4f}  ; Move to retract plane")

        # Peck drilling, written out rather than as a G83 canned cycle - see
        # _emit_peck_cycle for why (GRBL support, cycle time, preview, simulation).
        self._emit_peck_cycle(gcode, cx, cy, final_depth, retract_plane,
                              peck_depth, self.plunge_rate)

        if pure_drill:
            gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
            return gcode

        # Now at bottom of hole, do spiral clearing pass to open to final diameter
        # Start from center and spiral outward
        stepover = self.tool_diameter * self.stepover_percentage

        # We're already at center at full depth, so start spiraling immediately
        gcode.append(f"(Spiral clear from center to r={final_toolpath_radius:.4f}\")")

        # Calculate spiral parameters for clearing from center to final radius
        spiral_constant = stepover / (2 * math.pi)
        total_angle = final_toolpath_radius / spiral_constant if spiral_constant > 0 else 0

        # Generate spiral points from center outward
        angle_increment = math.radians(10)  # 10 degrees per segment
        num_points = int(math.ceil(total_angle / angle_increment))

        # Spiral outward from center (r=0) to final radius
        for i in range(1, num_points + 1):  # Start at i=1 to avoid staying at center
            current_angle = i * angle_increment
            current_radius = spiral_constant * current_angle
            current_radius = min(current_radius, final_toolpath_radius)  # Don't exceed target

            # Convert polar to Cartesian
            x = cx + current_radius * math.cos(current_angle)
            y = cy + current_radius * math.sin(current_angle)

            gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Final cleanup pass at exact final radius (full circle)
        final_x = cx + final_toolpath_radius
        final_y = cy
        gcode.append(f"G1 X{final_x:.4f} Y{final_y:.4f} F{self.feed_rate}  ; Move to final radius")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Final cleanup circle CCW for climb milling")

        # Spring pass: repeat the final circle at zero stepover to relieve tool
        # deflection that left the hole slightly undersized.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Spring pass at final radius")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _generate_hole_gcode(self, cx: float, cy: float, diameter: float, needs_peck_drill: bool = False) -> List[str]:
        """
        Generate G-code for a hole using helical entry + spiral-out strategy,
        or peck drilling + spiral for small holes.

        Args:
            cx, cy: Hole center coordinates
            diameter: Hole diameter (from CAD)
            needs_peck_drill: If True, use G83 peck drilling instead of helical entry
        """
        gcode = []

        # Calculate target toolpath radius (hole radius minus tool radius for inside cut)
        hole_radius = diameter / 2
        final_toolpath_radius = hole_radius - self.tool_radius

        # If hole is too small for helical entry, use peck drilling to get down. A hole at
        # the tool size has a zero (or float-noise-negative) toolpath radius: clamp it to 0
        # so it becomes a pure straight peck drill (the peck helper skips lateral clearing).
        if needs_peck_drill:
            return self._generate_peck_drill_and_spiral_gcode(cx, cy, diameter, max(final_toolpath_radius, 0.0))

        if final_toolpath_radius <= 0:
            gcode.append(f"(WARNING: Tool diameter {self.tool_diameter:.4f}\" is too large for {diameter:.4f}\" hole!)")
            return gcode

        # Strategy: Helical entry at small radius, then spiral outward
        # Each pass increases the radius by stepover percentage (material-specific)
        stepover = self.tool_diameter * self.stepover_percentage
        num_radial_passes = max(1, int(math.ceil(final_toolpath_radius / stepover)))

        # Calculate ramp start height (close to material surface)
        ramp_start_height = self.material_top + self.ramp_start_clearance

        # Calculate helical entry passes
        entry_radius = min(stepover, final_toolpath_radius)  # Use first stepover radius
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(entry_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Hole {diameter:.3f}\" dia: helical entry at {entry_radius:.4f}\" radius, then {num_radial_passes} radial passes)")

        # Position at edge of entry radius
        start_x = cx + entry_radius
        start_y = cy
        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.traverse_rate}  ; Position at entry radius")
        gcode.extend(self._approach_ramp_start(ramp_start_height))

        # Helical entry in multiple passes using ramp feed rate
        gcode.append(f"(Helical entry: {num_helical_passes} passes at {self.ramp_angle} deg, {depth_per_pass:.4f}\" per pass)")
        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-entry_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes} CCW for climb milling")

        # Clean up pass at entry radius and final depth
        gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-entry_radius:.4f} J0 F{self.feed_rate}  ; Clean up pass at entry radius CCW for climb milling")

        # True Archimedean spiral outward from entry radius to final radius
        # Spiral equation: r = r_start + b*θ where b = stepover/(2π)
        radius_delta = final_toolpath_radius - entry_radius

        if radius_delta > 0.001:
            # Calculate spiral parameters
            spiral_constant = stepover / (2 * math.pi)
            total_angle = radius_delta / spiral_constant if spiral_constant > 0 else 0

            # Generate spiral points
            angle_increment = math.radians(10)  # 10 degrees per segment
            num_points = int(math.ceil(total_angle / angle_increment))

            gcode.append(f"(Archimedean spiral: {num_points} points from r={entry_radius:.4f}\" to r={final_toolpath_radius:.4f}\")")

            # Cut continuous spiral from entry_radius to final_toolpath_radius
            # Use positive angle for counter-clockwise spiral (climb milling on inside feature)
            for i in range(num_points):
                current_angle = i * angle_increment  # Positive for counter-clockwise
                current_radius = entry_radius + spiral_constant * current_angle

                # Convert polar coordinates to Cartesian
                x = cx + current_radius * math.cos(current_angle)
                y = cy + current_radius * math.sin(current_angle)

                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Final cleanup pass at exact final radius
        final_x = cx + final_toolpath_radius
        final_y = cy
        gcode.append(f"(Final cleanup pass at exact radius)")
        gcode.append(f"G1 X{final_x:.4f} Y{final_y:.4f} F{self.feed_rate}  ; Move to final radius")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Cut final circle CCW for climb milling")

        # Spring pass: repeat the final circle at zero stepover to relieve tool
        # deflection that left the hole slightly undersized.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Spring pass at final radius")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _estimate_cycle_time(self, gcode_lines: List[str]) -> dict:
        """
        Estimate total cycle time from G-code.
        Returns dict with breakdown of time components.
        """
        cutting_time = 0.0  # G1/G2/G3 moves
        rapid_time = 0.0    # G0 moves
        dwell_time = 0.0    # G4 pauses

        # Assume typical rapid speed (machine dependent)
        rapid_speed = 400.0  # IPM - conservative estimate

        current_x = 0.0
        current_y = 0.0
        current_z = 0.0
        current_feed = self.feed_rate

        for line in gcode_lines:
            # Remove comments
            line = re.sub(r'\(.*?\)', '', line).strip()
            line = re.sub(r';.*$', '', line).strip()

            if not line:
                continue

            # Parse G-code command
            if line.startswith('G0'):
                # Rapid move
                x, y, z = current_x, current_y, current_z
                if 'X' in line:
                    x = float(re.search(r'X([-\d.]+)', line).group(1))
                if 'Y' in line:
                    y = float(re.search(r'Y([-\d.]+)', line).group(1))
                if 'Z' in line:
                    z = float(re.search(r'Z([-\d.]+)', line).group(1))

                distance = math.sqrt((x - current_x)**2 + (y - current_y)**2 + (z - current_z)**2)
                rapid_time += distance / rapid_speed * 60  # Convert to seconds

                current_x, current_y, current_z = x, y, z

            elif line.startswith('G1'):
                # Linear cutting move
                x, y, z = current_x, current_y, current_z
                feed = current_feed

                if 'X' in line:
                    x = float(re.search(r'X([-\d.]+)', line).group(1))
                if 'Y' in line:
                    y = float(re.search(r'Y([-\d.]+)', line).group(1))
                if 'Z' in line:
                    z = float(re.search(r'Z([-\d.]+)', line).group(1))
                if 'F' in line:
                    feed = float(re.search(r'F([-\d.]+)', line).group(1))
                    current_feed = feed

                distance = math.sqrt((x - current_x)**2 + (y - current_y)**2 + (z - current_z)**2)
                cutting_time += distance / feed * 60  # Convert to seconds

                current_x, current_y, current_z = x, y, z

            elif line.startswith('G2') or line.startswith('G3'):
                # Arc move
                x, y, z = current_x, current_y, current_z
                feed = current_feed

                if 'X' in line:
                    x = float(re.search(r'X([-\d.]+)', line).group(1))
                if 'Y' in line:
                    y = float(re.search(r'Y([-\d.]+)', line).group(1))
                if 'Z' in line:
                    z = float(re.search(r'Z([-\d.]+)', line).group(1))
                if 'F' in line:
                    feed = float(re.search(r'F([-\d.]+)', line).group(1))
                    current_feed = feed

                # Get arc center offsets
                i = 0.0
                j = 0.0
                if 'I' in line:
                    i = float(re.search(r'I([-\d.]+)', line).group(1))
                if 'J' in line:
                    j = float(re.search(r'J([-\d.]+)', line).group(1))

                # Calculate arc length (approximate)
                center_x = current_x + i
                center_y = current_y + j
                radius = math.sqrt(i**2 + j**2)

                # Calculate angle swept
                start_angle = math.atan2(current_y - center_y, current_x - center_x)
                end_angle = math.atan2(y - center_y, x - center_x)
                angle = end_angle - start_angle

                # Handle full circles and direction (G2=CW, G3=CCW)
                if abs(angle) < 0.001:  # Full circle
                    angle = 2 * math.pi
                elif line.startswith('G2') and angle > 0:
                    angle -= 2 * math.pi
                elif line.startswith('G3') and angle < 0:
                    angle += 2 * math.pi

                arc_length = abs(angle * radius)

                # Add Z component if helical
                z_distance = abs(z - current_z)
                total_distance = math.sqrt(arc_length**2 + z_distance**2)

                cutting_time += total_distance / feed * 60  # Convert to seconds

                current_x, current_y, current_z = x, y, z

            elif line.startswith('G4'):
                # Dwell
                if 'P' in line:
                    dwell_seconds = float(re.search(r'P([-\d.]+)', line).group(1))
                    dwell_time += dwell_seconds

        total_time = cutting_time + rapid_time + dwell_time

        return {
            'total': total_time,
            'cutting': cutting_time,
            'rapid': rapid_time,
            'dwell': dwell_time
        }

    def _format_time(self, seconds: float) -> str:
        """Format seconds as human-readable time string"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"

    def _is_pocket_circular(self, pocket_points: List[Tuple[float, float]], tolerance: float = 0.1) -> bool:
        """
        Detect if a pocket is circular by checking if all vertices are equidistant from centroid.

        Args:
            pocket_points: List of (x, y) coordinates
            tolerance: Relative tolerance (0.1 = 10% variation allowed)

        Returns:
            True if pocket is circular, False otherwise
        """
        pocket_poly = Polygon(pocket_points)
        cx = pocket_poly.centroid.x
        cy = pocket_poly.centroid.y

        # Calculate distances from centroid to all vertices
        distances = []
        for x, y in pocket_points:
            dist = self._distance_2d((x, y), (cx, cy))
            distances.append(dist)

        if not distances:
            return False

        # Check if all distances are within tolerance of the average
        avg_dist = sum(distances) / len(distances)
        max_deviation = max(abs(d - avg_dist) for d in distances)
        relative_deviation = max_deviation / avg_dist if avg_dist > 0 else 0

        return relative_deviation < tolerance

    def _contour_threshold_area(self) -> float:
        """Area above which a through-cut hole/pocket is contour-cleared rather than
        pocket-cleared. From config `machining.pockets.contour_threshold` (default 510; set 0
        to disable contouring -> infinite threshold), scaled by the tool footprint and
        stepover: contour_threshold * tool_diameter^2 * stepover_percentage."""
        contour_threshold = self.config._get('machining', 'pockets', 'contour_threshold', default=510)
        if contour_threshold <= 0:
            return float('inf')
        return contour_threshold * self.tool_diameter ** 2 * self.stepover_percentage

    def _insert_cycle_time_comment(self, gcode: List[str], time_estimate: dict, offset: int = 3) -> None:
        """Insert the estimated-cycle-time comment block into `gcode`, just after the header's
        (Operations: ...) line. `offset` is where the block lands relative to that line: the
        standard header has two more lines after Operations (offset 3); the multi-part job
        header does not (offset 1). Mutates gcode in place; no-op if there is no Operations line."""
        for i, line in enumerate(gcode):
            if line.startswith("(Operations:"):
                time_lines = [
                    "",
                    f"(Estimated cycle time: {self._format_time(time_estimate['total'])})",
                    f"(  Cutting: {self._format_time(time_estimate['cutting'])}, Rapids: {self._format_time(time_estimate['rapid'])}, Spindle: {self._format_time(time_estimate['dwell'])})",
                    "(  Note: Estimate does not include acceleration/deceleration)"
                ]
                for j, time_line in enumerate(time_lines):
                    gcode.insert(i + offset + j, time_line)
                break

    @staticmethod
    def _tessellate_circle(cx: float, cy: float, radius: float, segments: int = None,
                           chord_tol: float = 0.001, min_segments: int = 50,
                           max_segments: int = 400) -> List[Tuple[float, float]]:
        """Return points evenly spaced around a circle (open loop - no repeated closing
        point), used to turn a CAD circle into a polyline for contouring/containment.

        By default the segment count is chosen adaptively so the chord deviation from the
        true circle stays within `chord_tol` inches - a large circle (big bore or round
        plate perimeter) gets more segments instead of faceting, while the count is floored
        at `min_segments` (the long-standing 50) so small circles keep their exact prior
        density and output. Pass an explicit `segments` to force a fixed count."""
        if segments is None:
            if radius > 0 and chord_tol > 0:
                ratio = 1.0 - chord_tol / radius
                if ratio <= -1.0:      # tolerance dwarfs a sub-tol radius
                    n = min_segments
                else:
                    dtheta = 2.0 * math.acos(ratio)   # sagitta = r(1-cos(dtheta/2)) = chord_tol
                    n = math.ceil(2 * math.pi / dtheta)
                segments = max(min_segments, min(max_segments, n))
            else:
                segments = min_segments
        return [(cx + radius * math.cos((j / segments) * 2 * math.pi),
                 cy + radius * math.sin((j / segments) * 2 * math.pi))
                for j in range(segments)]

    @staticmethod
    def _reorder_closed_ring(points: List[Tuple[float, float]], ref: Tuple[float, float]) -> List[Tuple[float, float]]:
        """Rotate a closed ring's vertex list so it begins at the vertex nearest `ref`, keeping
        winding order. Makes the link from the previous ring a short one-stepover hop instead of
        a long diagonal (shapely's orient() does not fix the start vertex)."""
        if len(points) < 2:
            return list(points)
        rx, ry = ref
        best = min(range(len(points)), key=lambda i: (points[i][0] - rx) ** 2 + (points[i][1] - ry) ** 2)
        return list(points[best:]) + list(points[:best])

    def _emit_ring_ramp(self, gcode: List[str], ring: List[Tuple[float, float]],
                        ramp_start_height: float, final_cut_z: float) -> None:
        """Descend from ramp_start_height to final_cut_z by walking the closed `ring` (looping
        as many full laps as the ramp angle needs), ending back at ring[0] at depth. The tool is
        assumed positioned above ring[0] at ramp_start_height. Ramping ALONG the ring keeps the
        plunge inside the pocket - never a straight full-depth plunge into keep-material."""
        drop = ramp_start_height - final_cut_z
        if drop <= 1e-9:
            return
        loop = list(ring) + [ring[0]]
        perim = sum(math.dist(loop[i], loop[i + 1]) for i in range(len(loop) - 1)) or 1e-9
        ramp_len = drop / math.tan(math.radians(self.ramp_angle)) if self.ramp_angle > 0 else drop
        laps = min(max(1, int(math.ceil(ramp_len / perim))), 100)  # cap laps for tiny rings
        total_len = laps * perim
        traveled = 0.0
        prev = ring[0]
        for _ in range(laps):
            for pt in loop[1:]:               # one full lap ends back at ring[0]
                traveled += math.dist(prev, pt)
                z = ramp_start_height - drop * min(1.0, traveled / total_len)
                gcode.append(f"G1 X{pt[0]:.4f} Y{pt[1]:.4f} Z{z:.4f} F{self.ramp_feed_rate}  ; Ramp in along ring")
                prev = pt

    def _corner_feed_scale(self, prev: Tuple[float, float], v: Tuple[float, float],
                           nxt: Tuple[float, float]) -> float:
        """Feed multiplier for the corner at vertex v, from the included angle between its two
        edges (v->prev and v->nxt). Straight-through (~180 deg) -> 1.0 (no slowdown); sharper
        corners scale down toward corner_min_feed_scale, easing the feed through the high
        engagement where the cutter wraps two edges at once."""
        ax, ay = prev[0] - v[0], prev[1] - v[1]
        bx, by = nxt[0] - v[0], nxt[1] - v[1]
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        if la < 1e-9 or lb < 1e-9:
            return 1.0
        cos_a = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
        included = math.degrees(math.acos(cos_a))   # 0 (spike back on itself) .. 180 (straight)
        GENTLE, SHARP = 150.0, 60.0                  # >= GENTLE: full feed; <= SHARP: full slowdown
        if included >= GENTLE:
            return 1.0
        if included <= SHARP:
            return self.corner_min_feed_scale
        t = (included - SHARP) / (GENTLE - SHARP)
        return self.corner_min_feed_scale + t * (1.0 - self.corner_min_feed_scale)

    def _emit_ring_cut_with_corner_slowdown(self, gcode: List[str],
                                            ring: List[Tuple[float, float]], base_feed: float) -> None:
        """Trace a CLOSED ring (ring[0] -> ... -> ring[0]) as G1 cutting moves at base_feed,
        easing the feed down within corner_slowdown_zone of each sharp corner (see
        _corner_feed_scale) to tame the engagement/force spike there. Only collinear waypoints
        are inserted, so the toolpath geometry is unchanged. Assumes the tool is at ring[0]."""
        n = len(ring)
        if n < 2:
            return
        scale = [self._corner_feed_scale(ring[(i - 1) % n], ring[i], ring[(i + 1) % n]) for i in range(n)]
        for i in range(n):
            a, b = ring[i], ring[(i + 1) % n]
            sa, sb = scale[i], scale[(i + 1) % n]
            seg = math.hypot(b[0] - a[0], b[1] - a[1])
            slow_a, slow_b = sa < 0.999, sb < 0.999
            if seg < 1e-9 or (not slow_a and not slow_b):
                gcode.append(f"G1 X{b[0]:.4f} Y{b[1]:.4f} F{base_feed:.1f}")
                continue
            zone = min(self.corner_slowdown_zone, seg / 2.0)
            ux, uy = (b[0] - a[0]) / seg, (b[1] - a[1]) / seg
            cur = a
            if slow_a:  # ease OUT of the corner at a
                p1 = (a[0] + ux * zone, a[1] + uy * zone)
                gcode.append(f"G1 X{p1[0]:.4f} Y{p1[1]:.4f} F{base_feed * sa:.1f}  ; corner slowdown")
                cur = p1
            if slow_b:  # full feed across the middle, then ease INTO the corner at b
                p2 = (b[0] - ux * zone, b[1] - uy * zone)
                if math.hypot(p2[0] - cur[0], p2[1] - cur[1]) > 1e-6:
                    gcode.append(f"G1 X{p2[0]:.4f} Y{p2[1]:.4f} F{base_feed:.1f}")
                gcode.append(f"G1 X{b[0]:.4f} Y{b[1]:.4f} F{base_feed * sb:.1f}  ; corner slowdown")
            else:
                gcode.append(f"G1 X{b[0]:.4f} Y{b[1]:.4f} F{base_feed:.1f}")

    def _link_and_cut_ring(self, gcode: List[str], ring_points: List[Tuple[float, float]],
                           cur_pos: Tuple[float, float], safe_region, ramp_start_height: float,
                           final_cut_z: float, link_tol: float) -> Tuple[float, float]:
        """Move from cur_pos onto a closed contour ring, cut it, and return the end position
        (its start vertex). The ring is reordered to start nearest cur_pos (fix 2). If the
        straight link would leave the pocket - a concave notch, or a disconnected buffer region -
        retract and ramp back down along the ring instead of gouging straight across keep-material
        (fix 1)."""
        ring = self._reorder_closed_ring(ring_points, cur_pos)
        start = ring[0]
        if safe_region.buffer(link_tol).contains(LineString([cur_pos, start])):
            gcode.append(f"G1 X{start[0]:.4f} Y{start[1]:.4f} F{self.feed_rate}")
        else:
            gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract - straight link would cross keep-material")
            gcode.append(f"G0 X{start[0]:.4f} Y{start[1]:.4f}  ; Rapid over the notch to the ring")
            gcode.append(f"G0 Z{ramp_start_height:.4f}  ; Down to ramp-start height")
            self._emit_ring_ramp(gcode, ring, ramp_start_height, final_cut_z)  # ends at `start` at depth
        # Clean lap at depth, easing the feed through sharp (high-engagement) corners.
        self._emit_ring_cut_with_corner_slowdown(gcode, ring, self.feed_rate)
        return start

    def _generate_pocket_gcode(self, pocket_points: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for a pocket with tool compensation (offset inward) and helical entry.
        Uses spiral clearing for circular pockets, contour-parallel for non-circular."""
        gcode = []

        # Create offset path (inward by tool radius)
        pocket_poly = Polygon(pocket_points)

        # Buffer inward (negative buffer)
        offset_poly = pocket_poly.buffer(-self.tool_radius)

        if offset_poly.is_empty or offset_poly.area < 0.001:
            center_x, center_y = self._get_polygon_center(pocket_poly)
            error_msg = f"Pocket at approximately ({center_x:.3f}, {center_y:.3f}) is too small for {self.tool_diameter:.4f}\" tool - tool cannot fit inside with proper clearance"
            self._add_error(error_msg)
            return gcode

        # Get the boundary of the offset polygon. GEOS buffer() does NOT reliably orient
        # its output (it returns CW exteriors here), so normalize to canonical orientation
        # (exterior CCW) explicitly: cutting an interior pocket CCW is climb milling, matching
        # the CCW helical entry and hole toolpaths.
        if hasattr(offset_poly, 'exterior'):
            offset_poly = orient(offset_poly, 1.0)
            offset_points = list(offset_poly.exterior.coords)[:-1]  # Remove duplicate last point
        else:
            center_x, center_y = self._get_polygon_center(pocket_poly)
            error_msg = f"Pocket at approximately ({center_x:.3f}, {center_y:.3f}) resulted in invalid geometry after tool compensation"
            self._add_error(error_msg)
            return gcode

        # Entry (plunge) point. The offset polygon's centroid is a nicely-centered plunge
        # point for convex pockets, but for a CONCAVE pocket (e.g. an L or U shape) the
        # centroid can fall OUTSIDE the polygon - which would helical-bore into keep-material
        # and then slot laterally across to reach the pocket. Fall back to
        # representative_point() (guaranteed inside the polygon) in that case.
        entry_point = offset_poly.centroid
        if not offset_poly.contains(entry_point):
            entry_point = offset_poly.representative_point()
        entry_x = entry_point.x
        entry_y = entry_point.y

        # Calculate helical entry parameters
        helix_radius = self.tool_radius * self.helix_radius_multiplier  # Helix radius from material preset
        ramp_start_height = self.material_top + self.ramp_start_clearance
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(helix_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Pocket with helical entry at center: {num_helical_passes} passes at {self.ramp_angle} deg)")

        # Position at pocket center
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.traverse_rate}  ; Position at pocket center")
        gcode.extend(self._approach_ramp_start(ramp_start_height))

        # Helical entry at center
        start_x = entry_x + helix_radius
        start_y = entry_y
        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.traverse_rate}  ; Move to helix start (above material)")

        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes} CCW for climb milling")

        # Return to center after helix
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.feed_rate}  ; Return to pocket center")

        # Calculate stepover for pocket clearing
        stepover = self.tool_diameter * self.stepover_percentage

        # A genuinely round pocket clears best with an Archimedean spiral (continuous
        # engagement, no ring-closure reversals or radial link cuts). Only take this path
        # when the pocket really is circular; everything else falls through to the
        # general contour-parallel strategy, which handles arbitrary (e.g. slot-shaped)
        # pockets that a circular spiral would leave uncleared in the corners.
        circle = self._detect_solid_circle(pocket_poly)
        if circle is not None:
            final_radius = math.sqrt(offset_poly.area / math.pi)  # tool-center travel radius
            if final_radius > 0.001:
                gcode.extend(self._generate_circular_pocket_spiral(entry_x, entry_y, final_radius))
                gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
                return gcode

        # Always use contour-parallel clearing for reliable material removal
        # (a circular spiral would leave material in slot-shaped pockets)
        gcode.append(f"(Pocket clearing - using contour-parallel stepover passes)")

        # Depth the helix reached; every contour/perimeter pass cuts at this Z, and a ramped
        # re-entry (in _link_and_cut_ring) must ramp back down to it.
        final_cut_z = ramp_start_height - num_helical_passes * depth_per_pass

        # Generate inward offsets from perimeter to center
        contours = []
        test_offset = -self.tool_radius   # Start from the tool-compensated perimeter
        while True:
            test_offset -= stepover
            test_poly = pocket_poly.buffer(test_offset)
            if test_poly.is_empty or test_poly.area < 0.001:
                break
            contours.append(test_poly)   # may be a MultiPolygon for complex (e.g. concave) shapes

        gcode.append(f"(Contour-parallel clearing: {len(contours)} offset passes)")

        # Cut contours center-outward, then the exact perimeter. Every ring is linked with a
        # GUARDED move: a straight feed when the link stays inside the pocket (offset_poly),
        # otherwise a retract + ramped re-entry along the ring - so a concave (L/U) pocket never
        # slots straight across the notch through keep-material. Rings are also reordered to
        # start nearest the tool, keeping links to a short one-stepover hop. The tool sits at the
        # pocket entry (at depth) after the helix.
        link_tol = 1e-4
        cur_pos = (entry_x, entry_y)
        pass_number = 0
        for contour_geom in reversed(contours):
            polygons_to_cut = []
            if hasattr(contour_geom, 'exterior'):
                polygons_to_cut.append(contour_geom)
            elif hasattr(contour_geom, 'geoms'):
                polygons_to_cut.extend(contour_geom.geoms)   # disconnected regions of a concave pocket
            for poly in polygons_to_cut:
                if not hasattr(poly, 'exterior'):
                    continue
                poly = orient(poly, 1.0)   # exterior CCW = climb milling for interior pockets
                contour_points = list(poly.exterior.coords)[:-1]
                if len(contour_points) < 3:
                    continue
                pass_number += 1
                gcode.append(f"(Contour pass {pass_number})")
                cur_pos = self._link_and_cut_ring(gcode, contour_points, cur_pos, offset_poly,
                                                  ramp_start_height, final_cut_z, link_tol)

        # Final pass - cut the exact (tool-compensated) perimeter.
        gcode.append(f"(Final pass: cut exact perimeter)")
        cur_pos = self._link_and_cut_ring(gcode, offset_points, cur_pos, offset_poly,
                                          ramp_start_height, final_cut_z, link_tol)

        # Spring pass: re-trace the perimeter at zero stepover to relieve tool deflection that
        # left the pocket slightly undersized. The tool is already on the perimeter.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        spring = self._reorder_closed_ring(offset_points, cur_pos)
        for point in spring[1:]:
            gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
        gcode.append(f"G1 X{spring[0][0]:.4f} Y{spring[0][1]:.4f} F{self.feed_rate}  ; Close spring pass")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _detect_solid_circle(self, polygon: Polygon) -> Optional[Tuple[float, float, float]]:
        """Return (cx, cy, radius) if the polygon is a solid circle, else None.

        A solid circle has no interior holes and a circular exterior. Uses the same
        isoperimetric quotient (4*pi*area/perimeter^2) and 0.95 threshold as
        _detect_circular_ring, so the spiral clearing path is taken only when the
        pocket is genuinely round.
        """
        if not isinstance(polygon, Polygon) or len(polygon.interiors) != 0:
            return None

        area = polygon.area
        perimeter = polygon.exterior.length
        if perimeter <= 0 or area <= 0:
            return None

        circularity = (4 * math.pi * area) / (perimeter ** 2)
        if circularity < 0.95:
            return None

        centroid = polygon.centroid
        radius = math.sqrt(area / math.pi)
        return (centroid.x, centroid.y, radius)

    def _detect_circular_ring(self, polygon: Polygon) -> Optional[Tuple[float, float, float, float]]:
        """Check if a polygon with interiors is approximately a circular ring.

        Uses the isoperimetric quotient (4*pi*area/perimeter^2) to test circularity
        of both the exterior and each interior boundary, and verifies they share
        a common center.

        Args:
            polygon: Shapely Polygon, potentially with interior holes

        Returns:
            (center_x, center_y, outer_radius, inner_radius) if circular ring,
            or None if not a circular ring
        """
        if len(polygon.interiors) != 1:
            return None

        circularity_threshold = 0.95
        center_tolerance = 0.01  # inches

        # Check exterior circularity
        ext_area = Polygon(polygon.exterior).area
        ext_perimeter = polygon.exterior.length
        ext_circularity = (4 * math.pi * ext_area) / (ext_perimeter ** 2)
        if ext_circularity < circularity_threshold:
            return None

        # Check interior circularity
        interior = polygon.interiors[0]
        int_area = Polygon(interior.coords).area
        int_perimeter = interior.length
        int_circularity = (4 * math.pi * int_area) / (int_perimeter ** 2)
        if int_circularity < circularity_threshold:
            return None

        # Check shared center
        ext_centroid = Polygon(polygon.exterior).centroid
        int_centroid = Polygon(interior.coords).centroid
        if ext_centroid.distance(int_centroid) > center_tolerance:
            return None

        # Calculate radii from area (more accurate than perimeter for discretized circles)
        cx = ext_centroid.x
        cy = ext_centroid.y
        outer_radius = math.sqrt(ext_area / math.pi)
        inner_radius = math.sqrt(int_area / math.pi)

        return (cx, cy, outer_radius, inner_radius)

    def _generate_circular_ring_gcode(self, pocket_poly: Polygon, offset_poly: Polygon,
                                      cx: float, cy: float,
                                      outer_radius: float, inner_radius: float) -> List[str]:
        """Generate G-code for a circular ring/groove pocket using spiral clearing.

        Instead of contour-parallel passes (which cause full-width slotting on the
        first pass), this uses a ring-centered helical ramp at the entry radius,
        then Archimedean spirals outward and inward to clear the full ring width.
        Every pass only engages stepover-width of material.

        Args:
            pocket_poly: Original pocket polygon (before tool compensation)
            offset_poly: Tool-compensated polygon (buffered inward by tool_radius)
            cx, cy: Ring center coordinates
            outer_radius, inner_radius: Radii of the tool-compensated ring
        """
        gcode = []

        stepover = self.tool_diameter * self.stepover_percentage
        spiral_constant = stepover / (2 * math.pi)
        angle_increment = math.radians(10)

        # Entry point: use representative_point which lands in the solid ring
        rep_point = offset_poly.representative_point()
        entry_radius = math.sqrt((rep_point.x - cx) ** 2 + (rep_point.y - cy) ** 2)
        entry_radius = max(inner_radius, min(outer_radius, entry_radius))

        # Place the entry on the +X axis (angle 0) for a clean helical ramp.
        entry_x = cx + entry_radius
        entry_y = cy

        ramp_start_height = self.material_top + self.ramp_start_clearance
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(
            entry_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Circular ring spiral clearing: center {cx:.4f}, {cy:.4f}, "
                     f"outer r={outer_radius:.4f}, inner r={inner_radius:.4f})")

        # Position at entry point on ring circumference
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.traverse_rate}  ; Position at ring entry")
        gcode.extend(self._approach_ramp_start(ramp_start_height))

        # Ring-centered helical ramp: helix around ring center at entry_radius.
        i_offset = cx - entry_x  # = -entry_radius (since entry_x = cx + entry_radius)
        j_offset = cy - entry_y  # = 0 (since entry_y = cy)
        gcode.append(f"(Helical ramp: {num_helical_passes} passes at entry radius {entry_radius:.4f})")
        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{entry_x:.4f} Y{entry_y:.4f} I{i_offset:.4f} J{j_offset:.4f} "
                         f"Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes}")
        gcode.append(f"G3 X{entry_x:.4f} Y{entry_y:.4f} I{i_offset:.4f} J{j_offset:.4f} "
                     f"F{self.feed_rate}  ; Cleanup pass at entry radius")

        # Archimedean spiral helper. Spirals from (start_radius, start_angle) to
        # end_radius, emitting a G1 point every angle_increment, and returns the
        # tool's final (angle, radius). CRITICAL: each phase continues from where
        # the previous one ended - we never emit a straight move between two
        # different angles, because that cuts a CHORD across the ring interior and
        # gouges the central island (the class of bug this rewrite fixes).
        def spiral_to(start_radius, start_angle, end_radius):
            angle, radius = start_angle, start_radius
            if abs(end_radius - start_radius) > 0.001 and spiral_constant > 0:
                direction = 1.0 if end_radius > start_radius else -1.0
                total_angle = abs(end_radius - start_radius) / spiral_constant
                num_points = int(math.ceil(total_angle / angle_increment))
                for k in range(1, num_points + 1):
                    angle = start_angle + k * angle_increment
                    radius = start_radius + direction * spiral_constant * (k * angle_increment)
                    radius = min(radius, end_radius) if direction > 0 else max(radius, end_radius)
                    x = cx + radius * math.cos(angle)
                    y = cy + radius * math.sin(angle)
                    gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")
            return angle, radius

        def point_on(radius, angle):
            return cx + radius * math.cos(angle), cy + radius * math.sin(angle)

        # Spiral outward to the outer wall, then a full cleanup circle FROM THE
        # CURRENT position (a G2/G3 full circle traces the whole ring from any start
        # point, so no reposition-to-angle-0 chord is needed).
        angle, radius = spiral_to(entry_radius, 0.0, outer_radius)
        if abs(radius - outer_radius) > 1e-4:
            ox, oy = point_on(outer_radius, angle)  # radial nudge, constant angle
            gcode.append(f"G1 X{ox:.4f} Y{oy:.4f} F{self.feed_rate}  ; Radial to outer radius")
        ox, oy = point_on(outer_radius, angle)
        gcode.append(f"G3 X{ox:.4f} Y{oy:.4f} I{cx - ox:.4f} J{cy - oy:.4f} "
                     f"F{self.feed_rate}  ; Outer cleanup circle, CCW climb milling")
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G3 X{ox:.4f} Y{oy:.4f} I{cx - ox:.4f} J{cy - oy:.4f} "
                     f"F{self.feed_rate}  ; Outer wall spring pass")

        # Radial move (constant angle) back in to entry radius, then spiral inward
        # to the inner wall and a full cleanup circle from the current position.
        ex, ey = point_on(entry_radius, angle)
        gcode.append(f"G1 X{ex:.4f} Y{ey:.4f} F{self.feed_rate}  ; Radial to entry radius")
        angle, radius = spiral_to(entry_radius, angle, inner_radius)
        if abs(radius - inner_radius) > 1e-4:
            ix, iy = point_on(inner_radius, angle)  # radial nudge, constant angle
            gcode.append(f"G1 X{ix:.4f} Y{iy:.4f} F{self.feed_rate}  ; Radial to inner radius")
        ix, iy = point_on(inner_radius, angle)
        gcode.append(f"G2 X{ix:.4f} Y{iy:.4f} I{cx - ix:.4f} J{cy - iy:.4f} "
                     f"F{self.feed_rate}  ; Inner cleanup circle, CW climb milling")
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G2 X{ix:.4f} Y{iy:.4f} I{cx - ix:.4f} J{cy - iy:.4f} "
                     f"F{self.feed_rate}  ; Inner wall spring pass")

        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _generate_circular_pocket_spiral(self, cx: float, cy: float,
                                         final_radius: float) -> List[str]:
        """Clear a solid circular pocket with an Archimedean spiral from the center out.

        Assumes the helical entry at the center has already reached full depth (the tool
        is at the pocket center). Spirals outward at one stepover per revolution to
        final_radius (the tool-center travel radius), then a finish circle plus spring
        pass at the wall. Continuous engagement, so no ring-closure reversals or radial
        link cuts. CCW throughout = climb milling for an interior pocket.
        """
        gcode = []
        stepover = self.tool_diameter * self.stepover_percentage

        gcode.append("(Circular pocket - Archimedean spiral clearing)")

        # Archimedean spiral r = spiral_constant * theta, growing one stepover per turn.
        spiral_constant = stepover / (2 * math.pi)
        if spiral_constant > 0:
            total_angle = final_radius / spiral_constant
            angle_increment = math.radians(10)
            num_points = int(math.ceil(total_angle / angle_increment))
            gcode.append(f"(Spiral outward: {num_points} points from center to r={final_radius:.4f})")
            for i in range(num_points + 1):
                current_angle = i * angle_increment
                current_radius = min(spiral_constant * current_angle, final_radius)
                x = cx + current_radius * math.cos(current_angle)
                y = cy + current_radius * math.sin(current_angle)
                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Finish circle at the wall (exact size), then a zero-stepover spring pass to
        # relieve tool deflection. G3/CCW = climb on the interior wall.
        wall_x = cx + final_radius
        wall_y = cy
        gcode.append(f"G1 X{wall_x:.4f} Y{wall_y:.4f} F{self.feed_rate}  ; Move to wall radius")
        gcode.append(f"G3 X{wall_x:.4f} Y{wall_y:.4f} I{-final_radius:.4f} J0 F{self.feed_rate}  ; Finish circle, CCW climb")
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G3 X{wall_x:.4f} Y{wall_y:.4f} I{-final_radius:.4f} J0 F{self.feed_rate}  ; Spring pass")

        return gcode

    def _generate_pocket_gcode_from_polygon(self, pocket_poly: Polygon) -> List[str]:
        """Generate G-code for a pocket from a Shapely Polygon (supports interior holes/islands).
        This is the island-aware version that respects Polygon interiors."""
        gcode = []

        # Validate input
        if not pocket_poly.is_valid or pocket_poly.is_empty:
            return gcode

        # Check groove width for polygons with interior holes (rings/grooves)
        if len(pocket_poly.interiors) > 0:
            min_groove_width = float('inf')
            for interior in pocket_poly.interiors:
                interior_ring = LinearRing(interior.coords)
                width = pocket_poly.exterior.distance(interior_ring)
                min_groove_width = min(min_groove_width, width)

            if min_groove_width < self.tool_diameter:
                center_x, center_y = pocket_poly.centroid.x, pocket_poly.centroid.y
                error_msg = (
                    f"Groove at approximately ({center_x:.3f}, {center_y:.3f}) "
                    f"is {min_groove_width:.4f}\" wide, which is too narrow for "
                    f"{self.tool_diameter:.4f}\" tool"
                )
                self._add_error(error_msg)
                return gcode

        # Buffer inward (negative buffer) for tool compensation
        # Key: Shapely automatically respects interior holes during buffer!
        offset_poly = pocket_poly.buffer(-self.tool_radius)

        if offset_poly.is_empty or offset_poly.area < 0.001:
            center_x, center_y = pocket_poly.centroid.x, pocket_poly.centroid.y
            error_msg = f"Pocket at approximately ({center_x:.3f}, {center_y:.3f}) is too small for {self.tool_diameter:.4f}\" tool - tool cannot fit inside with proper clearance"
            self._add_error(error_msg)
            return gcode

        # Check for circular ring - use spiral clearing instead of contour-parallel
        if isinstance(offset_poly, Polygon) and len(offset_poly.interiors) > 0:
            ring_params = self._detect_circular_ring(offset_poly)
            if ring_params is not None:
                cx, cy, outer_r, inner_r = ring_params
                return self._generate_circular_ring_gcode(
                    pocket_poly, offset_poly, cx, cy, outer_r, inner_r)

        # Find a good entry point within the machining area
        # CRITICAL: For rings/donuts, centroid is in the center hole (island)!
        # Use representative_point() which is guaranteed to be inside the solid geometry
        if hasattr(offset_poly, 'representative_point'):
            rep_point = offset_poly.representative_point()
            entry_x = rep_point.x
            entry_y = rep_point.y
        elif hasattr(offset_poly, 'geoms'):
            # MultiPolygon - use representative point of largest piece
            largest_poly = max(offset_poly.geoms, key=lambda p: p.area)
            rep_point = largest_poly.representative_point()
            entry_x = rep_point.x
            entry_y = rep_point.y
        else:
            # Fallback to centroid
            entry_x = offset_poly.centroid.x
            entry_y = offset_poly.centroid.y

        # Calculate helical entry parameters
        helix_radius = self.tool_radius * self.helix_radius_multiplier

        # Adapt helix radius to fit within available space
        # The max helix radius at the entry point is the distance from entry to nearest boundary
        max_helix_radius = offset_poly.boundary.distance(Point(entry_x, entry_y))
        if helix_radius > max_helix_radius * 0.9:  # 90% safety factor
            helix_radius = max(max_helix_radius * 0.9, self.tool_radius * 0.25)  # Floor at 25% of tool_radius

        ramp_start_height = self.material_top + self.ramp_start_clearance
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(helix_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Island-aware pocket with helical entry: {num_helical_passes} passes at {self.ramp_angle} deg)")

        # Position at entry point
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.traverse_rate}  ; Position at entry point")
        gcode.extend(self._approach_ramp_start(ramp_start_height))

        # Helical entry
        start_x = entry_x + helix_radius
        start_y = entry_y
        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.traverse_rate}  ; Move to helix start")

        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes}")

        # Return to entry point after helix
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.feed_rate}  ; Return to entry point")

        # Calculate stepover for pocket clearing
        stepover = self.tool_diameter * self.stepover_percentage

        gcode.append(f"(Contour-parallel clearing with island avoidance)")

        # For ring polygons (with interior holes), buffer() on the whole ring shrinks
        # from both sides simultaneously, collapsing the ring in ~2 steps.
        # Instead, offset from the EXTERIOR ONLY and stop at the interior boundary.
        solid_exterior = Polygon(pocket_poly.exterior)

        # Build expanded interior (holes + tool_radius) as the no-go zone
        expanded_interiors = None
        if len(pocket_poly.interiors) > 0:
            interior_geoms = []
            for interior in pocket_poly.interiors:
                interior_poly = Polygon(interior.coords)
                interior_geoms.append(interior_poly.buffer(self.tool_radius))
            expanded_interiors = unary_union(interior_geoms)

        # Generate offset contours from exterior inward
        contours = []
        test_offset = -self.tool_radius
        while True:
            test_offset -= stepover
            offset_circle = solid_exterior.buffer(test_offset)
            if offset_circle.is_empty or offset_circle.area < 0.001:
                break

            # Subtract expanded interior to stay in machining area
            if expanded_interiors is not None:
                machining_portion = offset_circle.difference(expanded_interiors)
                if machining_portion.is_empty or machining_portion.area < 0.001:
                    break
                contours.append(machining_portion)
            else:
                contours.append(offset_circle)

        gcode.append(f"(Contour-parallel clearing: {len(contours)} offset passes)")

        # Every link between rings, and onto the final boundary trace, goes through the
        # SAME guard the plain pocket path uses: a straight feed only when the link stays
        # inside the already-cleared region, otherwise retract, rapid over, ramp back
        # down. Without it the tool fed in a straight line at full depth from wherever it
        # was to the next ring's start - and with an island between them, straight
        # through it. Only circular islands were diverted (to the spiral clearer), so a
        # rectangular one was gouged.
        link_tol = 1e-4
        final_cut_z = ramp_start_height - num_helical_passes * depth_per_pass
        cur_pos = (entry_x, entry_y)

        # Cut contours from outside-in
        pass_number = 0
        for contour_geom in reversed(contours):
            # Handle both Polygon and MultiPolygon
            polygons_to_cut = []
            if isinstance(contour_geom, Polygon):
                polygons_to_cut.append(contour_geom)
            elif isinstance(contour_geom, MultiPolygon):
                polygons_to_cut.extend(contour_geom.geoms)

            for poly_to_cut in polygons_to_cut:
                if not hasattr(poly_to_cut, 'exterior'):
                    continue

                # Canonical orientation (exterior CCW) = climb milling for interior pockets.
                poly_to_cut = orient(poly_to_cut, 1.0)
                contour_coords = list(poly_to_cut.exterior.coords)[:-1]
                if len(contour_coords) < 3:
                    continue

                pass_number += 1
                gcode.append(f"(Contour pass {pass_number})")
                cur_pos = self._link_and_cut_ring(gcode, contour_coords, cur_pos,
                                                  offset_poly, ramp_start_height,
                                                  final_cut_z, link_tol)

        # Final pass - trace tool-compensated boundary (exterior + interiors). Canonical
        # orientation makes the exterior CCW (climb around the pocket wall) and interiors CW
        # (climb around any island), matching the CCW hole/helical toolpaths.
        if isinstance(offset_poly, Polygon):
            offset_poly = orient(offset_poly, 1.0)
        exterior_coords = (list(offset_poly.exterior.coords)[:-1]
                           if hasattr(offset_poly, 'exterior') else [])
        if len(exterior_coords) >= 3:
            pass_number += 1
            gcode.append(f"(Contour pass {pass_number} - final outer perimeter)")
            cur_pos = self._link_and_cut_ring(gcode, exterior_coords, cur_pos,
                                              offset_poly, ramp_start_height,
                                              final_cut_z, link_tol)

            # Spring pass: re-trace the exterior at zero stepover to relieve
            # tool deflection. The tool is already on the ring.
            gcode.append(f"(Spring pass - compensate for tool deflection)")
            spring = self._reorder_closed_ring(exterior_coords, cur_pos)
            for point in spring[1:]:
                gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
            gcode.append(f"G1 X{spring[0][0]:.4f} Y{spring[0][1]:.4f} F{self.feed_rate}")

        # Also trace interior boundaries of the tool-compensated ring
        if hasattr(offset_poly, 'interiors'):
            for interior in offset_poly.interiors:
                interior_coords = list(interior.coords)[:-1]
                if len(interior_coords) >= 3:
                    pass_number += 1
                    gcode.append(f"(Contour pass {pass_number} - inner boundary)")
                    cur_pos = self._link_and_cut_ring(gcode, interior_coords, cur_pos,
                                                      offset_poly, ramp_start_height,
                                                      final_cut_z, link_tol)

                    # Spring pass on this interior boundary.
                    gcode.append(f"(Spring pass - compensate for tool deflection)")
                    spring = self._reorder_closed_ring(interior_coords, cur_pos)
                    for point in spring[1:]:
                        gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
                    gcode.append(f"G1 X{spring[0][0]:.4f} Y{spring[0][1]:.4f} F{self.feed_rate}")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _emit_ramp_moves(self, gcode: List[str],
                         ramp_points: List[Tuple[float, float, float]],
                         start_point: Tuple[float, float],
                         ramp_start_height: float, ramp_depth: float,
                         ramp_distance: float, floor_z: float,
                         tab_zones: List[Tuple[float, float]],
                         tab_z: float) -> float:
        """Emit the ramp-in moves, holding tab height wherever the ramp would cut a tab.

        The ramp descends along the perimeter for as much as several inches - on 1/4"
        aluminum at 4 degrees, about 4.6" - so on a small part it covers ground that
        later carries a tab. Where the ramp has already dropped below the top of a
        standing tab it is milling that tab away, which is why the lift belongs here as
        well as in the ordinary cutting pass. Where the ramp is still above the tab top,
        the material is intact on its own and no lift is needed.

        Returns the Z the tool is left at.
        """
        if not ramp_points:
            return ramp_start_height

        def z_at(distance: float) -> float:
            # The end of the ramp is the pass floor EXACTLY. Recomputing it as
            # `start - depth` leaves float dust that differs between the two Z datums,
            # and a "did we get there?" comparison then emitted an extra move in one
            # frame and not the other.
            if ramp_distance <= 0 or distance >= ramp_distance - 1e-12:
                return floor_z
            return ramp_start_height - (distance / ramp_distance) * ramp_depth

        def tab_at(distance: float) -> bool:
            return any(a <= distance <= b for a, b in tab_zones)

        # Distances of the ramp's own waypoints, plus every tab boundary that falls
        # inside the ramp, so a tab can start and end mid-segment.
        waypoints = []      # (distance, x, y)
        distance = 0.0
        previous = start_point
        for x, y, _ in ramp_points:
            distance += self._distance_2d(previous, (x, y))
            waypoints.append((min(distance, ramp_distance), x, y))
            previous = (x, y)

        cuts = sorted({d for zone in tab_zones for d in zone
                       if 0.0 < d < waypoints[-1][0]})

        expanded = []
        index = 0
        previous_distance, previous_point = 0.0, start_point
        for target, x, y in waypoints:
            span = target - previous_distance
            while index < len(cuts) and cuts[index] < target:
                cut = cuts[index]
                t = (cut - previous_distance) / span if span > 1e-12 else 0.0
                expanded.append((cut,
                                 previous_point[0] + t * (x - previous_point[0]),
                                 previous_point[1] + t * (y - previous_point[1])))
                index += 1
            expanded.append((target, x, y))
            previous_distance, previous_point = target, (x, y)

        current_z = ramp_start_height
        previous_distance = 0.0
        tab_number = 0
        for step, (target, x, y) in enumerate(expanded, 1):
            middle = (previous_distance + target) / 2.0
            # The ramp only descends, so the end of the piece is its deepest point:
            # testing the midpoint instead let the last piece before a lift dip a few
            # ten-thousandths into the tab.
            if tab_zones and tab_at(middle) and z_at(target) < tab_z:
                if abs(current_z - tab_z) > 1e-9:
                    tab_number += 1
                    gcode.append(f"G1 Z{tab_z:.4f} F{self.plunge_rate}  "
                                 f"; Tab lift during ramp")
                    current_z = tab_z
                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")
            else:
                z = z_at(target)
                if abs(current_z - tab_z) < 1e-9 and current_z > z + 1e-9:
                    # Leaving a tab: drop back onto the ramp schedule before moving on.
                    gcode.append(f"G1 Z{z_at(previous_distance):.4f} "
                                 f"F{self.plunge_rate}  ; Tab end")
                gcode.append(f"G1 X{x:.4f} Y{y:.4f} Z{z:.4f} "
                             f"F{self.ramp_feed_rate}  ; Ramp segment {step}")
                current_z = z
            previous_distance = target
        return current_z

    def _generate_contour_gcode(self,
                               contour_points: List[Tuple[float, float]],
                               contour_type: str,
                               offset_direction: int,
                               clockwise: bool,
                               remove_tabs_at_end: bool,
                               defer_tab_removal: bool = False) -> List[str]:
        """Generate G-code for contour cutting (perimeter or pocket contour) with tabs

        Shared logic for both perimeter and pocket contour operations.
        Supports multi-pass cutting for thick materials based on max_slotting_depth.
        Tabs are only cut on the final pass.

        Args:
            contour_points: List of (x, y) coordinates defining the contour
            contour_type: "perimeter" or "pocket" (for comments)
            offset_direction: +1 for outward offset (perimeter), -1 for inward (pocket)
            clockwise: True for CW (perimeter), False for CCW (pocket interior)
            remove_tabs_at_end: Whether to generate tab removal pass (False for pockets)
            defer_tab_removal: when True, don't emit the tab-removal pass inline; instead
                stash the computed tab positions on self._deferred_tab_positions so the
                caller (multi-part job assembler) can emit all parts' tab removals as a
                final collated phase. Used only for the perimeter in job mode.
        """
        gcode = []

        # Calculate number of passes needed based on material thickness and max slotting depth
        # Total depth = from material top to cut depth (which is below Z=0)
        total_cut_depth = self.material_top - self.cut_depth  # e.g., 0.25 - (-0.02) = 0.27"
        num_passes = self.passes_for_depth(total_cut_depth, self.max_slotting_depth)

        if num_passes > 1:
            actual_depth_per_pass = total_cut_depth / num_passes
            gcode.append(f"(Multi-pass {contour_type}: {num_passes} passes @ {actual_depth_per_pass:.3f}\" each, max {self.max_slotting_depth:.3f}\" per pass)")

        # Create offset path
        contour_poly = Polygon(contour_points)

        # Buffer by tool radius (positive for outward/perimeter, negative for inward/pocket)
        offset_distance = offset_direction * self.tool_radius
        offset_poly = contour_poly.buffer(offset_distance)

        if offset_poly.is_empty:
            center_x, center_y = self._get_polygon_center(contour_poly)
            error_msg = f"{contour_type.capitalize()} at approximately ({center_x:.3f}, {center_y:.3f}) failed offset operation - may have internal corners with radius smaller than {self.tool_diameter:.4f}\" tool can mill"
            self._add_error(error_msg)
            return gcode

        # Get the boundary of the offset polygon
        if hasattr(offset_poly, 'exterior'):
            # GEOS buffer() does NOT reliably orient its output, so normalize to canonical
            # orientation (exterior CCW) explicitly. CCW = climb for an interior pocket;
            # reverse to CW for climb on an outside feature (perimeter).
            offset_poly = orient(offset_poly, 1.0)
            offset_points = list(offset_poly.exterior.coords)[:-1]  # Remove duplicate last point
            if clockwise:
                offset_points = offset_points[::-1]
        else:
            center_x, center_y = self._get_polygon_center(contour_poly)
            error_msg = f"{contour_type.capitalize()} at approximately ({center_x:.3f}, {center_y:.3f}) resulted in invalid geometry after tool compensation - may have internal corners too sharp for {self.tool_diameter:.4f}\" tool"
            self._add_error(error_msg)
            return gcode

        # Calculate segment lengths
        segment_lengths = []
        for i in range(len(offset_points)):
            p1 = offset_points[i]
            p2 = offset_points[(i + 1) % len(offset_points)]
            length = self._distance_2d(p1, p2)
            segment_lengths.append(length)

        # Calculate total contour length
        contour_length = sum(segment_lengths)

        # Will store tab positions for final removal pass (only populated on final pass)
        all_tab_positions = []

        # Calculate equal depth per pass for consistent tool loading
        depth_per_pass = total_cut_depth / num_passes

        # A tab stands from the finished cut depth up to `tab_top_z`, and EVERY pass
        # whose floor is below that lifts over the tab zones - not just the final one.
        # Lifting only on the final pass meant the intermediate passes cut straight
        # through the tabs at their own depth: on 5-pass 1/4" aluminum the tabs ended up
        # 0.054" tall instead of the configured 0.15", a third of the holding area the
        # operator was told they had. The removal pass reads this to know how much
        # material it really has to step through.
        tab_top_z = min(self.material_top, self.cut_depth + self.tab_height)
        self._tab_material_top = tab_top_z

        # Tabs are distributed over the WHOLE perimeter. Sizing them against
        # `contour_length - ramp_distance` collapsed on small parts: aluminum's 4 degree
        # ramp is ~4.6" on 1/4" stock, so a 1"x1" part had a shorter perimeter than its
        # own ramp and got a negative spacing with all three tabs stacked at one point.
        tab_zones_by_distance = []
        num_tabs = 0
        actual_tab_spacing = 0.0
        if self.tabs_enabled:
            if 3 * self.tab_width > contour_length:
                self._add_error(
                    f"{contour_type.capitalize()} is only {contour_length:.3f}\" around, "
                    f"which is too short for three {self.tab_width:.3f}\" tabs. The part "
                    f"is too small for tabbed profiling at these settings: use narrower "
                    f"tabs, or hold this part another way and turn tabs off.")
                return gcode
            num_tabs = max(3, int(math.ceil(contour_length / self.tab_spacing)))
            actual_tab_spacing = contour_length / num_tabs
            half_tab_width = self.tab_width / 2
            for i in range(num_tabs):
                centre = actual_tab_spacing * (i + 0.5)
                tab_zones_by_distance.append((centre - half_tab_width,
                                              centre + half_tab_width))

        # Multi-pass cutting loop
        for pass_num in range(1, num_passes + 1):
            is_final_pass = (pass_num == num_passes)

            # Calculate target depth for this pass (equal increments)
            if is_final_pass:
                # Final pass goes exactly to target depth to avoid rounding errors
                pass_cut_depth = self.cut_depth
            else:
                # Intermediate passes cut equal increments from material top
                pass_cut_depth = self.material_top - (pass_num * depth_per_pass)

            if num_passes > 1:
                gcode.append(f"")
                gcode.append(f"(===== PASS {pass_num}/{num_passes} - cutting to {pass_cut_depth:.3f}\" =====)")

            # Calculate ramp start height (close to material surface)
            ramp_start_height = self.material_top + self.ramp_start_clearance

            # Calculate ramp-in distance using material-specific ramp angle
            ramp_depth = ramp_start_height - pass_cut_depth
            ramp_distance = ramp_depth / math.tan(math.radians(self.ramp_angle))
            gcode.append(f"(Ramp-in: {ramp_distance:.4f}\" at {self.ramp_angle} deg)")

            # This pass lifts over the tabs if its floor would otherwise go below the
            # top of the standing tab. A pass whose floor is still above `tab_top_z`
            # leaves the tab material intact by itself and needs no lift.
            tab_zones = tab_zones_by_distance if pass_cut_depth < tab_top_z - 1e-9 else []
            tab_z = tab_top_z
            if is_final_pass and self.tabs_enabled:
                gcode.append(f"(Tabs: {num_tabs} tabs - desired spacing: {self.tab_spacing:.2f}\", actual: {actual_tab_spacing:.2f}\" - width: {self.tab_width:.4f}\")")
            elif is_final_pass and not self.tabs_enabled:
                gcode.append(f"(Tabs disabled - perimeter will be cut through completely)")

            # Move to start
            start = offset_points[0]
            gcode.append(f"G1 X{start[0]:.4f} Y{start[1]:.4f} F{self.traverse_rate}  ; Move to perimeter start")
            gcode.extend(self._approach_ramp_start(ramp_start_height))

            # Ramp in along the perimeter path
            # Calculate points along perimeter for ramping
            ramp_points = []
            current_ramp_dist = 0
            current_z = ramp_start_height
            ramp_end_segment = 0  # Track which segment the ramp ends on

            for i in range(len(offset_points)):
                p1 = offset_points[i]
                p2 = offset_points[(i + 1) % len(offset_points)]
                seg_len = segment_lengths[i]

                if current_ramp_dist >= ramp_distance:
                    break  # Ramp complete

                if current_ramp_dist + seg_len <= ramp_distance:
                    # Entire segment is part of ramp
                    z_at_end = ramp_start_height - (current_ramp_dist + seg_len) / ramp_distance * ramp_depth
                    ramp_points.append((p2[0], p2[1], z_at_end))
                    current_ramp_dist += seg_len
                    ramp_end_segment = i + 1  # Ramp ends at the end of this segment
                else:
                    # Partial segment - ramp ends partway through
                    remaining_ramp = ramp_distance - current_ramp_dist
                    t = remaining_ramp / seg_len
                    final_x = p1[0] + t * (p2[0] - p1[0])
                    final_y = p1[1] + t * (p2[1] - p1[1])
                    ramp_points.append((final_x, final_y, pass_cut_depth))
                    current_ramp_dist = ramp_distance
                    ramp_end_segment = i  # Ramp ends partway through this segment
                    break

            # Execute the ramp, lifting over any tab the ramp would otherwise cut
            # through. The ramp descends along the perimeter, so once it is below the
            # top of a tab it is removing that tab's material - the very thing the later
            # passes are carefully preserving.
            current_z = self._emit_ramp_moves(
                gcode, ramp_points, offset_points[0], ramp_start_height, ramp_depth,
                ramp_distance, pass_cut_depth, tab_zones, tab_z)

            # Ensure we're at full depth
            if current_ramp_dist < ramp_distance:
                # Calculate remaining depth to descend
                if ramp_points:
                    current_pos = ramp_points[-1]
                    current_z = current_pos[2]
                    remaining_depth = current_z - pass_cut_depth

                    if remaining_depth > 0.001:  # Only if significant depth remains
                        # Use small helical loop instead of straight plunge
                        helix_radius = self.tool_radius * self.helix_radius_multiplier  # Helix radius from material preset
                        helix_center_x = current_pos[0]
                        helix_center_y = current_pos[1]

                        # Calculate number of helical loops needed
                        circumference = 2 * math.pi * helix_radius
                        depth_per_loop = circumference * math.tan(math.radians(self.ramp_angle))
                        num_loops = max(1, int(math.ceil(remaining_depth / depth_per_loop)))
                        depth_per_loop_actual = remaining_depth / num_loops

                        gcode.append(f"(Perimeter too short - using helical finish: {num_loops} loops at {self.ramp_angle} deg)")

                        # Move to edge of helix radius
                        start_x = helix_center_x + helix_radius
                        start_y = helix_center_y
                        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.feed_rate}  ; Move to helix start")

                        # Perform helical loops
                        for loop_num in range(num_loops):
                            target_z = current_z - (loop_num + 1) * depth_per_loop_actual
                            gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical loop {loop_num + 1}/{num_loops} CCW for climb milling")

                        # Return to perimeter path
                        gcode.append(f"G1 X{helix_center_x:.4f} Y{helix_center_y:.4f} F{self.feed_rate}  ; Return to perimeter")
                        current_z = target_z
                    else:
                        current_z = pass_cut_depth

            gcode.append("")

            # Cut around the perimeter, lifting over the tab zones. Distances here are
            # measured along the contour from `offset_points[0]`, the same frame the tab
            # zones and the ramp use, so a tab has ONE position whichever code sees it.
            # The lap begins where the RAMP ended, which is why it starts at
            # `current_ramp_dist` and the first segment is short by however far into
            # that segment the ramp went.
            lap_start = current_ramp_dist
            lap_end = current_ramp_dist + contour_length
            current_distance = lap_start
            tab_number = 0
            # The lap re-cuts the whole perimeter, so every tab is crossed exactly once:
            # a tab behind the lap's start point is met at the END of the lap, one
            # contour length further along. A tab the lap STARTS inside is met at both
            # ends, in two pieces - the ramp can finish anywhere, tabs included, and the
            # half left behind would otherwise be cut away by the closing move.
            lap_tab_zones = []       # (start, end, tab_idx, piece_order)
            for tab_idx, (a, b) in enumerate(tab_zones):
                if a >= lap_start:
                    lap_tab_zones.append((a, b, tab_idx, 0))
                elif b <= lap_start:
                    lap_tab_zones.append((a + contour_length, b + contour_length,
                                          tab_idx, 0))
                else:
                    lap_tab_zones.append((lap_start, b, tab_idx, 1))
                    lap_tab_zones.append((a + contour_length, lap_start + contour_length,
                                          tab_idx, 0))

            # Store tab positions for the tab removal pass (only on final pass).
            # A single tab can straddle multiple contour segments — common on
            # curves, where circles are approximated as many short chords — so
            # we keep an ordered waypoint list per tab. The removal pass plunges
            # at the first waypoint and traces every piece in order; a tab that
            # lives entirely on one segment ends up with two waypoints, same as
            # the old straight-line behavior.
            tab_waypoints_by_idx = {}  # tab_idx -> [(x, y), (x, y), ...]

            # Create perimeter points list starting from where ramp ended
            # Continue from ramp_end_segment to end, then wrap around to start
            remaining_points = offset_points[ramp_end_segment:] + offset_points[:ramp_end_segment]
            remaining_lengths = segment_lengths[ramp_end_segment:] + segment_lengths[:ramp_end_segment]

            # The tool is at the RAMP END, not at the start of that segment. Starting
            # the first segment from its own first vertex made the lap's distances run
            # ahead of the real path by however far into the segment the ramp went - and
            # a tab placed in that stretch was then cut on the way back to it.
            if ramp_points:
                ramp_end_point = (ramp_points[-1][0], ramp_points[-1][1])
                next_vertex_distance = (sum(segment_lengths[:ramp_end_segment])
                                        + segment_lengths[ramp_end_segment % len(segment_lengths)])
                remaining_points = [ramp_end_point] + remaining_points[1:]
                remaining_lengths = ([max(0.0, next_vertex_distance - current_ramp_dist)]
                                     + remaining_lengths[1:])

            # Helper function to process a segment with tab checking
            def process_segment(p1, p2, seg_start_dist, seg_length):
                nonlocal tab_number, current_z, tab_waypoints_by_idx

                if seg_length == 0:
                    return

                seg_end_dist = seg_start_dist + seg_length

                # Find all tab zones that intersect this segment. Every pass whose floor
                # is below the tab top lifts, not only the final one - otherwise the
                # intermediate passes mill away the material the tabs are made of.
                intersecting_tabs = []
                for tab_start, tab_end, tab_idx, piece in lap_tab_zones:
                    # Check if tab zone overlaps with segment
                    if tab_start < seg_end_dist and tab_end > seg_start_dist:
                        # Clamp to segment boundaries
                        overlap_start = max(tab_start, seg_start_dist)
                        overlap_end = min(tab_end, seg_end_dist)
                        intersecting_tabs.append((overlap_start, overlap_end,
                                                  (tab_idx, piece)))

                if not intersecting_tabs:
                    # No tabs in this segment - ensure we're at cut depth, then cut normally
                    if abs(current_z - pass_cut_depth) > 1e-9:
                        gcode.append(f"G1 Z{pass_cut_depth:.4f} F{self.plunge_rate}")
                        current_z = pass_cut_depth
                    gcode.append(f"G1 X{p2[0]:.4f} Y{p2[1]:.4f} F{self.feed_rate}")
                    return

                # Segment has tabs - split it into subsegments
                # Sort intersecting tabs by start distance
                intersecting_tabs.sort(key=lambda x: x[0])

                # Build list of subsegments: [(start_dist, end_dist, is_tab), ...]
                subsegments = []
                current_pos = seg_start_dist

                for overlap_start, overlap_end, tab_key in intersecting_tabs:
                    # Add pre-tab segment if there's a gap
                    if current_pos < overlap_start:
                        subsegments.append((current_pos, overlap_start, False, None))

                    # Add tab segment
                    subsegments.append((overlap_start, overlap_end, True, tab_key))
                    current_pos = overlap_end

                # Add post-tab segment if there's remaining length
                if current_pos < seg_end_dist:
                    subsegments.append((current_pos, seg_end_dist, False, None))

                # Process each subsegment
                for sub_start, sub_end, is_tab, tab_key in subsegments:
                    # Calculate XY position at subsegment end
                    t_end = (sub_end - seg_start_dist) / seg_length
                    end_x = p1[0] + t_end * (p2[0] - p1[0])
                    end_y = p1[1] + t_end * (p2[1] - p1[1])

                    if is_tab:
                        # Calculate XY position at subsegment start
                        t_start = (sub_start - seg_start_dist) / seg_length
                        start_x = p1[0] + t_start * (p2[0] - p1[0])
                        start_y = p1[1] + t_start * (p2[1] - p1[1])

                        # Record this sub-segment for the removal pass. Contiguous
                        # pieces of the same tab share an endpoint geometrically,
                        # so we only append the new endpoint on continuations.
                        if tab_key not in tab_waypoints_by_idx:
                            tab_waypoints_by_idx[tab_key] = [(start_x, start_y), (end_x, end_y)]
                        else:
                            tab_waypoints_by_idx[tab_key].append((end_x, end_y))

                        # Move to tab start in XY
                        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.feed_rate}")

                        # Raise Z only if not already at tab height
                        if abs(current_z - tab_z) > 1e-9:
                            tab_number += 1
                            gcode.append(f"G1 Z{tab_z:.4f} F{self.plunge_rate}  ; Tab {tab_number} start")
                            current_z = tab_z

                        # Move across tab (at tab height)
                        gcode.append(f"G1 X{end_x:.4f} Y{end_y:.4f} F{self.feed_rate}")
                    else:
                        # Lower Z only if not already at cut depth
                        if abs(current_z - pass_cut_depth) > 1e-9:
                            gcode.append(f"G1 Z{pass_cut_depth:.4f} F{self.plunge_rate}  ; Tab end")
                            current_z = pass_cut_depth

                        # Normal cutting move (at cut depth)
                        gcode.append(f"G1 X{end_x:.4f} Y{end_y:.4f} F{self.feed_rate}")

            # Process all segments from where ramp ended to closing
            for i in range(len(remaining_points) - 1):
                p1 = remaining_points[i]
                p2 = remaining_points[i + 1]
                seg_length = remaining_lengths[i]

                process_segment(p1, p2, current_distance, seg_length)
                current_distance += seg_length

            # Close the perimeter by returning to where ramp ended
            if ramp_points:
                ramp_end_x, ramp_end_y, _ = ramp_points[-1]
                last_point = remaining_points[-1]

                # The closing move covers the contour distance still owed, so a tab that
                # lives in it is not clipped off the end of the lap. Geometrically it is
                # a chord back to the ramp's end point.
                closing_length = max(self._distance_2d((ramp_end_x, ramp_end_y), last_point),
                                     lap_end - current_distance)

                # Process closing segment
                process_segment(last_point, (ramp_end_x, ramp_end_y), current_distance, closing_length)

            # Store tab positions from final pass for removal. A tab the lap started
            # inside was recorded in two pieces; they are the same physical tab, so they
            # are re-joined in contour order and the removal pass cuts it as one.
            if is_final_pass:
                merged = {}
                for (tab_idx, piece), points in sorted(tab_waypoints_by_idx.items()):
                    existing = merged.get(tab_idx)
                    if existing is None:
                        merged[tab_idx] = list(points)
                    else:
                        existing.extend(p for p in points if p != existing[-1])
                all_tab_positions = sorted(merged.items(), key=lambda kv: kv[0])

            # Retract
            gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        # ===== TAB REMOVAL PASS =====
        # Remove tabs in star pattern to gradually release the part (only if tabs were created
        # and removal is enabled). Note: Pocket contours NEVER remove tabs (center material
        # remains for manual removal).
        # In job mode (defer_tab_removal) the caller collates all parts' tab removals into a
        # final phase, so we stash the positions instead of emitting the pass inline here.
        if defer_tab_removal:
            self._deferred_tab_positions = all_tab_positions
        elif all_tab_positions and remove_tabs_at_end:
            gcode.extend(self._generate_tab_removal_gcode(all_tab_positions))

        return gcode

    def _generate_tab_removal_gcode(self, all_tab_positions) -> List[str]:
        """Generate the star-pattern tab-removal pass for a perimeter's tabs.

        Args:
            all_tab_positions: list of (tab_idx, waypoints) sorted by tab_idx, as captured
                on the final perimeter pass. waypoints[0] is the kerf entry point; the rest
                are the cut-through points for that tab.

        Returns the tab-removal toolpath lines (empty list if there are no tabs).
        """
        gcode = []
        if not all_tab_positions:
            return gcode

        # How much material is standing in each tab, and how many passes the active
        # depth-per-pass limit needs to chew it. Until 2026-08-24 tab removal was the
        # ONE cut that ignored max_slotting_depth: it plunged to the through depth and
        # slotted the full tab height in a single move. On thin stock a tab can be the
        # entire plate thickness, so a program whose profile politely stepped down in
        # 0.027" passes still buried the cutter 0.133" deep at the tabs - which is how
        # a 1/8" end mill snapped on a real 6061 part. Tabs now step down like every
        # other cut, re-entering through the open kerf for each pass.
        tab_top = getattr(self, '_tab_material_top', None)
        if tab_top is None:   # deferred call without a prior contour pass: assume full tabs
            tab_top = min(self.material_top, self.cut_depth + self.tab_height)
        total_tab_depth = max(0.0, tab_top - self.cut_depth)
        depth_limit = getattr(self, 'max_slotting_depth', None) or total_tab_depth
        tab_passes = self.passes_for_depth(total_tab_depth, depth_limit)
        tab_step = total_tab_depth / tab_passes

        gcode.append("")
        gcode.append("(===== TAB REMOVAL PASS =====)")
        gcode.append(f"(Removing {len(all_tab_positions)} tabs in star pattern)")
        if tab_passes > 1:
            gcode.append(f"(Each tab in {tab_passes} passes @ {tab_step:.3f}\" each, "
                         f"max {depth_limit:.3f}\" per pass)")

        # all_tab_positions is already sorted by tab_idx.

        # Generate star pattern order: alternates between first and second half
        # For 4 tabs (0,1,2,3): order is 0,2,1,3
        # For 6 tabs (0,1,2,3,4,5): order is 0,3,1,4,2,5
        num_tabs = len(all_tab_positions)
        star_order = []
        half = num_tabs // 2
        for i in range(half):
            star_order.append(i)
            if i + half < num_tabs:
                star_order.append(i + half)
        # Handle odd number of tabs
        if num_tabs % 2 == 1:
            star_order.append(num_tabs - 1)

        gcode.append(f"(Star pattern order: {', '.join(str(i+1) for i in star_order)})")
        gcode.append("")

        # Remove each tab in star order
        for removal_num, tab_order_idx in enumerate(star_order, 1):
            tab_idx, waypoints = all_tab_positions[tab_order_idx]
            start_x, start_y = waypoints[0]

            gcode.append(f"(Tab {tab_idx + 1} removal - #{removal_num} in sequence)")

            # Rapid to retract height (like moving between holes)
            gcode.append(f"G0 Z{self.retract_height:.4f}")

            # Rapid to position just before the tab (in the kerf)
            gcode.append(f"G0 X{start_x:.4f} Y{start_y:.4f}  ; Move to tab start (in kerf)")

            # Step down through the tab, one depth-limited pass at a time. Every pass
            # re-enters through the kerf entry point, which the perimeter cut opened to
            # full depth - so each plunge is into air, never into the tab itself.
            for pass_num in range(1, tab_passes + 1):
                pass_z = (self.cut_depth if pass_num == tab_passes
                          else tab_top - pass_num * tab_step)
                if pass_num > 1:
                    gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract for next tab pass")
                    gcode.append(f"G0 X{start_x:.4f} Y{start_y:.4f}  ; Back to tab start (in kerf)")

                # Plunge to this pass's depth in empty kerf at approach rate
                gcode.append(f"G1 Z{pass_z:.4f} F{self.approach_rate}  ; Plunge in kerf")

                # Cut through each piece of the tab in contour order so curved
                # tabs (spanning multiple short chord segments) get fully removed.
                for ex, ey in waypoints[1:]:
                    gcode.append(f"G1 X{ex:.4f} Y{ey:.4f} F{self.feed_rate}  ; Cut through tab")

            # Retract after each tab
            gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
            gcode.append("")

        return gcode

    def _generate_perimeter_gcode(self, perimeter_points: List[Tuple[float, float]],
                                  defer_tab_removal: bool = False) -> List[str]:
        """Generate G-code for perimeter with tabs and tool compensation (offset outward)

        Wrapper function that calls _generate_contour_gcode with perimeter-specific parameters.
        Supports multi-pass cutting for thick materials based on max_slotting_depth.
        Tabs are only cut on the final pass.
        """
        return self._generate_contour_gcode(
            contour_points=perimeter_points,
            contour_type="perimeter",
            offset_direction=+1,  # Outward offset
            clockwise=True,       # CW for climb milling on outside features
            remove_tabs_at_end=self.config.remove_tabs,  # Config-based tab removal
            defer_tab_removal=defer_tab_removal
        )

    def _generate_pocket_contour_gcode(self, pocket_points: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for pocket contour (outline only) with tabs

        Contours large pockets instead of fully clearing them to save machining time.
        The center material remains attached by tabs and must be manually removed.

        Args:
            pocket_points: List of (x, y) coordinates defining the pocket boundary

        Returns:
            List of G-code lines for pocket contouring operation
        """
        gcode = ["(WARNING: Interior pocket contour - center material requires manual removal)"]

        gcode.extend(self._generate_contour_gcode(
            contour_points=pocket_points,
            contour_type="pocket",
            offset_direction=-1,  # Inward offset
            clockwise=False,      # CCW for climb milling on inside features
            remove_tabs_at_end=False  # NEVER remove tabs on pockets - material stays in place
        ))

        return gcode

    # ---- Chamfer / edge break (V-tool) ------------------------------------------------
    # A symmetric V-tool centred exactly ON the true (uncompensated) contour and dropped
    # `chamfer_depth` below the material top breaks that top edge by `chamfer_width`
    # horizontally: at height h above the tip the cone radius is h*tan(half_angle), so as
    # the tool follows the edge its flank sweeps the plane running from (edge, top) down
    # to (edge, top - depth). That makes a chamfer pass a plain ZERO-COMPENSATION contour
    # trace at a shallow depth - no offset math - and the same routine works on the
    # perimeter, a hole, or a pocket. See docs/TOOL_COMPENSATION_GUIDE.md.

    @staticmethod
    def chamfer_depth(chamfer_width: float, included_angle: float = 90.0) -> float:
        """Tip depth below the material top that yields `chamfer_width` of horizontal edge
        break with a V-tool of the given included angle. A 90 deg V-bit gives depth ==
        width; a narrower tool must go deeper for the same width."""
        half = math.radians(min(179.0, max(1.0, included_angle)) / 2.0)
        return chamfer_width / math.tan(half)

    @staticmethod
    def chamfer_max_width(tool_diameter: float, included_angle: float = 90.0) -> float:
        """Widest edge break this V-tool can cut in one pass: the cone reaches its full
        radius at the material top, so the break can't exceed the tool radius."""
        return tool_diameter / 2.0

    def _orient_ring(self, points: List[Tuple[float, float]], clockwise: bool) -> List[Tuple[float, float]]:
        """Return `points` as a closed ring (no repeated last vertex) wound CW or CCW.
        Shapely's orient() gives a CCW exterior; reverse it for CW."""
        poly = Polygon(points)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or not hasattr(poly, 'exterior'):
            ring = list(points)
        else:
            ring = list(orient(poly, 1.0).exterior.coords)[:-1]
        if clockwise:
            ring = ring[::-1]
        return ring

    def _generate_chamfer_ring_gcode(self, points: List[Tuple[float, float]],
                                     cut_z: float, clockwise: bool) -> List[str]:
        """One chamfer lap around a single closed contour at `cut_z`, entered by ramping
        along the ring (never a straight plunge onto the edge)."""
        ring = self._orient_ring(points, clockwise)
        if len(ring) < 3:
            return []

        gcode = []
        ramp_start_height = self.material_top + self.ramp_start_clearance
        start = ring[0]
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract before chamfer move")
        gcode.append(f"G0 X{start[0]:.4f} Y{start[1]:.4f}  ; Rapid to chamfer start")
        gcode.extend(self._approach_ramp_start(ramp_start_height))
        self._emit_ring_ramp(gcode, ring, ramp_start_height, cut_z)  # ends back at ring[0] at depth
        self._emit_ring_cut_with_corner_slowdown(gcode, ring, self.feed_rate)
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
        return gcode

    def _generate_chamfer_gcode(self, rings: List[Dict[str, Any]], chamfer_width: float,
                                included_angle: float = 90.0) -> List[str]:
        """Generate a complete chamfer operation over a set of contours.

        Args:
            rings: [{'points': [(x, y), ...], 'clockwise': bool, 'label': str,
                     'min_radius': float or None}] - `clockwise` should be True for
                    outside edges (perimeter) and False for inside edges (holes,
                    pockets) so the V-tool climb mills; `min_radius`, when given, is the
                    tightest concave radius on the contour and is checked against the
                    cone radius at the material top.
            chamfer_width: horizontal edge break, inches.
            included_angle: V-tool included angle in degrees (90 is typical).

        Validation errors are recorded on self.errors, which callers check.
        """
        gcode = []
        if not rings:
            return gcode

        max_width = self.chamfer_max_width(self.tool_diameter, included_angle)
        if chamfer_width > max_width + 1e-9:
            self._add_error(
                f"Chamfer width {chamfer_width:.4f}\" exceeds what a {self.tool_diameter:.4f}\" "
                f"V-tool can cut in one pass, max {max_width:.4f}\". Use a larger V-tool or a "
                f"smaller chamfer.")
            return gcode

        depth = self.chamfer_depth(chamfer_width, included_angle)
        if depth >= self.material_thickness:
            self._add_error(
                f"Chamfer of {chamfer_width:.4f}\" needs a {depth:.4f}\" deep cut with a "
                f"{included_angle:.0f} deg V-tool, which is through {self.material_thickness:.4f}\" "
                f"stock. Use a smaller chamfer or a wider V-tool.")
            return gcode

        cut_z = self.material_top - depth

        gcode.append("(===== CHAMFER =====)")
        gcode.append(f"(V-tool {self.tool_diameter:.4f}\" diam, {included_angle:.0f} deg included angle)")
        gcode.append(f"(Edge break {chamfer_width:.4f}\" wide, tip depth {depth:.4f}\" below material top)")
        gcode.append("(Tool centre follows the true edge - no cutter compensation)")
        gcode.append("")

        for ring in rings:
            points = ring.get('points') or []
            if len(points) < 3:
                continue
            label = sanitize_comment(ring.get('label') or 'Contour')
            min_radius = ring.get('min_radius')
            if min_radius is not None and chamfer_width >= min_radius - 1e-9:
                self._add_error(
                    f"Chamfer width {chamfer_width:.4f}\" does not fit {label}, whose tightest "
                    f"radius is {min_radius:.4f}\". The V-tool would cut past the far wall.")
                continue
            gcode.append(f"({label})")
            gcode.extend(self._generate_chamfer_ring_gcode(points, cut_z, bool(ring.get('clockwise'))))
            gcode.append("")

        return gcode

    # ---- Standard-mode deburr / chamfer pass ------------------------------------------
    # The single-tool flows (generate_gcode, generate_part_phases) can append one V-bit
    # edge-break pass behind a manual tool change: profile first with tabs holding every
    # part, then the chamfer, then (when the machine removes tabs) a change back to the
    # end mill for the tab-removal pass. Tabs are what make cutting after the profile
    # safe, so a tabless part with a perimeter refuses the pass outright - the same
    # guard tooling.py enforces on multi-tool plans.

    @staticmethod
    def chamfer_fits(poly: Polygon, width: float) -> bool:
        """Whether a `width` chamfer fits everywhere around this region.

        The V-tool reaches `width` sideways from the edge at the material top, so
        opposite edges less than 2 x width apart have their chamfers meet and the
        material between them disappears. Eroding the region by `width` finds that
        without measuring any particular feature: an EMPTY result means the shape is
        thin everywhere, and MORE pieces than it started with means the erosion ate
        through a neck - which is exactly the narrow spot in question. Testing only for
        empty misses the common case: two generous lobes joined by a thin waist erode
        to two healthy islands while the waist is machined away.
        """
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            return False
        eroded = poly.buffer(-width)
        if eroded.is_empty:
            return False
        pieces = len(eroded.geoms) if hasattr(eroded, 'geoms') else 1
        original = len(poly.geoms) if hasattr(poly, 'geoms') else 1
        return pieces <= original

    def _chamfer_pass_rings(self) -> List[Dict[str, Any]]:
        """The contours the deburr pass traces, from this part's own features.

        Uncompensated contours, as _generate_chamfer_gcode expects: the perimeter is
        climb-milled clockwise as an outside edge, holes and pockets counter-clockwise
        as inside edges. Only features the program actually cut are offered (self.holes
        is the millable set), so the pass never rides an edge that does not exist.
        Fit problems are recorded on self.errors.
        """
        spec = self.chamfer_pass
        width = spec['width']
        targets = spec['targets']
        rings: List[Dict[str, Any]] = []

        if 'perimeter' in targets and self.perimeter:
            if not self.chamfer_fits(Polygon(self.perimeter), width):
                self._add_error(
                    f"Deburr pass: this part is too narrow somewhere for a {width:.4f} in "
                    f"chamfer on both sides. The V-bit would cut away the material between "
                    f"the two edges.")
            else:
                rings.append({'points': self.perimeter, 'clockwise': True,
                              'label': 'Perimeter', 'min_radius': None})

        if 'holes' in targets:
            for hole in (self.holes or []):
                cx, cy = hole['center']
                radius = hole['diameter'] / 2.0
                rings.append({'points': self._tessellate_circle(cx, cy, radius),
                              'clockwise': False,
                              'label': f"Hole {hole['diameter']:.3f} in dia at X{cx:.3f} Y{cy:.3f}",
                              'min_radius': radius})

        if 'pockets' in targets:
            for points in (self.pockets or []):
                poly = Polygon(points)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if not self.chamfer_fits(poly, width):
                    self._add_error(
                        f"Deburr pass: a pocket at X{poly.centroid.x:.3f} "
                        f"Y{poly.centroid.y:.3f} is too narrow somewhere for a "
                        f"{width:.4f} in chamfer on both walls.")
                    continue
                rings.append({'points': points, 'clockwise': False,
                              'label': f"Pocket at X{poly.centroid.x:.3f} Y{poly.centroid.y:.3f}",
                              'min_radius': None})

        return rings

    def _chamfer_pass_body(self) -> List[str]:
        """The deburr pass toolpath body (no tool-change pause).

        Generated with the V-bit's geometry: tool_diameter is swapped to the bit for the
        duration so the width-vs-bit-radius check inside _generate_chamfer_gcode judges
        the tool that will actually be in the spindle, then restored. Refusals land on
        self.errors; callers must check for new errors after this returns.
        """
        spec = self.chamfer_pass
        if self.perimeter and not self.tabs_enabled:
            self._add_error(
                'A deburr / chamfer pass needs tabs: without them the profile cut leaves '
                'the part loose on the table before the V-bit runs. Enable tabs or remove '
                'the chamfer pass.')
            return []
        rings = self._chamfer_pass_rings()
        if not rings:
            return []
        original = self.tool_diameter
        self.tool_diameter = spec['bit_diameter']
        try:
            return self._generate_chamfer_gcode(rings, spec['width'], spec['bit_angle'])
        finally:
            self.tool_diameter = original

    def _chamfer_tool_change_gcode(self, to_vbit: bool) -> List[str]:
        """The pause-and-park block that swaps between the milling cutter and the V-bit."""
        import feeds_speeds

        spec = self.chamfer_pass
        bit_desc = f"{spec['bit_diameter']:.4f} in {spec['bit_angle']:.0f} deg V-bit"
        mill_desc = f"{self.tool_diameter:.4f} in end mill"
        if to_vbit:
            title = 'TOOL CHANGE - DEBURR CHAMFER PASS'
            instructions = [f"Remove the {mill_desc}",
                            f"Install the {bit_desc}"]
        else:
            title = 'TOOL CHANGE - BACK TO END MILL'
            instructions = [f"Remove the {bit_desc}",
                            f"Install the {mill_desc} for tab removal"]
        instructions += [
            f'Re-zero G54 Z to {self.z_zero_surface()} with the new tool, not with G92',
            'Do NOT change the X or Y zero',
        ]
        material_key = feeds_speeds.canonical_material_key(
            getattr(self, 'material_id', getattr(self, 'material_name', '')))
        if feeds_speeds.is_aluminum_material(material_key):
            instructions += [
                'Confirm incoming cutter is sharp, clean, and approved for aluminum',
                'Clean collet, minimize stickout, and verify low runout',
                'Confirm continuous directed air and a clear chip escape path before restart',
            ]
            if material_key == 'aluminum_6063':
                instructions.append(
                    'Confirm proven aluminum-compatible lubricant or MQL is ready for 6063')
        return self._generate_pause_and_park_gcode(
            title, instructions, tool_change=True,
            resume_checkpoint=self._next_resume_checkpoint(),
            resume_description=(bit_desc if to_vbit else mill_desc),
        )

    def _chamfer_pass_tool_table(self) -> List[str]:
        """Header tool list for a single-tool program that ends with the V-bit pass."""
        spec = self.chamfer_pass
        return [
            f"T1: {self.tool_diameter:.4f} in end mill - all cutting",
            f"T2: {spec['bit_diameter']:.4f} in {spec['bit_angle']:.0f} deg V-bit - "
            f"deburr chamfer pass",
        ]

    def _offset_coordinate(self, line: str, axis: str, offset: float) -> str:
        """
        Offset a coordinate in a G-code line by adding an offset.

        Generic method for offsetting X, Y, or Z coordinates in G-code lines.

        Args:
            line: G-code line to modify
            axis: Coordinate axis to offset ('X', 'Y', or 'Z')
            offset: Offset to add to coordinate value

        Returns:
            Modified G-code line with offset coordinate
        """
        def replace_coord(match):
            coord_val = float(match.group(1))
            new_val = coord_val + offset
            return f'{axis}{new_val:.4f}'

        # Match axis letter followed by optional minus and digits
        return re.sub(rf'{axis}(-?\d+\.?\d*)', replace_coord, line)

    def _adjust_y_coordinate(self, line: str, y_offset: float) -> str:
        """
        Adjust Y coordinate in a G-code line by adding offset.
        Legacy method - wraps generic _offset_coordinate() for backwards compatibility.
        """
        return self._offset_coordinate(line, 'Y', y_offset)

    def _calculate_tube_operation_passes(self, tube_height: float) -> dict:
        """
        Calculate pass parameters for tube operations (facing, cutting).

        Common calculation for both tube facing and cut-to-length operations.
        Both use the same depth strategy: cut just over half the tube height,
        with multiple passes to respect flute length limits.

        Args:
            tube_height: Height of tube in inches (Z dimension)

        Returns:
            Dict with pass calculation results:
            - total_depth: Total depth to cut
            - wall_thickness: Wall thickness from material_thickness
            - num_roughing_passes: Number of roughing passes needed
            - roughing_depth_per_pass: Depth per roughing pass
            - num_finishing_passes: Number of finishing passes needed
            - finishing_depth_per_pass: Depth per finishing pass
        """
        # Cutting parameters
        total_depth = tube_height / 2 + self.tube_facing_params['depth_margin']  # Just over half the tube height
        wall_thickness = self.material_thickness  # Wall thickness of box tubing

        # Roughing: respects flute length limit (max per pass from params)
        # 1" tube (0.505"): 2 passes, 2" tube (1.005"): 4 passes
        # Side-facing is low radial engagement, but its former 0.300/0.510 in axial
        # levels reached 1.6D/3.2D with the default 4 mm cutter. Keep each fresh axial
        # engagement to at most 1D; exact tooling may authorize more, but generic CAM
        # must not assume it. The operator preflight separately confirms total reach.
        max_roughing_depth = min(
            self.tube_facing_params['max_roughing_depth'], self.tool_diameter)
        num_roughing_passes = max(1, int(math.ceil(total_depth / max_roughing_depth)))
        roughing_depth_per_pass = total_depth / num_roughing_passes

        # Finishing: light stepover allows deeper passes (max per pass from params)
        # 1" tube (0.505"): 1 pass, 2" tube (1.005"): 2 passes
        max_finishing_depth = min(
            self.tube_facing_params['max_finishing_depth'], self.tool_diameter)
        num_finishing_passes = max(1, int(math.ceil(total_depth / max_finishing_depth)))
        finishing_depth_per_pass = total_depth / num_finishing_passes

        return {
            'total_depth': total_depth,
            'wall_thickness': wall_thickness,
            'num_roughing_passes': num_roughing_passes,
            'roughing_depth_per_pass': roughing_depth_per_pass,
            'num_finishing_passes': num_finishing_passes,
            'finishing_depth_per_pass': finishing_depth_per_pass
        }

    #: Extra depth a pass must have gone past the nominal wall bottom before the middle
    #: of the tube counts as open. Extruded box tube wall thickness is a nominal figure;
    #: 0.02" covers the usual mill tolerance on 6061 tube.
    TUBE_WALL_CLEAR_MARGIN = 0.02

    def _tube_middle_is_open(self, pass_num: int, depth_per_pass: float,
                             wall_thickness: float) -> bool:
        """Has a previous pass cut clear through the top wall at mid-tube?

        The top wall of box tube spans the FULL width, so mid-tube is solid until some
        pass has milled past its underside. `pass_num` is 0-based, and the pass before
        it reached `pass_num * depth_per_pass` below the top of the tube.

        Only when that is past the wall bottom with margin may a pass cut the two side
        walls and skip the middle - otherwise it would leave an uncut web (which can
        stop cut-to-length severing the tube) and, worse, cross that web at depth.
        """
        cleared_depth = pass_num * depth_per_pass
        return cleared_depth >= wall_thickness + self.TUBE_WALL_CLEAR_MARGIN

    def _parse_tube_size(self, tube_size: str) -> tuple[float, float]:
        """
        Parse tube size string to width and height dimensions.

        Args:
            tube_size: Size string like '1x1', '2x1-standing', '2x1-flat', '1.5x1.5', '2x2'

        Returns:
            (width, height) tuple in inches
        """
        if tube_size == '1x1':
            return (1.0, 1.0)
        elif tube_size == '2x1' or tube_size == '2x1-flat':
            return (2.0, 1.0)  # Flat: wide width, short height (most common)
        elif tube_size == '2x1-standing':
            return (1.0, 2.0)  # Standing: narrow width, tall height
        elif tube_size == '1.5x1.5':
            return (1.5, 1.5)
        elif tube_size == '2x2':
            return (2.0, 2.0)
        else:
            # Default to 1x1 if unknown
            return (1.0, 1.0)

    def _generate_parametric_tube_facing(self, tube_width: float, tube_height: float,
                                          phase: int = 1) -> list[str]:
        """
        Generate tube facing toolpath - face the end of box tubing.

        Squares the end of box tubing with one vertical plunge and two
        horizontal passes (roughing + finishing).

        Coordinate system (tube lying horizontal, end facing spindle):
        - X: across tube width (cut direction)
        - Z: tube height (plunge direction, vertical)
        - Y: facing depth (material removal from tube end, negative = into tube)

        Phase 1 (first end):
        - Roughing tool edge at Y=+0.05"
        - Finishing tool edge at Y=+0.0625"

        Phase 2 (after flip):
        - Roughing tool edge at Y=-0.0125"
        - Finishing tool edge at Y=0"

        Args:
            tube_width: Tube width in inches (X dimension)
            tube_height: Tube height in inches (Z dimension, typically 1" or 2")
            phase: 1 for first end (with stepover), 2 for second end (no stepover)

        Returns:
            List of G-code lines for the facing operation
        """
        gcode = []
        tool_radius = self.tool_diameter / 2.0

        # Calculate pass parameters using shared helper
        passes = self._calculate_tube_operation_passes(tube_height)
        total_depth = passes['total_depth']
        wall_thickness = passes['wall_thickness']
        num_roughing_passes = passes['num_roughing_passes']
        roughing_depth_per_pass = passes['roughing_depth_per_pass']
        num_finishing_passes = passes['num_finishing_passes']
        finishing_depth_per_pass = passes['finishing_depth_per_pass']

        # Tool edge positions for each phase (these are the final face positions)
        if phase == 1:
            # Phase 1: Roughing and finishing positions from params
            roughing_tool_edge = self.tube_facing_params['roughing_tool_edge_p1']
            finishing_tool_edge = self.tube_facing_params['finishing_tool_edge_p1']
        else:
            # Phase 2: Roughing and finishing positions from params
            roughing_tool_edge = self.tube_facing_params['roughing_tool_edge_p2']
            finishing_tool_edge = self.tube_facing_params['finishing_tool_edge_p2']

        # Arc clearing parameters (needed to calculate roughing_y offset)
        arc_advance = self.tube_facing_params['arc_advance']  # How far each arc advances in X
        arc_radius = self.tube_facing_params['arc_radius']  # Arc radius
        half_advance = arc_advance / 2
        j_offset = math.sqrt(arc_radius**2 - half_advance**2)

        # Tool CENTER positions for tube facing:
        # - Coordinate system: +Y is INTO the tube (toward tube body)
        # - Kept material (tube body) is at +Y, tube face is at Y≈0
        # - Tool's +Y edge (toward tube body) defines the face position
        #
        # With positive J, G3 (CCW) arc goes through TOP of circle (max Y).
        # Arc center Y = roughing_y + j_offset
        # Top of circle Y = center_y + arc_radius = roughing_y + j_offset + arc_radius
        #
        # At arc CHORD (start/end): tool center Y = roughing_y
        # At arc PEAK (top of circle): tool center Y = roughing_y + j_offset + arc_radius
        #
        # The PEAK is where the tool cuts deepest into the tube (maximum +Y edge).
        # Roughing should never exceed roughing_tool_edge, so we set PEAK at that limit.
        #
        # For roughing +Y edge at PEAK to equal roughing_tool_edge:
        #   (roughing_y + j_offset + arc_radius) + tool_radius = roughing_tool_edge
        #   roughing_y = roughing_tool_edge - tool_radius - j_offset - arc_radius
        roughing_y = roughing_tool_edge - tool_radius - j_offset - arc_radius
        finishing_y = finishing_tool_edge - tool_radius

        # X positions (tool edge 0.05" from material edge)
        clearance = tool_radius + 0.05
        start_x = tube_width + clearance  # Far side
        end_x = -clearance  # Near side

        # Z positions
        # + dry_run_lift: in a dry run the whole tube frame rises with the plate
        # frame, so the tool traces the same path in the air above the jig.
        z_top = tube_height + self.dry_run_lift        # Top of tube
        z_safe = tube_height + self.dry_run_lift + 0.25  # Safe height above tube
        z_final = z_top - total_depth  # Final depth (just over half height)

        chord_face = roughing_y + tool_radius  # Face position at chord (start/end of arc)
        gcode.append(f'( Tube facing: {tube_width:.2f}" wide x {tube_height:.2f}" tall )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" )')
        gcode.append(f'( Total depth: {total_depth:.3f}" )')
        gcode.append(f'( Roughing: {num_roughing_passes} passes of {roughing_depth_per_pass:.3f}" each, +Y edge at Y={roughing_tool_edge:.4f}" )')
        gcode.append(f'( Finishing: {num_finishing_passes} passes of {finishing_depth_per_pass:.3f}" each, +Y edge at Y={finishing_tool_edge:.4f}" )')

        # === ROUGHING PASSES ===
        arc_feed = self.feed_rate

        gcode.append('( === ROUGHING PASSES === )')
        gcode.append(f'( {num_roughing_passes} depth passes with arc clearing )')

        # Calculate wall boundaries for subsequent passes (box tubing is hollow)
        # Back wall (far side): from start_x to inner edge
        back_wall_inner_x = tube_width - wall_thickness - clearance
        # Front wall (near side): from inner edge to end_x
        front_wall_inner_x = wall_thickness + clearance

        for pass_num in range(num_roughing_passes):
            z_cut = z_top - (pass_num + 1) * roughing_depth_per_pass

            if not self._tube_middle_is_open(pass_num, roughing_depth_per_pass,
                                             wall_thickness):
                # Full width: the top wall spans the whole tube, and nothing has proven
                # it is gone yet at mid-tube.
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - full width )')

                # Position at start
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X, so this rapid
                # descends alongside the tube, not into it.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Arc clearing pattern across tube width
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, cross the (now proven open) middle
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Arc clearing through back wall only
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > back_wall_inner_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Finish back wall
                if current_x > back_wall_inner_x:
                    gcode.append(f'G1 X{back_wall_inner_x:.4f}')

                # Retract, rapid across the cleared middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Feed back down inside the tube. The earlier passes cleared this
                # column, but a saw-cut end can leave stock proud by more than the
                # margin, so this descent is a controlled feed, not a rapid.
                gcode.append(f'G1 Z{z_cut:.4f} F{self.plunge_rate:.1f}')

                # Arc clearing through front wall
                gcode.append(f'G1 F{arc_feed}')
                current_x = front_wall_inner_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')

        gcode.append(f'( Roughing complete: {num_roughing_passes} passes )')

        # === FINISHING PASSES ===
        stepover = finishing_tool_edge - roughing_tool_edge
        gcode.append('( === FINISHING PASSES === )')
        gcode.append(f'( {num_finishing_passes} depth passes, stepover {stepover:.4f}" )')

        for pass_num in range(num_finishing_passes):
            z_cut = z_top - (pass_num + 1) * finishing_depth_per_pass

            if not self._tube_middle_is_open(pass_num, finishing_depth_per_pass,
                                             wall_thickness):
                # Full width: nothing has proven the top wall is gone at mid-tube yet.
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - full width )')

                # Position for finishing
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Single horizontal cut across
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, cross the (now proven open) middle
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Cut through back wall only
                gcode.append(f'G1 X{back_wall_inner_x:.4f} F{self.feed_rate}')

                # Retract, rapid across the cleared middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Feed back down inside the tube, not a rapid: proud saw-cut stock
                # can sit below the cleared floor by more than the margin allows.
                gcode.append(f'G1 Z{z_cut:.4f} F{self.plunge_rate:.1f}')

                # Cut through front wall
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')

        return gcode

    def _generate_tube_facing_toolpath(self, tube_width: float, tube_height: float,
                                       tool_radius: float, stepover: float,
                                       stepdown: float, facing_depth: float,
                                       finish_allowance: float, phase: int = 1) -> list[str]:
        """
        Generate complete tube facing toolpath using parametric side-entry approach.

        This method generates toolpaths from scratch for any tube size using
        side-entry (plunge outside tube, arc into material) and contour clearing.
        The approach allows for 0.55" deep facing in a single pass per Z level.

        Args:
            tube_width: Width of tube (X dimension) in inches
            tube_height: Height of tube (Z dimension) in inches
            tool_radius: Unused (calculated internally)
            stepover: Unused (uses stepover_percentage)
            stepdown: Unused (single pass per Z level)
            facing_depth: Unused (hardcoded to 0.55")
            finish_allowance: Unused
            phase: 1 for first end (with stepover), 2 for second end (no stepover)

        Returns:
            List of G-code lines for the facing operation
        """
        return self._generate_parametric_tube_facing(tube_width, tube_height, phase)

    def generate_tube_facing_gcode(self, tube_size: str = '1x1', suggested_filename: str = None, timestamp: str = None) -> PostProcessorResult:
        """
        Generate G-code for tube facing operation with parameterized tube dimensions.

        Strategy:
        - Roughing passes: Zigzag pocketing at multiple Z depths with helical ramping
        - Finishing pass: Profile around tube perimeter with proper lead-in/lead-out
        - Phase 1: Face first half (Y=-0.125 to Y=+0.125)
        - Pause for flip (M0)
        - Phase 2: Face second half (Y=-0.25 to Y=0)

        Args:
            tube_size: Size of tube ('1x1', '2x1-standing', '2x1-flat')
            suggested_filename: Optional filename (without timestamp, will be added)

        Returns:
            PostProcessorResult with gcode string and stats
        """
        try:
            self.validate_aluminum_cutting_parameters()
        except ValueError as exc:
            return PostProcessorResult(success=False, errors=[str(exc)])
        self._force_board_datum_for_tube()

        # Parse tube dimensions
        tube_width, tube_height = self._parse_tube_size(tube_size)

        # Calculate toolpath parameters
        tool_radius = self.tool_diameter / 2.0
        stepover = self.tool_diameter * 0.4  # 40% stepover for roughing
        stepdown = 0.05  # Conservative Z stepdown
        facing_depth = 0.25  # How much material to remove
        finish_allowance = 0.01  # Leave this much for finish pass

        # Generate separate toolpaths for each phase
        # Phase 1: Roughing and finishing at different Y depths (stepover)
        # Phase 2: Roughing and finishing at same Y depth (no stepover)
        phase1_toolpath = self._generate_tube_facing_toolpath(
            tube_width, tube_height, tool_radius, stepover,
            stepdown, facing_depth, finish_allowance, phase=1
        )
        phase2_toolpath = self._generate_tube_facing_toolpath(
            tube_width, tube_height, tool_radius, stepover,
            stepdown, facing_depth, finish_allowance, phase=2
        )

        # Tool edge positions are now directly specified in the toolpath generation
        # Phase 1: Roughing at +0.05", Finishing at +0.0625"
        # Phase 2: Roughing at -0.0125", Finishing at 0"
        # No Y offset needed - positions are absolute
        pass1_y_offset = 0
        pass2_y_offset = 0

        gcode = []

        # Use provided timestamp (from client's timezone) or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Format for G-code header (just date and time, no seconds)
        # Client-supplied; truncating to YYYY-MM-DD HH:MM is not sanitization.
        timestamp_display = sanitize_comment(timestamp[:16], 'unknown')

        # === HEADER ===
        gcode.append('( PENGUINCAM TUBE FACING OPERATION )')
        gcode.append(f'( Generated: {timestamp_display} )')
        gcode.append(f'( Tube size: {tube_size} )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" {self.tool_flutes}-flute end mill )')
        gcode.append('( )')
        gcode.extend(self._dry_run_banner())
        gcode.append('( SETUP INSTRUCTIONS: )')
        gcode.append('( 1. Mount tube in jig with end facing user )')
        gcode.append(self._tube_wcs_setup_comment())
        gcode.append('( 3. Z=0 is at bottom of tube, jig surface )')
        gcode.append('( 4. Y=0 is at nominal end face of tube )')
        gcode.append('( )')

        # === INITIALIZATION ===
        gcode.append('')
        gcode.append('( === INITIALIZATION === )')
        gcode.append('G90 G94 G91.1 G40 G49 G17')
        gcode.append('G92.1  ; Cancel any temporary coordinate offset')
        gcode.append('G20')
        gcode.append('G90  ; Absolute positioning mode')
        gcode.append('')
        facing_reach = self._calculate_tube_operation_passes(tube_height)['total_depth']
        gcode.extend(self._aluminum_preflight_gcode(tube_reach=facing_reach))
        gcode.append('( Spindle )')
        gcode.extend(self._spindle_start_gcode())
        tube_coolant_on = None if self.is_dry_run else self._coolant_on_gcode()
        if tube_coolant_on:
            gcode.append(tube_coolant_on)
        gcode.append('')
        gcode.append(self._tube_wcs_activate_gcode())
        gcode.append('')

        # Safe height above the tube, in work coordinates (portable - no G53).
        tube_safe_z = tube_height + self.dry_run_lift + 0.25

        # === PHASE 1: FACE FIRST HALF ===
        gcode.append('( === PHASE 1: FACE FIRST HALF === )')
        gcode.append('( Face from Y=-0.125 to Y=+0.125 )')
        gcode.append('')
        gcode.append(f'G0 Z{tube_safe_z:.4f}  ; Safe Z clearance')
        gcode.append('G0 X0 Y0  ; Rapid to work origin')
        gcode.append('')

        # Add Phase 1 toolpath with Pass 1 Y offset
        for line in phase1_toolpath:
            line = line.strip()
            if line and not line.startswith('G52'):
                adjusted_line = self._adjust_y_coordinate(line, pass1_y_offset)
                gcode.append(adjusted_line)

        # === PAUSE FOR FLIP ===
        gcode.extend(self._generate_pause_and_park_gcode(
            'PAUSE FOR TUBE FLIP',
            [
                'Flip tube 180 degrees end-for-end',
                'Re-clamp tube in jig'
            ],
            safe_z=tube_safe_z  # must clear the full tube, not just the wall
        ))

        # === PHASE 2: FACE SECOND HALF ===
        gcode.append('( === PHASE 2: FACE SECOND HALF === )')
        gcode.append('( Face from Y=-0.250 to Y=-0.125 )')
        gcode.append('')
        gcode.append(f'G0 Z{tube_safe_z:.4f}  ; Safe Z clearance')
        gcode.append('G0 X0 Y0  ; Rapid to work origin')
        gcode.append('')

        # Add Phase 2 toolpath with Pass 2 Y offset (no stepover - same Y for roughing/finishing)
        for line in phase2_toolpath:
            line = line.strip()
            if line and not line.startswith('G52'):
                adjusted_line = self._adjust_y_coordinate(line, pass2_y_offset)
                gcode.append(adjusted_line)

        # === END ===
        gcode.append('')
        gcode.append('( === PROGRAM END === )')
        gcode.append(f'G0 Z{tube_safe_z:.4f}  ; Safe Z clearance')
        tube_coolant_off = self._coolant_off_gcode()
        if tube_coolant_off:
            gcode.append(tube_coolant_off)
        gcode.append('M5')
        wcs_reset = self._tube_wcs_reset_gcode()
        if wcs_reset:
            gcode.append(wcs_reset)
        gcode.extend(self._park_gcode('Park for part access'))  # G53 park only if configured
        gcode.append('M30')

        # Estimate cycle time
        time_estimate = self._estimate_cycle_time(gcode)

        # Generate filename with timestamp (name sanitized for safe disk write + download)
        filename = build_output_filename(suggested_filename, timestamp, "tube_facing", dry_run=self.is_dry_run)

        # Return result
        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=[],
            stats={
                'operation': 'tube_facing',
                'tube_size': tube_size,
                'tube_width': tube_width,
                'tube_height': tube_height,
                'num_holes': 0,
                'num_pockets': 0,
                'has_perimeter': False,
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell']),
                'setup_instructions': [
                    'Mount tube in jig with end facing spindle',
                    self._tube_wcs_setup_instruction(),
                    'Z=0 is at bottom of tube (jig surface)',
                    'Y=0 is at nominal end face of tube'
                ],
                'operation_notes': [
                    'Phase 1: Face first half of tube end',
                    'Program pauses (M0) for tube flip',
                    'Phase 2: Face second half of tube end'
                ]
            }
        )

    def generate_tube_pattern_gcode(self, tube_height: float,
                                   square_end: bool, cut_to_length: bool,
                                   tube_width: float = None, tube_length: float = None,
                                   suggested_filename: str = None, timestamp: str = None,
                                   second_face_pp: 'FRCPostProcessor' = None) -> PostProcessorResult:
        """
        Generate G-code for machining DXF pattern(s) on both faces of a tube.

        The tube sits in a jig with the end facing the spindle. This method:
        1. Optionally squares the tube end (if square_end=True)
        2. Machines the first face's DXF pattern
        3. Pauses (M0) for the operator to flip the tube 180° around Y-axis
        4. Machines the second face (X-mirrored to account for the physical flip)
        5. Optionally machines tube to length (if cut_to_length=True - stub)

        The second face's pattern is either:
        - the same pattern as face 1 mirrored onto the opposite side (one-face mode,
          second_face_pp is None), or
        - a distinct pattern (two-face mode) when second_face_pp is provided — its
          geometry is X-mirrored the same way to land correctly on the flipped tube.

        The jig uses the configured tube work coordinate system (default G54; an alternate
        fixed WCS such as G55 is opt-in via config.tube_work_coordinate_system) with:
        - Origin at bottom-left corner of tube face
        - X-axis along tube width
        - Y-axis pointing away from spindle (into tube)
        - Z-axis along tube height (vertical)

        Args:
            tube_height: Height of tube in Z direction (inches)
            square_end: Whether to square the tube end before machining pattern
            cut_to_length: Whether to cut tube to length after pattern (stub)
            tube_width: Width of tube face (X dimension) in inches (optional, calculated from DXF if not provided)
            tube_length: Length of tube face (Y dimension) in inches (optional, for future use)
            suggested_filename: Optional filename (without timestamp, will be added)
            second_face_pp: Optional processor already loaded with a distinct second-face
                pattern. When None, face 2 = face 1 mirrored (one-face mode).

        Returns:
            PostProcessorResult with gcode string and stats
        """
        self._force_board_datum_for_tube()
        # The second face is machined from a processor of its own, built by the caller -
        # so forcing the datum on `self` alone left face 2 on whatever the team config
        # said. Its toolpath is lifted into place by (tube_height - wall thickness),
        # arithmetic that only holds from the board datum, so a stock-top setting put
        # every face-2 feature a full wall thickness below the wall: pockets cut in the
        # tube cavity, holes opened in air.
        if second_face_pp is not None:
            second_face_pp._force_board_datum_for_tube()

        # Squaring the end and cutting to length are MILLING operations: they feed the
        # tool sideways, full width, a quarter inch deep. A drilled hole pattern has a
        # twist drill in the spindle and there is no tool change in this program, so the
        # combination fed a 0.201 in drill laterally through the wall 316 times. A drill
        # has no peripheral cutting edge and no radial rigidity; it snaps.
        if getattr(self, 'tube_pattern_mode', None) == 'holes' and (square_end or cut_to_length):
            wanted = []
            if square_end:
                wanted.append('squaring the end')
            if cut_to_length:
                wanted.append('cutting to length')
            return PostProcessorResult(success=False, errors=[
                f"Cannot combine a drilled hole pattern with {' and '.join(wanted)}: "
                f"those are milling operations and this program has a "
                f"{self.tool_diameter:.3f} in twist drill loaded, with no tool change. "
                f"Run the facing as a separate tube-facing job with an end mill."])

        # A generated pattern never goes through transform_coordinates, which is where a
        # DXF part gets checked against the machine. So the check was simply absent for
        # the path most likely to need it: a tube length is typed, not measured, and the
        # advertised 24" tube is longer than the Y travel of the machine this was written
        # for. The program ran off the end and the operator found out at the soft limit.
        if getattr(self, 'tube_pattern_mode', None) is not None:
            span_x = tube_width or 0.0
            span_y = tube_length or 0.0
            x_max = self.config.machine_x_max
            y_max = self.config.machine_y_max
            if span_x > x_max or span_y > y_max:
                return PostProcessorResult(success=False, errors=[
                    f'A {span_x:.2f}" x {span_y:.2f}" tube does not fit the machine '
                    f'({x_max:.1f}" x {y_max:.1f}" of travel). Machine it in shorter '
                    f'sections, or use a longer-travel machine.'])

        # Cutting to length needs to know the length. Without this the cut plane was
        # computed as `None + offset` and the job died with a TypeError.
        if cut_to_length and not tube_length:
            return PostProcessorResult(success=False, errors=[
                'Cutting to length needs a tube length; none was given or it could not '
                'be measured from the drawing.'])

        try:
            self.validate_aluminum_cutting_parameters()
            if second_face_pp is not None:
                second_face_pp.validate_aluminum_cutting_parameters()
        except ValueError as exc:
            return PostProcessorResult(success=False, errors=[str(exc)])

        # Check for validation errors first (both faces, in two-face mode).
        combined_errors = list(self.errors)
        if second_face_pp is not None:
            combined_errors.extend(second_face_pp.errors)
        if combined_errors:
            print(f"\n❌ Cannot generate G-code: {len(combined_errors)} validation error(s) found")
            for error in combined_errors:
                print(f"   - {error}")
            return PostProcessorResult(
                success=False,
                errors=combined_errors
            )

        two_face = second_face_pp is not None

        gcode = []

        # Use provided timestamp (from client's timezone) or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Format for G-code header (just date and time, no seconds)
        # Client-supplied; truncating to YYYY-MM-DD HH:MM is not sanitization.
        timestamp_display = sanitize_comment(timestamp[:16], 'unknown')

        # === HEADER ===
        gcode.append('( PENGUINCAM TUBE PATTERN OPERATION )')
        gcode.append(f'( Generated: {timestamp_display} )')
        if hasattr(self, 'user_name') and self.user_name:
            # A Google/Onshape display name reads like "Trent Fox (Mentor) Jose" - a
            # nested paren and a non-ASCII byte, both forbidden. The plate header has
            # sanitized this for a while; the tube header had not.
            gcode.append(f"( User: {sanitize_comment(self.user_name, 'unknown')} )")
        gcode.append(f'( Tube height: {tube_height:.3f}" )')
        # A drilled hole pattern runs a twist drill, not an end mill, and the header is
        # what the operator reads before loading a tool.
        _tool_kind = ('twist drill' if getattr(self, 'tube_pattern_mode', None) == 'holes'
                      else 'end mill')
        flute_text = ('' if _tool_kind == 'twist drill'
                      else f' {self.tool_flutes}-flute')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}"{flute_text} {_tool_kind} )')
        if getattr(self, 'tube_pattern_mode', None) == 'custom':
            # A custom face is whatever the operator drew, so the header has to say what
            # is in it - "tube pattern" alone tells them nothing about this program.
            _nh, _np = len(self.holes), len(self.pockets)
            gcode.append(f'( Custom design: {_nh} hole{"" if _nh == 1 else "s"}, '
                         f'{_np} pocket{"" if _np == 1 else "s"} )')
        gcode.append(f'( Material: {self.spindle_speed} RPM, {self.feed_rate:.1f} ipm )')
        if two_face:
            gcode.append('( Two-face mode: distinct pattern machined on each side )')
        else:
            gcode.append('( One-face mode: face 1 pattern mirrored onto opposite side )')
        gcode.append('( )')
        gcode.extend(self._dry_run_banner())
        gcode.append('( SETUP INSTRUCTIONS: )')
        gcode.append('( 1. Mount tube in jig with end facing spindle )')
        if self.tube_wcs == 'G54':
            gcode.append('( 2. Zero G54 to the tube for this job )')
        else:
            gcode.append(f'( 2. Jig uses fixed {self.tube_wcs} work coordinate system )')
        gcode.append(f'( 3. {self.tube_wcs} origin is at bottom-left corner of tube face )')
        gcode.append('( 4. X = tube width, Y = into tube, Z = tube height )')
        gcode.append('( )')

        # === INITIALIZATION ===
        gcode.append('')
        gcode.append('( === INITIALIZATION === )')
        gcode.append('G90 G94 G91.1 G40 G49 G17')
        gcode.append('G92.1  ; Cancel any temporary coordinate offset')
        gcode.append('G20')
        gcode.append('G90  ; Absolute positioning mode')
        gcode.append('')
        side_reach = (self._calculate_tube_operation_passes(tube_height)['total_depth']
                      if square_end or cut_to_length else None)
        gcode.extend(self._aluminum_preflight_gcode(tube_reach=side_reach))
        gcode.append('( Spindle )')
        gcode.extend(self._spindle_start_gcode())
        pattern_coolant_on = None if self.is_dry_run else self._coolant_on_gcode()
        if pattern_coolant_on:
            gcode.append(pattern_coolant_on)
        gcode.append('')
        gcode.append(self._tube_wcs_activate_gcode())
        gcode.append('')

        # Safe height above the tube, in work coordinates (portable - no G53).
        tube_safe_z = tube_height + self.dry_run_lift + 0.25

        # Retract BEFORE the first lateral move. Without this the program went straight
        # from the WCS line to `G0 X.. Y..`, so the first rapid ran at whatever height the
        # machine happened to be left at - and a tool sitting below the top of the tube
        # would be dragged across it. Every later move is already covered, because each
        # feature ends by retracting.
        gcode.append(f'G0 Z{tube_safe_z:.4f}  ; Retract to safe height before any XY move')
        gcode.append('')

        # Determine tube width for facing operations
        if tube_width is None:
            # Measured from the geometry, ALWAYS - not only when squaring. Face 2 is
            # mirrored with X_new = tube_width - X_old, so a guessed width does not make
            # the mirror approximate, it puts every face-2 feature at the wrong X. With
            # the old default of 1.0 a 2" tube machined face 2 up to 1" off the near
            # edge - into the jig.
            tube_width = None
            if True:
                all_x_coords = []
                if hasattr(self, 'holes'):
                    for hole in self.holes:
                        all_x_coords.append(hole['center'][0])
                if hasattr(self, 'pockets'):
                    for pocket in self.pockets:
                        all_x_coords.extend([p[0] for p in pocket])
                if hasattr(self, 'perimeter') and self.perimeter:
                    all_x_coords.extend([p[0] for p in self.perimeter])

                if all_x_coords:
                    calculated_width = max(all_x_coords) - min(all_x_coords)
                    if calculated_width > 0.1:  # Only use if reasonable
                        tube_width = calculated_width
            if tube_width is None:
                return PostProcessorResult(success=False, errors=[
                    'Could not measure the tube width from the drawing, and face 2 is '
                    'machined by mirroring about it. Pass the tube width explicitly '
                    '(--tube-width) rather than have every face-2 feature land at the '
                    'wrong X.'])

        # === PHASE 1: FIRST FACE (SQUARE + MACHINE PATTERN) ===
        gcode.append('( === PHASE 1: FIRST FACE === )')
        gcode.append('')

        # Square the end first (if requested)
        if square_end:
            gcode.append('( Square tube end )')
            tool_radius = self.tool_diameter / 2.0
            stepover = self.tool_diameter * 0.4
            stepdown = 0.05
            facing_depth = 0.25
            finish_allowance = 0.01

            facing_toolpath = self._generate_tube_facing_toolpath(
                tube_width, tube_height, tool_radius, stepover,
                stepdown, facing_depth, finish_allowance
            )

            # Facing toolpath Y coordinates are already absolute (calculated in _generate_parametric_tube_facing)
            # No additional offset needed - the face positions are set by roughing_tool_edge/finishing_tool_edge
            for line in facing_toolpath:
                gcode.append(line)
            gcode.append('')

        # Machine the pattern on this face
        gcode.append('( Machine pattern on first face )')
        gcode.append('( Machining holes and pockets only - perimeter is tube face )')
        z_offset = tube_height - self.material_thickness
        gcode.append(f'( Z offset: {z_offset:+.3f}", tube_height minus wall_thickness )')
        # Y offset for first face: matches facing offset so holes align with face
        y_offset_first_face = self.tube_facing_offset if square_end else 0.0
        gcode.append(f'( Y offset: +{y_offset_first_face:.3f}", rough end will be milled back )')
        gcode.append('')
        gcode.extend(self._generate_toolpath_gcode(skip_perimeter=True, z_offset=z_offset, y_offset=y_offset_first_face))

        # === CUT TO LENGTH - PHASE 1 ===
        if cut_to_length:
            gcode.append('')
            gcode.append('( === CUT TUBE TO LENGTH - PHASE 1 === )')
            cut_gcode = self._generate_cut_to_length(tube_width, tube_height, tube_length, phase=1, square_end=square_end)
            gcode.extend(cut_gcode)

        # === PAUSE FOR FLIP ===
        gcode.extend(self._generate_pause_and_park_gcode(
            'PAUSE FOR TUBE FLIP',
            [
                'Flip tube 180 degrees around Y-axis',
                'Holes will be machined on opposite face'
            ],
            safe_z=tube_safe_z  # must clear the full tube, not just the wall
        ))

        # === PHASE 2: SECOND FACE (SQUARE + MACHINE PATTERN) ===
        gcode.append('( === PHASE 2: SECOND FACE === )')
        gcode.append('')

        # Square the end first (if requested)
        if square_end:
            gcode.append('( Square tube end )')
            tool_radius = self.tool_diameter / 2.0
            stepover = self.tool_diameter * 0.4
            stepdown = 0.05
            facing_depth = 0.25
            finish_allowance = 0.01

            facing_toolpath = self._generate_tube_facing_toolpath(
                tube_width, tube_height, tool_radius, stepover,
                stepdown, facing_depth, finish_allowance, phase=2
            )

            # Facing toolpath Y coordinates are already absolute (calculated in _generate_parametric_tube_facing)
            # No additional offset needed - the face positions are set by roughing_tool_edge/finishing_tool_edge
            for line in facing_toolpath:
                gcode.append(line)
            gcode.append('')

        # Machine the pattern on this face (X-mirrored, Y offset for facing alignment).
        # In two-face mode the second face has its own distinct pattern; otherwise it is
        # face 1's pattern mirrored onto the opposite side.
        if two_face:
            gcode.append('( Machine second face pattern - X-mirrored )')
            gcode.append('( Distinct second-face pattern, X-mirrored, tube flipped end-for-end )')
        else:
            gcode.append('( Machine pattern on second face - X-mirrored )')
            gcode.append('( Pattern is X-mirrored, tube flipped end-for-end, so holes align opposite )')
        z_offset = tube_height - self.material_thickness
        gcode.append(f'( Z offset: {z_offset:+.3f}", tube_height minus wall_thickness )')
        # Y offset: 0 for Phase 2 - work zero is re-established after flip, face is at Y=0"
        y_offset_phase2 = 0.0
        gcode.append(f'( Y offset: {y_offset_phase2:.4f}", face at Y=0, no offset needed )')
        gcode.append('')

        # Mirror X coordinates around tube centerline (tube flipped end-for-end). The
        # geometry source is the second face's processor in two-face mode, else self.
        face2_source = second_face_pp if two_face else self
        mirrored_toolpath = face2_source._generate_toolpath_gcode_mirrored_x(
            z_offset=z_offset, tube_width=tube_width, y_offset=y_offset_phase2
        )
        gcode.extend(mirrored_toolpath)

        # === CUT TO LENGTH - PHASE 2 ===
        if cut_to_length:
            gcode.append('')
            gcode.append('( === CUT TUBE TO LENGTH - PHASE 2 === )')
            cut_gcode = self._generate_cut_to_length(tube_width, tube_height, tube_length, phase=2, square_end=square_end)
            gcode.extend(cut_gcode)

        # === END ===
        gcode.append('')
        gcode.append('( === PROGRAM END === )')
        gcode.append(f'G0 Z{tube_safe_z:.4f}  ; Safe Z clearance')
        pattern_coolant_off = self._coolant_off_gcode()
        if pattern_coolant_off:
            gcode.append(pattern_coolant_off)
        gcode.append('M5')
        wcs_reset = self._tube_wcs_reset_gcode()
        if wcs_reset:
            gcode.append(wcs_reset)
        gcode.extend(self._park_gcode('Park for part access'))  # G53 park only if configured
        gcode.append('M30')

        # Estimate cycle time
        time_estimate = self._estimate_cycle_time(gcode)

        # Collect stats. In one-face mode both sides share face 1's counts; in two-face
        # mode each side has its own, so sum them for the total.
        face1_holes = len(self.holes) if hasattr(self, 'holes') else 0
        face1_pockets = len(self.pockets) if hasattr(self, 'pockets') else 0
        if two_face:
            face2_holes = len(second_face_pp.holes) if hasattr(second_face_pp, 'holes') else 0
            face2_pockets = len(second_face_pp.pockets) if hasattr(second_face_pp, 'pockets') else 0
        else:
            face2_holes, face2_pockets = face1_holes, face1_pockets
        num_holes = face1_holes + face2_holes
        num_pockets = face1_pockets + face2_pockets

        # Generate filename with timestamp (name sanitized for safe disk write + download)
        filename = build_output_filename(suggested_filename, timestamp, "tube_pattern", dry_run=self.is_dry_run)

        # Build operation notes based on configuration
        phase2_note = ('Phase 2: Machine distinct pattern on opposite face'
                       if two_face else
                       'Phase 2: Machine pattern on opposite face (mirrored)')
        operation_notes = []
        if square_end:
            operation_notes.extend([
                'Phase 0: Square tube end',
                'Flip tube end-for-end (M0)',
                'Phase 0: Square opposite end',
                'Phase 1: Machine pattern on first face',
                'Flip tube 180° around Y-axis (M0)',
                phase2_note
            ])
        else:
            operation_notes.extend([
                'Phase 1: Machine pattern on first face',
                'Flip tube 180° around Y-axis (M0)',
                phase2_note
            ])
        if cut_to_length:
            operation_notes.append(f'Cut to length: Y={tube_length}" (each phase)')

        # Return result
        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=[],
            stats={
                'operation': 'tube_pattern',
                'tube_height': tube_height,
                'tube_width': tube_width,
                'tube_length': tube_length,
                'square_end': square_end,
                'cut_to_length': cut_to_length,
                'num_holes': num_holes,
                'num_pockets': num_pockets,
                'num_holes_per_face': face1_holes,
                'num_pockets_per_face': face1_pockets,
                'two_face': two_face,
                'has_perimeter': False,
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell']),
                'setup_instructions': [
                    'Mount tube in jig with end facing spindle',
                    self._tube_wcs_setup_instruction(),
                    'Origin (0,0,0) = bottom-left corner of tube face'
                ],
                'operation_notes': operation_notes
            }
        )

    def _generate_toolpath_gcode(self, skip_perimeter: bool = False, z_offset: float = 0.0, y_offset: float = 0.0) -> list[str]:
        """
        Generate toolpath G-code for the current DXF geometry.

        Args:
            skip_perimeter: If True, skip perimeter cutting (useful for tube faces)
            z_offset: Offset to add to all Z coordinates (for tube mode, shifts to tube face height)
            y_offset: Offset to add to all Y coordinates (for tube first face, accounts for material removal)
        """
        toolpath = []

        # Generate toolpaths for holes
        if hasattr(self, 'holes') and self.holes:
            for hole in self.holes:
                emit = lambda hole=hole: self._generate_hole_gcode(
                    hole['center'][0], hole['center'][1], hole['diameter'],
                    needs_peck_drill=hole.get('needs_peck_drill', False))
                toolpath.extend(emit() if getattr(self, 'tool_has_drill_point', False)
                                else self._clear_in_depth_levels(emit))

        # Generate toolpaths for pockets
        if hasattr(self, 'pockets') and self.pockets:
            for pocket in self.pockets:
                toolpath.extend(self._clear_in_depth_levels(
                    lambda pocket=pocket: self._generate_pocket_gcode(pocket)))

        # Perimeter (only for standard mode, not tube faces)
        if not skip_perimeter and hasattr(self, 'perimeter') and self.perimeter:
            toolpath.extend(self._generate_perimeter_gcode(self.perimeter))

        # Apply offsets if needed (for tube mode)
        if z_offset != 0.0:
            toolpath = [self._offset_z_coordinate(line, z_offset) for line in toolpath]
        if y_offset != 0.0:
            toolpath = [self._offset_y_coordinate(line, y_offset) for line in toolpath]

        return toolpath

    def _generate_toolpath_gcode_mirrored_x(self, z_offset: float = 0.0, tube_width: float = 1.0,
                                            y_offset: float = 0.0) -> list[str]:
        """
        Generate toolpath G-code for mirrored features (second tube face).

        This is used for the second face of tube machining after flipping end-for-end.
        Instead of transforming generated toolpaths (which breaks safety logic),
        we mirror the feature geometry FIRST, then generate fresh toolpaths.

        This preserves all safety features:
        - Helical entry at center
        - Outward Archimedean spiral (gradual material removal)
        - Proper climb milling direction

        When flipping a tube 180° around Y-axis (end-for-end):
        - Feature X coordinates mirror around centerline: X_new = tube_width - X_old
        - Feature Y coordinates get offset to account for facing: Y_new = Y_old + y_offset
        - Toolpaths are regenerated from mirrored geometry

        Args:
            z_offset: Offset to add to all Z coordinates (for tube mode)
            tube_width: Width of tube face for mirroring X around centerline
            y_offset: Offset to add to all Y coordinates (for tube facing alignment)
        """
        toolpath = []

        # Generate toolpaths for mirrored holes
        if hasattr(self, 'holes') and self.holes:
            for hole in self.holes:
                # Mirror the hole center around tube centerline
                original_cx = hole['center'][0]
                original_cy = hole['center'][1]
                mirrored_cx = tube_width - original_cx
                mirrored_cy = original_cy + y_offset  # Apply Y offset for facing alignment

                # Generate fresh toolpath for the mirrored hole
                # This preserves helical entry + outward spiral safety
                emit = lambda hole=hole, x=mirrored_cx, y=mirrored_cy: self._generate_hole_gcode(
                    x, y, hole['diameter'],
                    needs_peck_drill=hole.get('needs_peck_drill', False))
                toolpath.extend(emit() if getattr(self, 'tool_has_drill_point', False)
                                else self._clear_in_depth_levels(emit))

        # Generate toolpaths for mirrored pockets
        if hasattr(self, 'pockets') and self.pockets:
            for pocket in self.pockets:
                # Mirror all pocket points around tube centerline and apply Y offset
                mirrored_pocket = [(tube_width - x, y + y_offset) for x, y in pocket]
                toolpath.extend(self._clear_in_depth_levels(
                    lambda pocket=mirrored_pocket: self._generate_pocket_gcode(pocket)))

        # Perimeter is not machined on tube faces (skip)

        # Apply Z offset if needed (for tube mode)
        if z_offset != 0.0:
            toolpath = [self._offset_z_coordinate(line, z_offset) for line in toolpath]

        return toolpath

    def _offset_z_coordinate(self, line: str, z_offset: float) -> str:
        """
        Offset Z coordinate in a G-code line by adding z_offset.

        For standard mode: Z=0 at bottom of plate, toolpath cuts from Z=thickness (top) to Z=0 (bottom).
        For tube mode: Z=0 at bottom of lower face. Upper face bottom is at Z=tube_height-tube_wall_thickness.
        This method shifts Z coordinates by (tube_height - tube_wall_thickness) to position at upper face.

        Legacy method - wraps generic _offset_coordinate() for backwards compatibility.
        """
        return self._offset_coordinate(line, 'Z', z_offset)

    def _offset_y_coordinate(self, line: str, y_offset: float) -> str:
        """
        Offset Y coordinate in a G-code line by adding y_offset.

        For tube mode first face: Y offset accounts for material that will be removed during facing.
        If rough end will be milled back by 0.125", pattern must be positioned 0.125" deeper.

        Legacy method - wraps generic _offset_coordinate() for backwards compatibility.
        """
        return self._offset_coordinate(line, 'Y', y_offset)

    def _generate_cut_to_length(self, tube_width: float, tube_height: float,
                                 tube_length: float, phase: int, square_end: bool) -> list[str]:
        """
        Generate G-code to cut tube to length using arc clearing pattern.

        Uses the same technique as tube facing:
        - Arc clearing pattern for roughing (reduces chip load)
        - Straight finishing pass
        - Single plunge to just over half tube height
        - 1.5x tool diameter clearance outside tube
        - Phase-specific Y offsets for alignment

        Coordinate system:
        - X: across tube width (cut direction)
        - Z: tube height (plunge direction, vertical)
        - Y: along tube length (cut position)

        Args:
            tube_width: Width of tube (X dimension)
            tube_height: Height of tube (Z dimension)
            tube_length: Desired tube length (Y dimension)
            phase: 1 (before flip) or 2 (after flip)
            square_end: Whether the tube end was squared (affects Phase 1 offset)

        Returns:
            List of G-code lines
        """
        gcode = []
        tool_radius = self.tool_diameter / 2.0

        # Calculate pass parameters using shared helper
        passes = self._calculate_tube_operation_passes(tube_height)
        total_depth = passes['total_depth']
        wall_thickness = passes['wall_thickness']
        num_roughing_passes = passes['num_roughing_passes']
        roughing_depth_per_pass = passes['roughing_depth_per_pass']
        num_finishing_passes = passes['num_finishing_passes']
        finishing_depth_per_pass = passes['finishing_depth_per_pass']

        # Y offset for cut position
        # Phase 1: Add tube_facing_offset ONLY if square_end=True (material removed from front)
        # Phase 2: No offset (coordinate system reset after flip)
        if phase == 1:
            # Phase 1: Cut at tube_length + optional facing offset + tool radius compensation
            if square_end:
                y_cut = tube_length + self.tube_facing_offset + self.tool_radius
            else:
                y_cut = tube_length + self.tool_radius
            z_start = tube_height  # Top of tube (tube sits on sacrifice board at Z=0)
            gcode.append(f'( Cut to length at Y={y_cut:.4f}", Phase 1: before flip )')
        else:
            # Phase 2: Cut at tube_length + tool radius compensation (no facing offset)
            y_cut = tube_length + self.tool_radius
            z_start = tube_height  # Top of tube
            gcode.append(f'( Cut to length at Y={y_cut:.4f}", Phase 2: after flip )')

        # For cut to length, the tool's -Y edge defines the kept part boundary
        # (opposite of tube facing where +Y edge defines the face)
        # Roughing leaves 0.0125" for finishing pass
        finish_stock = 0.0125  # Material left for finishing

        # Arc clearing parameters (same as tube facing)
        arc_advance = 0.04  # How far each arc advances in X
        arc_radius = 0.05  # Arc radius
        half_advance = arc_advance / 2
        j_offset = math.sqrt(arc_radius**2 - half_advance**2)

        # Tool CENTER positions for cut to length:
        # - The kept part is at Y < y_cut, waste is at Y > y_cut
        # - Tool's -Y edge (toward kept part) defines the cut boundary
        #
        # With positive J, G3 (CCW) arc goes through TOP of circle (max Y, into waste).
        # Arc center Y = roughing_y + j_offset
        # Top of circle Y = center_y + arc_radius = roughing_y + j_offset + arc_radius
        #
        # At arc CHORD (start/end): tool center Y = roughing_y, tool -Y edge = roughing_y - tool_radius
        # At arc PEAK (top of circle): tool center Y = roughing_y + j_offset + arc_radius (in waste)
        #
        # The CHORD is where tool -Y edge is closest to kept part (the limit for roughing).
        # For roughing to leave finish_stock, the -Y edge at chord should be at finish_stock from kept edge:
        #   roughing_y - tool_radius = (tube_length + finish_stock)
        #   roughing_y = tube_length + finish_stock + tool_radius
        # Since y_cut already equals tube_length + tool_radius:
        #   roughing_y = y_cut + finish_stock
        roughing_y = y_cut + finish_stock
        finishing_y = y_cut  # y_cut is already the tool center position

        # Calculate peak position for comments
        peak_y = roughing_y + j_offset + arc_radius  # Tool center at peak
        peak_minus_edge = peak_y - tool_radius  # Tool -Y edge at peak (in waste)

        # X positions (tool edge 0.05" from material edge)
        clearance = tool_radius + 0.05
        start_x = tube_width + clearance  # Far side
        end_x = -clearance  # Near side

        # Z positions
        # + dry_run_lift: in a dry run the whole tube frame rises with the plate
        # frame, so the tool traces the same path in the air above the jig.
        z_top = tube_height + self.dry_run_lift        # Top of tube
        z_safe = tube_height + self.dry_run_lift + 0.25  # Safe height above tube
        z_final = z_top - total_depth  # Final depth (just over half height)

        gcode.append(f'( Tube width: {tube_width:.2f}" x height: {tube_height:.2f}" )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" )')
        gcode.append(f'( Total depth: {total_depth:.3f}" )')
        gcode.append(f'( Roughing: {num_roughing_passes} passes of {roughing_depth_per_pass:.3f}" each, leaves {finish_stock:.4f}" for finishing )')
        gcode.append(f'( Finishing: {num_finishing_passes} passes of {finishing_depth_per_pass:.3f}" each, -Y edge at Y={y_cut:.4f}" )')
        gcode.append('')

        # === ROUGHING PASSES ===
        # Use arc clearing pattern to reduce chip load
        arc_feed = self.feed_rate  # Full feed rate

        gcode.append('( === ROUGHING PASSES === )')
        gcode.append(f'( {num_roughing_passes} depth passes with arc clearing )')

        # Calculate wall boundaries for subsequent passes (box tubing is hollow)
        # Back wall (far side): from start_x to inner edge
        back_wall_inner_x = tube_width - wall_thickness - clearance
        # Front wall (near side): from inner edge to end_x
        front_wall_inner_x = wall_thickness + clearance

        for pass_num in range(num_roughing_passes):
            z_cut = z_top - (pass_num + 1) * roughing_depth_per_pass

            if not self._tube_middle_is_open(pass_num, roughing_depth_per_pass,
                                             wall_thickness):
                # Full width: the top wall spans the whole tube and no pass has yet
                # proven it gone at mid-tube. Skipping the middle here would also leave
                # an uncut web that stops the tube separating.
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - full width )')

                # Position at start (combine X Y for cleaner G-code)
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Arc clearing pattern across tube width
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, cross the (now proven open) middle
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Arc clearing through back wall only
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > back_wall_inner_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Finish back wall
                if current_x > back_wall_inner_x:
                    gcode.append(f'G1 X{back_wall_inner_x:.4f}')

                # Retract, rapid across the cleared middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Feed back down inside the tube. Earlier passes cleared this column,
                # but a saw-cut end can leave stock proud past the margin, so the
                # descent is a controlled feed rather than a rapid.
                gcode.append(f'G1 Z{z_cut:.4f} F{self.plunge_rate:.1f}')

                # Arc clearing through front wall
                gcode.append(f'G1 F{arc_feed}')
                current_x = front_wall_inner_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')

        gcode.append(f'( Roughing complete: {num_roughing_passes} passes )')

        # === FINISHING PASSES ===
        gcode.append('( === FINISHING PASSES === )')
        gcode.append(f'( {num_finishing_passes} depth passes, removes {finish_stock:.4f}" )')

        for pass_num in range(num_finishing_passes):
            z_cut = z_top - (pass_num + 1) * finishing_depth_per_pass

            if not self._tube_middle_is_open(pass_num, finishing_depth_per_pass,
                                             wall_thickness):
                # Full width: nothing has proven the top wall is gone at mid-tube yet.
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - full width )')

                # Position for finishing
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Single horizontal cut across
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, cross the (now proven open) middle
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth. start_x is clear of the tube in X.
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge, off the tube in X

                # Cut through back wall only
                gcode.append(f'G1 X{back_wall_inner_x:.4f} F{self.feed_rate}')

                # Retract, rapid across the cleared middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Feed back down inside the tube, not a rapid: proud saw-cut stock can
                # sit below the cleared floor by more than the margin allows.
                gcode.append(f'G1 Z{z_cut:.4f} F{self.plunge_rate:.1f}')

                # Cut through front wall
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')

        return gcode


def validate_job_layout(parts, machine_x_max, machine_y_max, min_gap=0.0, stock=None):
    """Validate a multi-part job layout: it fits the machine, it fits the stock, and no
    two parts collide.

    Without a sheet the parts' combined bounding box IS the stock, so the only fit check
    is the machine. Given a sheet (`stock`), placements are absolute on that sheet and
    every part has to be ON it - the check the browser does, repeated here because the
    browser is not the only thing that can post a job, and a part hanging off the sheet
    is a cut into the spoilboard or the clamps.

    Args:
        parts: list of dicts, each with 'name' and 'bbox' = (minX, minY, maxX, maxY),
               and optionally 'polygon' (a placed Shapely polygon for real-geometry
               overlap testing).
        machine_x_max, machine_y_max: machine travel envelope (inches).
        min_gap: required clearance between parts (inches). Pass the tool diameter to
                 reject parts closer than one kerf. Defaults to 0 (touching allowed).
        stock: (width, height) of the sheet the parts are placed on, or None when the
               parts' own bounding box is the stock.

    Returns:
        List of error dicts: {'part_index': int|None, 'name': str|None, 'error': str}.
        Empty list means the layout is valid.
    """
    errors = []
    tol = 1e-6

    boxes = [p.get('bbox') for p in parts if p.get('bbox')]
    if stock:
        sheet_w, sheet_h = float(stock[0]), float(stock[1])
        if sheet_w > machine_x_max + tol or sheet_h > machine_y_max + tol:
            errors.append({
                'part_index': None, 'name': None,
                'error': (f"The stock ({sheet_w:.2f}\" x {sheet_h:.2f}\") exceeds the machine "
                          f"({machine_x_max:.1f}\" x {machine_y_max:.1f}\").")
            })
        # The cutter rides half a kerf OUTSIDE the outline on a profile pass, so a part
        # whose outline is flush with the sheet edge still cuts past it.
        pad = min_gap / 2.0
        for i, part in enumerate(parts):
            b = part.get('bbox')
            if b is None:
                continue
            if (b[0] - pad < -tol or b[1] - pad < -tol
                    or b[2] + pad > sheet_w + tol or b[3] + pad > sheet_h + tol):
                name = part.get('name', f'part {i + 1}')
                errors.append({
                    'part_index': i, 'name': name,
                    'error': (f"{name} and its cut path do not fit on the "
                              f"{sheet_w:.2f}\" x {sheet_h:.2f}\" stock.")
                })
    elif boxes:
        # The combined bounding box (the stock) must fit the machine.
        w = max(b[2] for b in boxes) - min(b[0] for b in boxes)
        h = max(b[3] for b in boxes) - min(b[1] for b in boxes)
        if w > machine_x_max + tol or h > machine_y_max + tol:
            errors.append({
                'part_index': None,
                'name': None,
                'error': (f"Parts ({w:.2f}\" x {h:.2f}\") exceed the machine "
                          f"({machine_x_max:.1f}\" x {machine_y_max:.1f}\").")
            })
    if boxes:
        # Absolute travel, not just size. Checking the bounding box's WIDTH was only
        # ever equivalent while placements were bbox-relative; with a sheet they are
        # absolute, so a part at X42 on a 48" sheet passed a 31" machine check.
        far_x = max(b[2] for b in boxes) + min_gap / 2.0
        far_y = max(b[3] for b in boxes) + min_gap / 2.0
        if far_x > machine_x_max + tol or far_y > machine_y_max + tol:
            errors.append({
                'part_index': None, 'name': None,
                'error': (f"The job reaches X{far_x:.2f}\" Y{far_y:.2f}\", past the machine's "
                          f"{machine_x_max:.1f}\" x {machine_y_max:.1f}\" travel.")
            })
    for i, part in enumerate(parts):
        if part.get('bbox') is None:
            errors.append({'part_index': i, 'name': part.get('name', f'part {i + 1}'),
                           'error': f"{part.get('name', 'part')}: no geometry to place."})

    # Parts must not overlap (or sit closer than min_gap). When a placed perimeter
    # polygon is supplied, test the real geometry (so a part can nest into another's
    # concave region even though their bounding boxes intersect). Otherwise fall back
    # to a bounding-box gap test.
    for i in range(len(parts)):
        bi = parts[i].get('bbox')
        if bi is None:
            continue
        for j in range(i + 1, len(parts)):
            bj = parts[j].get('bbox')
            if bj is None:
                continue

            # Cheap bbox prune: if the boxes themselves clear the gap, parts are clear.
            clear_x = (bi[2] + min_gap <= bj[0] + tol) or (bj[2] + min_gap <= bi[0] + tol)
            clear_y = (bi[3] + min_gap <= bj[1] + tol) or (bj[3] + min_gap <= bi[1] + tol)
            if clear_x or clear_y:
                continue

            pi = parts[i].get('polygon')
            pj = parts[j].get('polygon')
            too_close = True
            if pi is not None and pj is not None:
                try:
                    too_close = pi.distance(pj) < (min_gap - tol)
                except Exception:
                    too_close = True  # geometry error -> be conservative

            if too_close:
                ni = parts[i].get('name', f'part {i + 1}')
                nj = parts[j].get('name', f'part {j + 1}')
                gap_note = f" (need {min_gap:.3f}\" clearance)" if min_gap else ""
                errors.append({
                    'part_index': j,
                    'name': nj,
                    'error': f"{ni} and {nj} overlap or are too close{gap_note}."
                })

    return errors


def assemble_job_gcode(part_jobs, header_pp, timestamp=None, suggested_filename=None):
    """Stitch per-part G-code phases into one multi-part program, collated by phase.

    Rather than running each part to completion before the next, the whole job is
    ordered by phase across all parts: all interiors -> one shared refixturing pause
    (if configured) -> all perimeters -> all tab removals. This makes the
    "pause before perimeter" option sensible for a whole sheet (fixture every part
    once, between interiors and perimeters). A single-part job reduces to the normal
    single-part order.

    Args:
        part_jobs: ordered list of dicts:
            {'name': str, 'place_x': float, 'place_y': float, 'rotation': float,
             'interior': [str], 'perimeter': [str], 'tab_removal': [str]}
            -- the three phase line-lists from FRCPostProcessor.generate_part_phases().
        header_pp: an FRCPostProcessor carrying the shared job parameters (material,
            tool, thickness, spindle, park Z, pause_before_perimeter). Used to build the
            single header/footer, the shared pause, and estimate total cycle time.
            (v1: one tool/material per job.)
        timestamp, suggested_filename: as in generate_gcode.

    Returns:
        PostProcessorResult with the assembled program and aggregate stats.
    """
    if not timestamp:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # A job with a deburr / chamfer phase involves two tools, and the header must say so
    # before the operator starts the program with only the end mill to hand.
    has_chamfer = any(pj.get('chamfer') for pj in part_jobs)
    chamfer_tools = (header_pp._chamfer_pass_tool_table()
                     if has_chamfer and header_pp.chamfer_pass else None)

    gcode = header_pp._generate_gcode_header(timestamp, is_job=True,
                                             job_part_count=len(part_jobs),
                                             tool_table=chamfer_tools)

    def _part_label(i, pj):
        name = pj.get('name', f'part {i}')
        px = pj.get('place_x', 0.0)
        py = pj.get('place_y', 0.0)
        rot = pj.get('rotation', 0)
        # Sanitize the name for a comment (no nested parens or brackets, ASCII only).
        safe_name = sanitize_comment(name, f'part {i}')
        return f"(--- PART {i}: {safe_name} @ X{px:.4f} Y{py:.4f} ROT {rot:g} deg ---)"

    def _emit_phase(section_title, phase_key):
        """Append every part's body for one phase, each under its part label + safe Z.
        Returns True if any part contributed lines to this phase."""
        bodies = [(i, pj) for i, pj in enumerate(part_jobs, 1) if pj.get(phase_key)]
        if not bodies:
            return False
        gcode.append("")
        gcode.append(f"(===== {section_title} =====)")
        for i, pj in bodies:
            gcode.append("")
            gcode.append(_part_label(i, pj))
            gcode.append(f"G0 Z{header_pp._safe_z():.4f}  ; Safe Z between parts")
            gcode.extend(pj[phase_key])
        return True

    # Phase A: all parts' interior features.
    _emit_phase("PHASE: INTERIOR FEATURES", 'interior')

    # Phase B: one shared refixturing pause between interiors and perimeters, if
    # configured. Only meaningful when there are perimeters still to cut.
    has_perimeters = any(pj.get('perimeter') for pj in part_jobs)
    if header_pp.pause_before_perimeter and has_perimeters:
        gcode.extend(header_pp._generate_pause_and_park_gcode(
            'PAUSE FOR FIXTURING',
            [
                "All parts' internal features complete",
                'Install screws through holes into sacrifice board on ALL parts',
                'Fixture every part securely before perimeter cutting'
            ]
        ))

    # Phase C: all parts' perimeters (tab removal deferred to phase D).
    _emit_phase("PHASE: PERIMETERS", 'perimeter')

    # Phase C2: the optional deburr / chamfer pass - one shared V-bit change for the
    # whole sheet, then every part's edges. Every part is still tabbed to the stock
    # here (tab removal is phase D), which is what makes cutting after the profile
    # safe. When the machine removes tabs, one change back to the end mill follows.
    if has_chamfer and header_pp.chamfer_pass:
        gcode.extend(header_pp._chamfer_tool_change_gcode(to_vbit=True))
    _emit_phase("PHASE: DEBURR CHAMFER PASS", 'chamfer')
    if (has_chamfer and header_pp.chamfer_pass
            and any(pj.get('tab_removal') for pj in part_jobs)):
        gcode.extend(header_pp._chamfer_tool_change_gcode(to_vbit=False))

    # Phase D: all parts' tab removals (only parts whose perimeter left tabs).
    _emit_phase("PHASE: TAB REMOVAL", 'tab_removal')

    gcode.extend(header_pp._generate_gcode_footer())

    # Estimate total cycle time across the whole program and insert into the header. The job
    # header has no Helical/plunge lines after (Operations:), so the block lands at offset 1.
    time_estimate = header_pp._estimate_cycle_time(gcode)
    header_pp._insert_cycle_time_comment(gcode, time_estimate, offset=1)

    filename = build_output_filename(suggested_filename, timestamp, "job",
                                     dry_run=header_pp.is_dry_run)

    return PostProcessorResult(
        success=True,
        gcode='\n'.join(gcode),
        filename=filename,
        stats={
            'num_parts': len(part_jobs),
            'total_lines': len(gcode),
            'cycle_time_seconds': time_estimate['total'],
            'cycle_time_display': header_pp._format_time(time_estimate['total']),
            'cutting_time': header_pp._format_time(time_estimate['cutting']),
            'rapid_time': header_pp._format_time(time_estimate['rapid']),
            'dwell_time': header_pp._format_time(time_estimate['dwell'])
        }
    )


#: The edges a deburr / chamfer pass may break, in the order the UI lists them.
CHAMFER_TARGETS = ('perimeter', 'holes', 'pockets')


def parse_chamfer_spec(spec) -> dict:
    """Validate a deburr / chamfer pass request into the dict chamfer_pass holds.

    Accepts {'width', 'bit_diameter', 'bit_angle', 'targets'} with numbers (or numeric
    strings, as form fields arrive) in inches and degrees; targets may be a list or a
    comma-separated string and defaults to the perimeter. Raises ValueError naming the
    problem - these are numbers that reach Z arithmetic and erosion buffers directly,
    so nothing non-finite or non-physical gets through. Geometry-dependent refusals
    (wider than the bit can cut, through the stock, part too narrow) are judged later,
    against the actual part.
    """
    if not isinstance(spec, dict):
        raise ValueError('The chamfer pass must be an object with width, bit_diameter, '
                         'bit_angle and targets.')

    def _number(key, default=None):
        raw = spec.get(key, default)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise ValueError(f'The chamfer pass needs a {key}.')
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f'Chamfer {key} must be a number, got {raw!r}.')
        if not math.isfinite(value):
            raise ValueError(f'Chamfer {key} must be a finite number.')
        return value

    width = _number('width')
    bit_diameter = _number('bit_diameter')
    bit_angle = _number('bit_angle', 90.0)
    if not 0 < width < 1.0:
        raise ValueError(f'Chamfer width must be between 0 and 1 inch, got {width:g}.')
    if not 0 < bit_diameter <= 2.0:
        raise ValueError(f'Chamfer V-bit diameter must be between 0 and 2 inches, '
                         f'got {bit_diameter:g}.')
    if not 0 < bit_angle < 180:
        raise ValueError(f'Chamfer V-bit included angle must be between 0 and 180 '
                         f'degrees, got {bit_angle:g}.')

    # None means "not specified" and defaults; an explicitly EMPTY list is the UI with
    # every edge unchecked, and deserves the "needs at least one edge" error below.
    targets_raw = spec.get('targets')
    if targets_raw is None:
        targets_raw = ['perimeter']
    if isinstance(targets_raw, str):
        targets_raw = [t.strip() for t in targets_raw.split(',') if t.strip()]
    if not isinstance(targets_raw, (list, tuple)):
        raise ValueError('Chamfer targets must be a list or comma-separated string.')
    targets = []
    for target in targets_raw:
        if target not in CHAMFER_TARGETS:
            raise ValueError(f'Unknown chamfer target {target!r}. Expected any of: '
                             f'{", ".join(CHAMFER_TARGETS)}.')
        if target not in targets:
            targets.append(target)
    if not targets:
        raise ValueError('The chamfer pass needs at least one edge to break.')

    return {'width': width, 'bit_diameter': bit_diameter, 'bit_angle': bit_angle,
            'targets': targets}


def add_timestamp_to_filename(filename: str) -> str:
    """Add timestamp to filename before extension."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(filename)[0]
    extension = os.path.splitext(filename)[1]
    return f"{base_name}_{timestamp}{extension}"


def load_cli_config(config_path: str):
    """Load a PenguinCAM-config.yaml for a CLI run, or return None for built-in defaults.

    Shared by every mode, and deliberately strict in the same way local mode is: a path
    that does not exist, cannot be read, or is not usable is REPORTED and stops the run.
    Falling back to Team 6238's defaults on a typo would have the machine cutting on
    someone else's feeds without saying so.
    """
    if not config_path:
        return None
    if not os.path.isfile(config_path):
        print(f"ERROR: config file not found: {config_path}")
        raise SystemExit(1)
    try:
        with open(config_path, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh.read())
    except OSError as exc:
        print(f"ERROR: cannot read {config_path}: {exc}")
        raise SystemExit(1)
    except UnicodeDecodeError:
        print(f"ERROR: {config_path} is not UTF-8 text. Re-save it as UTF-8.")
        raise SystemExit(1)
    except yaml.YAMLError as exc:
        print(f"ERROR: {config_path} is not valid YAML: {exc}")
        raise SystemExit(1)
    if not isinstance(data, dict):
        print(f"ERROR: {config_path} does not contain a PenguinCAM config.")
        raise SystemExit(1)
    config = TeamConfig.from_dict(data)
    print(f"Using config {config_path}: {config.team_name} (#{config.team_number})")
    return config


def run_ops_file(ops_path: str, output_gcode: str, config_path: str = None,
                 user: str = None) -> int:
    """Run a multi-tool job described by a JSON file. Returns a process exit code.

    The job file is the same shape the web UI posts to /process-multitool, with each
    part naming a `dxf` path (relative to the job file) instead of an upload index::

        {
          "name": "gearbox",
          "material": "aluminum",
          "thickness": 0.25,
          "tools": [
            {"slot": 1, "name": "1/8 in 1-flute", "diameter": 0.125, "flutes": 1},
            {"slot": 2, "name": "1/4 in 1-flute", "diameter": 0.25,  "flutes": 1},
            {"slot": 3, "name": "1/2 in 90 deg V", "diameter": 0.5, "flutes": 2,
             "type": "vbit", "included_angle": 90}
          ],
          "parts": [
            {"dxf": "plate.dxf", "name": "plate", "place_x": 0, "place_y": 0,
             "operations": [
               {"op_type": "holes",     "tool_slot": 1, "scope": {"max_diameter": 0.3}},
               {"op_type": "holes",     "tool_slot": 2, "scope": {"min_diameter": 0.3}},
               {"op_type": "pockets",   "tool_slot": 2, "depth": 0.125},
               {"op_type": "perimeter", "tool_slot": 2},
               {"op_type": "chamfer",   "tool_slot": 3,
                "scope": {"targets": ["perimeter"], "width": 0.02}}
             ]}
          ]
        }

    This is the scriptable half of local mode: no browser, no Onshape, no network.
    """
    import json
    import tooling

    try:
        with open(ops_path, 'r', encoding='utf-8') as fh:
            spec = json.load(fh)
    except OSError as exc:
        print(f"ERROR: cannot read job file {ops_path}: {exc}")
        return 1
    except ValueError as exc:
        print(f"ERROR: {ops_path} is not valid JSON: {exc}")
        return 1

    # Part DXF paths are resolved relative to the job file, so a job folder can be
    # copied or shared whole without every path breaking.
    base_dir = os.path.dirname(os.path.abspath(ops_path))
    dxf_paths = {}
    for i, part in enumerate(spec.get('parts') or []):
        raw = part.get('dxf') or part.get('file')
        if not raw:
            print(f"ERROR: part {i + 1} in {ops_path} has no \"dxf\" path")
            return 1
        path = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
        if not os.path.isfile(path):
            print(f"ERROR: part {i + 1} references {path}, which does not exist")
            return 1
        part['file_index'] = i
        dxf_paths[i] = path

    config = load_cli_config(config_path)

    try:
        job = tooling.job_from_dict(spec, dxf_paths, config=config, user_name=user)
        result = tooling.generate_multitool_job(
            job, suggested_filename=os.path.splitext(os.path.basename(output_gcode))[0])
    except tooling.ToolingError as exc:
        print(f"ERROR: {exc}")
        return 1

    if not result.success:
        print("ERROR: Failed to generate G-code")
        for error in result.errors:
            print(f"  - {error}")
        return 1

    output_path = os.path.join(os.path.dirname(output_gcode) or '.', result.filename)
    with open(output_path, 'w') as fh:
        fh.write(result.gcode)

    print(f"OUTPUT_FILE:{output_path}")
    print(f"\nDone! G-code written to: {output_path}")
    print(f"\nTOOLS ({result.stats['num_tools']}, "
          f"{result.stats['tool_changes']} manual change(s)):")
    for line in result.stats['tools']:
        print(f"  {line}")
    print(f"\n{result.stats['num_operations']} operation(s) across "
          f"{result.stats['num_parts']} part(s), {result.stats['total_lines']} lines")
    print(f"Estimated cycle time: {result.stats['cycle_time_display']}"
          + (" (excludes time spent changing tools)"
             if result.stats.get('excludes_tool_change_time') else ""))
    for warning in result.warnings or []:
        print(f"  WARNING: {warning}")
    print("\nReview the G-code file before running on your machine.")
    return 0


def main():
    parser = argparse.ArgumentParser(description='PenguinCAM - Team 6238 Post-Processor')
    parser.add_argument('input_dxf', nargs='?', help='Input DXF file from Onshape (not needed for tube-facing or --ops-file mode)')
    parser.add_argument('output_gcode', help='Output G-code file')
    parser.add_argument('--ops-file', type=str, default=None,
                       help='JSON file describing a multi-tool job: the tools loaded and, '
                            'for each part, the ordered operations and which tool cuts each. '
                            'Replaces --tool-diameter and the single-tool modes; see '
                            'run_ops_file for the format.')
    parser.add_argument('--config', type=str, default=None,
                       help='Path to a PenguinCAM-config.yaml with your team/machine '
                            'settings, used instead of the built-in defaults. Applies to '
                            'every mode. Local runs have no Onshape to fetch it from.')
    parser.add_argument('--mode', type=str, default='standard',
                       choices=['standard', 'tube-facing', 'tube-pattern'],
                       help='Operation mode: standard (DXF processing), tube-facing (square tube ends), or tube-pattern (DXF pattern on tube faces)')
    parser.add_argument('--tube-size', type=str, default='1x1',
                       choices=['1x1', '2x1-standing', '2x1-flat'],
                       help='Tube size for tube-facing mode')
    parser.add_argument('--tube-height', type=float, default=1.0,
                       help='Tube Z-height in inches for tube-pattern mode (default: 1.0)')
    parser.add_argument('--tube-width', type=float,
                       help='Tube face width (X dimension) in inches for tube-pattern mode (optional, calculated from DXF if not provided)')
    parser.add_argument('--tube-length', type=float,
                       help='Tube face length (Y dimension) in inches for tube-pattern mode (optional, calculated from DXF if not provided)')
    parser.add_argument('--tube-pattern', type=str, default='none',
                        choices=['none', 'holes', 'lightening'],
                        help="Use a pre-designed tube pattern instead of a DXF. "
                             "'holes' DRILLS #10 clearance holes on 0.5in centres, three "
                             "per column on a 2in face and one on a 1in face - the tool "
                             "must be a 0.201in twist drill. 'lightening' mills "
                             "right-triangle truss pockets and no holes. The two are "
                             "never combined. Requires --tube-length.")
    parser.add_argument('--square-end', action='store_true',
                       help='Square the tube end before machining pattern (tube-pattern mode)')
    parser.add_argument('--cut-to-length', action='store_true',
                       help='Machine tube to length after pattern (tube-pattern mode)')
    parser.add_argument('--material', type=str, default='aluminum',
                       help='Material preset (default: aluminum). Built-in: plywood, aluminum, polycarbonate. Custom materials from config also supported.')
    parser.add_argument('--thickness', type=float, default=0.25,
                       help='Material thickness in inches (default: 0.25)')
    parser.add_argument('--tool-diameter', type=float, default=0.25,
                       help='Tool diameter in inches (default: 0.25 = 1/4 in endmill)')
    parser.add_argument('--tool-flutes', type=int, default=1,
                       help='Number of cutting flutes (default: 1; aluminum router jobs '
                            'require a 1- or 2-flute cutter)')
    parser.add_argument('--sacrifice-depth', type=float, default=0.02,
                       help='How far to cut into sacrifice board in inches (default: 0.02")')
    parser.add_argument('--z-zero', choices=['board', 'stock-top'], default=None,
                       help='Which surface the operator zeros Z on: "board" (the sacrifice '
                            'board, the default) or "stock-top" (the top face of the stock, '
                            'so cutting Z is negative throughout). Omit to use the team config.')
    parser.add_argument('--units', choices=['inch', 'mm'], default='inch',
                       help='Units (default: inch)')
    parser.add_argument('--chamfer-width', type=float, default=None,
                        help='Add a deburr / chamfer pass after the profile (standard mode '
                             'only): horizontal edge-break width in inches, e.g. 0.02. '
                             'The program pauses for a manual change to the V-bit.')
    parser.add_argument('--chamfer-bit-diameter', type=float, default=0.5,
                        help='V-bit diameter in inches for the chamfer pass (default: 0.5)')
    parser.add_argument('--chamfer-bit-angle', type=float, default=90.0,
                        help='V-bit full included angle in degrees for the chamfer pass '
                             '(default: 90)')
    parser.add_argument('--chamfer-targets', type=str, default='perimeter,holes',
                        help='Comma-separated edges the chamfer pass breaks: perimeter, '
                             'holes, pockets (default: perimeter,holes)')
    parser.add_argument('--max-pass-depth', type=float, default=None,
                        help='Ceiling on the depth of one contour pass (in the --units '
                             'system). Profiles and pockets split into more, shallower '
                             'passes - use this to baby fragile or multi-flute cutters. '
                             'Only ever lowers the automatic per-pass depth.')
    parser.add_argument('--tab-spacing', type=float, default=6.0,
                       help='Desired spacing between tabs in inches (default: 6.0, minimum 3 tabs)')
    parser.add_argument('--origin-corner', default='bottom-left',
                       choices=['bottom-left', 'bottom-right', 'top-left', 'top-right'],
                       help='Which corner should be origin (0,0) - default: bottom-left')
    parser.add_argument('--rotation', type=int, default=0,
                       choices=[0, 90, 180, 270],
                       help='Rotation angle in degrees clockwise (default: 0)')
    parser.add_argument('--user', type=str, default=None,
                       help='User name for G-code header (from Google OAuth)')

    # NEW: Cutting parameters
    parser.add_argument('--spindle-speed', type=int, default=18000,
                       help='Spindle speed in RPM (default: 18000)')
    parser.add_argument('--feed-rate', type=float, default=None,
                       help='Feed rate (default: 14 ipm or 365 mm/min depending on units)')
    parser.add_argument('--plunge-rate', type=float, default=None,
                       help='Plunge rate (default: 10 ipm or 339 mm/min depending on units)')
    
    args = parser.parse_args()

    if args.tool_flutes < 1 or args.tool_flutes > 12:
        parser.error('--tool-flutes must be a whole number from 1 to 12')
    import feeds_speeds as _feeds_speeds
    if _feeds_speeds.is_aluminum_material(args.material):
        safety = _feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX
        to_ipm = (1.0 / 25.4) if args.units == 'mm' else 1.0
        diameter_in = args.tool_diameter * to_ipm
        factor = min(1.0, (diameter_in / _feeds_speeds.REFERENCE_TOOL['diameter'])
                     ** _feeds_speeds.DIAMETER_EXPONENT)
        if (args.feed_rate is not None
                and (not math.isfinite(args.feed_rate) or args.feed_rate <= 0
                     or args.feed_rate * to_ipm > safety['feed_rate'] * factor)):
            parser.error(f"--feed-rate {args.feed_rate:g} exceeds the aluminum router "
                         f"diameter-scaled safety ceiling of "
                         f"{safety['feed_rate'] * factor:g} IPM")
        if (args.plunge_rate is not None
                and (not math.isfinite(args.plunge_rate) or args.plunge_rate <= 0
                     or args.plunge_rate * to_ipm > safety['plunge_rate'] * factor)):
            parser.error(f"--plunge-rate {args.plunge_rate:g} exceeds the aluminum router "
                         f"diameter-scaled safety ceiling of "
                         f"{safety['plunge_rate'] * factor:g} IPM")

    # The chamfer pass belongs to standard mode only. Refuse it loudly elsewhere: a
    # deburr flag silently dropped from a tube program would read as a promise kept.
    if args.chamfer_width is not None and (args.ops_file or args.mode != 'standard'):
        parser.error('--chamfer-width is only supported in standard mode. Multi-tool '
                     'jobs (--ops-file) describe a chamfer as an operation instead.')

    # Tube modes are inch-only, all the way through: the tube frame, the jig geometry
    # and the emitted G20 are all inches, and load_tube_pattern / load_tube_design
    # already refuse millimetres. The CLI did not, so `--units mm` built a millimetre
    # post-processor and then emitted inch tube geometry under a hard-coded G20.
    if args.mode.startswith('tube-'):
        if args.units != 'inch':
            parser.error(f'--mode {args.mode} is inch-only. Tube jigs, tube sizes and '
                         f'the tube coordinate frame are all in inches; drop '
                         f'--units mm and give the dimensions in inches.')
        # Silently ignoring --z-zero hid a real mistake: a tube job zeroes on the JIG,
        # not on a sheet lying on a spoilboard, so neither datum choice means anything.
        if args.z_zero is not None:
            parser.error(f'--z-zero does not apply to --mode {args.mode}. A tube job is '
                         f'zeroed at the jig in G54, with Z=0 at the bottom of the tube; '
                         f'there is no stock top or sacrifice board to choose between.')

    # A multi-tool job describes its own tools and operations, so it bypasses the
    # single-tool mode branching below entirely.
    if args.ops_file:
        sys.exit(run_ops_file(args.ops_file, args.output_gcode,
                              config_path=args.config, user=args.user))

    # Mode branching
    if args.mode == 'tube-facing':
        # Tube facing mode - generate G-code for squaring tube ends
        if not args.output_gcode:
            parser.error("output_gcode is required for tube-facing mode")

        pp = FRCPostProcessor(args.thickness, args.tool_diameter,
                              config=load_cli_config(args.config),
                              tool_flutes=args.tool_flutes)
        try:
            pp.apply_material_preset(args.material)  # Tube facing is always aluminum family
        except ValueError as exc:
            parser.error(str(exc))
        pp.scale_feeds_to_tool()
        if args.spindle_speed != 18000:
            pp.spindle_speed = args.spindle_speed
        if args.feed_rate is not None:
            pp.feed_rate = args.feed_rate
        if args.plunge_rate is not None:
            pp.plunge_rate = args.plunge_rate

        # Call API to generate G-code
        base_name = os.path.splitext(os.path.basename(args.output_gcode))[0]
        result = pp.generate_tube_facing_gcode(tube_size=args.tube_size, suggested_filename=base_name)

        if not result.success:
            print(f"ERROR: Failed to generate G-code")
            for error in result.errors:
                print(f"  - {error}")
            sys.exit(1)

        # Write G-code to file
        output_path = os.path.join(os.path.dirname(args.output_gcode) or '.', result.filename)
        with open(output_path, 'w') as f:
            f.write(result.gcode)

        # Print output for CLI
        print(f'OUTPUT_FILE:{output_path}')
        print(f'Tube facing G-code generated for {args.tube_size} tube')
        print(f"\nIdentified 0 millable holes and 0 pockets")
        print(f"Total lines: {result.stats['total_lines']}")
        print(f"\n⏱️  ESTIMATED_CYCLE_TIME: {result.stats['cycle_time_seconds']:.1f} seconds ({result.stats['cycle_time_display']})")
        print(f'\nSETUP:')
        for instruction in result.stats['setup_instructions']:
            print(f'  {instruction}')
        print(f'\nOPERATION:')
        for note in result.stats['operation_notes']:
            print(f'  {note}')

    elif args.mode == 'tube-pattern':
        # Tube pattern mode - machine a pattern on both tube faces. The pattern comes
        # either from a DXF the user drew or, with --tube-pattern, from tube_patterns.
        use_pattern = args.tube_pattern != 'none'
        if not use_pattern and not args.input_dxf:
            parser.error("input_dxf is required for tube-pattern mode "
                         "(or pass --tube-pattern standard to generate one)")
        if use_pattern and not args.tube_length:
            parser.error("--tube-length is required with --tube-pattern "
                         "(it decides how many holes fit)")
        if not args.output_gcode:
            parser.error("output_gcode is required for tube-pattern mode")

        # A drilled hole pattern is produced by sizing the cutter to the hole, so the
        # tool for that mode IS the drill. Set before the post-processor is built rather
        # than mutated afterwards, because min_millable_hole is derived from it at
        # construction and a later change would leave the two disagreeing.
        if use_pattern and args.tube_pattern == 'holes':
            import tube_patterns as _tp
            if abs(args.tool_diameter - _tp.HOLE_DIAMETER) > 1e-4:
                print(f'Using a {_tp.HOLE_DIAMETER:.4f}" twist drill for the hole pattern '
                      f'(--tool-diameter {args.tool_diameter:.4f}" is for milling)')
            args.tool_diameter = _tp.HOLE_DIAMETER

        # Create post-processor with tube WALL thickness (not height!)
        pp = FRCPostProcessor(material_thickness=args.thickness,
                              tool_diameter=args.tool_diameter,
                              units=args.units,
                              config=load_cli_config(args.config),
                              tool_flutes=(1 if use_pattern and args.tube_pattern == 'holes'
                                           else args.tool_flutes))

        # Store tube height for Z-offset calculations
        pp.tube_height = args.tube_height

        # Apply material preset and user parameters (shared logic). Scaled to the
        # actual tool - a no-op for the drilled pattern's 0.201" bit, a derate for a
        # custom design milled with a small cutter. Explicit feed flags come last.
        try:
            pp.apply_material_preset(args.material)
        except ValueError as exc:
            parser.error(str(exc))
        pp.scale_feeds_to_tool()
        if args.user:
            pp.user_name = args.user
        if args.spindle_speed != 18000:
            pp.spindle_speed = args.spindle_speed
        if args.feed_rate is not None:
            pp.feed_rate = args.feed_rate
        if args.plunge_rate is not None:
            pp.plunge_rate = args.plunge_rate

        # Load the pattern - generated or drawn.
        pattern_warnings = []
        if use_pattern:
            # The face being machined and the tube height both follow from the tube size;
            # --tube-width / --tube-height still win when given, so an odd extrusion or a
            # tube standing on edge can be described exactly.
            size_width, size_height = pp._parse_tube_size(args.tube_size)
            face_width = args.tube_width if args.tube_width else size_width
            # Squaring the end needs the real face width; without this it would be
            # re-derived from the pattern's own extents and come up narrow.
            args.tube_width = face_width
            if not any(a.startswith('--tube-height') for a in sys.argv):
                args.tube_height = size_height
                pp.tube_height = size_height
            pattern_warnings = pp.load_tube_pattern(
                face_width, args.tube_length, mode=args.tube_pattern)
        else:
            pp.load_dxf(args.input_dxf)
            pp.transform_coordinates('bottom-left', args.rotation)  # Tube jig is always bottom-left
            # identify_perimeter_and_pockets FIRST: it is what claims a circular outline
            # as the perimeter and drops that circle from self.circles. Classifying holes
            # first left a round part's outline machined as a giant hole as well.
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()

        # Debug: Check what was classified
        hole_count = len(pp.holes) if hasattr(pp, 'holes') else 0
        pocket_count = len(pp.pockets) if hasattr(pp, 'pockets') else 0
        has_perimeter = bool(pp.perimeter) if hasattr(pp, 'perimeter') else False
        print(f'DEBUG: Classified {hole_count} holes, {pocket_count} pockets, perimeter={has_perimeter}')

        # Call API to generate G-code
        base_name = os.path.splitext(os.path.basename(args.output_gcode))[0]
        result = pp.generate_tube_pattern_gcode(
            tube_height=args.tube_height,
            square_end=args.square_end,
            cut_to_length=args.cut_to_length,
            tube_width=args.tube_width,
            tube_length=args.tube_length,
            suggested_filename=base_name
        )

        if not result.success:
            print(f"ERROR: Failed to generate G-code")
            for error in result.errors:
                print(f"  - {error}")
            sys.exit(1)

        # Write G-code to file
        output_path = os.path.join(os.path.dirname(args.output_gcode) or '.', result.filename)
        with open(output_path, 'w') as f:
            f.write(result.gcode)

        # Print output for CLI
        print(f'OUTPUT_FILE:{output_path}')
        print(f'Tube pattern G-code generated')
        print(f"\nIdentified {result.stats['num_holes_per_face']} millable holes and {result.stats['num_pockets_per_face']} pockets on each face")
        print(f"Total lines: {result.stats['total_lines']}")
        print(f"\n⏱️  ESTIMATED_CYCLE_TIME: {result.stats['cycle_time_seconds']:.1f} seconds ({result.stats['cycle_time_display']})")
        print(f'\nSETUP:')
        for instruction in result.stats['setup_instructions']:
            print(f'  {instruction}')
        print(f'\nOPERATIONS:')
        for note in result.stats['operation_notes']:
            print(f'  {note}')

    else:
        # Standard mode - DXF processing
        if not args.input_dxf:
            parser.error("input_dxf is required for standard mode")

        # Create post-processor
        pp = FRCPostProcessor(material_thickness=args.thickness,
                              tool_diameter=args.tool_diameter,
                              units=args.units,
                              config=load_cli_config(args.config),
                              z_datum=args.z_zero,
                              tool_flutes=args.tool_flutes)

        # Apply material preset and user parameters (shared logic). The preset is tuned
        # for the 4 mm reference tool; scale it to the tool actually specified. An
        # explicit --feed-rate / --plunge-rate afterwards is the user overriding the
        # derate on purpose, so those come last.
        try:
            pp.apply_material_preset(args.material)
        except ValueError as exc:
            parser.error(str(exc))
        pp.scale_feeds_to_tool()
        if args.max_pass_depth is not None:
            try:
                pp.apply_max_pass_depth(args.max_pass_depth)
            except ValueError as exc:
                parser.error(str(exc))
        if args.user:
            pp.user_name = args.user
        if args.spindle_speed != 18000:
            pp.spindle_speed = args.spindle_speed
        if args.feed_rate is not None:
            pp.feed_rate = args.feed_rate
        if args.plunge_rate is not None:
            pp.plunge_rate = args.plunge_rate

        # Standard mode specific parameters
        pp.tab_spacing = args.tab_spacing
        pp.sacrifice_board_depth = args.sacrifice_depth
        pp._apply_z_frame()   # the sacrifice depth moves the bottom of every through-cut

        # Optional deburr / chamfer pass with a user-specified V-bit.
        if args.chamfer_width is not None:
            try:
                pp.chamfer_pass = parse_chamfer_spec({
                    'width': args.chamfer_width,
                    'bit_diameter': args.chamfer_bit_diameter,
                    'bit_angle': args.chamfer_bit_angle,
                    'targets': args.chamfer_targets,
                })
            except ValueError as exc:
                parser.error(str(exc))

        # Load and process DXF (shared logic)
        pp.load_dxf(args.input_dxf)
        pp.transform_coordinates(args.origin_corner, args.rotation)
        # Perimeter first, then holes - see the tube branch above and the route in
        # frc_cam_gui_app, which have always done it in this order.
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        # Call API to generate G-code
        base_name = os.path.splitext(os.path.basename(args.output_gcode))[0]
        result = pp.generate_gcode(suggested_filename=base_name)

        if not result.success:
            print(f"ERROR: Failed to generate G-code")
            for error in result.errors:
                print(f"  - {error}")
            sys.exit(1)

        # Write G-code to file
        output_path = os.path.join(os.path.dirname(args.output_gcode) or '.', result.filename)
        with open(output_path, 'w') as f:
            f.write(result.gcode)

        # Print actual output path for GUI to parse (prefixed with OUTPUT_FILE:)
        print(f"OUTPUT_FILE:{output_path}")
        print(f"\nDone! G-code written to: {output_path}")
        print("Review the G-code file before running on your machine.")
        print(f"\nCUTTING PARAMETERS:")
        print(f"  Spindle speed: {pp.spindle_speed} RPM")
        print(f"  Feed rate: {pp.feed_rate:.1f} {args.units}/min")
        print(f"  Plunge rate: {pp.plunge_rate:.1f} {args.units}/min")
        print(f"\nZ-AXIS SETUP:")
        print(f"  ** Zero your Z-axis to {pp.z_zero_surface().upper()} **")
        print(f"  Material top will be at Z={pp.material_top:.4f}\"")
        print(f"  Cut depth: Z={pp.cut_depth:.4f}\" ({pp.sacrifice_board_depth:.4f}\" into sacrifice board)")
        print(f"  Retract height: Z={pp.retract_height:.4f}\"")
        print(f"\nTool compensation applied:")
        print(f"  Tool diameter: {pp.tool_diameter:.4f}\"")
        print(f"  Tool radius: {pp.tool_radius:.4f}\"")
        print(f"  Perimeter: offset OUTWARD by {pp.tool_radius:.4f}\"")
        print(f"  Pockets: offset INWARD by {pp.tool_radius:.4f}\"")
        print(f"  Holes: toolpath radius reduced by {pp.tool_radius:.4f}\" (holes < {pp.min_millable_hole:.3f}\" skipped)")


if __name__ == '__main__':
    main()
