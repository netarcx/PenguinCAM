"""
Team Configuration Management for PenguinCAM

Handles loading and managing team-specific settings from YAML config files
stored in Onshape documents. Falls back to Team 6238 defaults if config
is missing or incomplete.
"""

import copy
import re
import yaml
from typing import Optional, Dict, Any, List


# =============================================================================
# LENGTH PARSING (units in config values)
# =============================================================================
# PenguinCAM is inch-native, but config values may carry a unit so metric and SAE
# tooling can be expressed naturally (e.g. "4mm", '0.25"', "1/8", "3 cm"). Numbers are
# treated as inches. Mirrors the client-side parser in static/wizard.js.

_LENGTH_TO_INCH = {
    '': 1.0, 'in': 1.0, 'inch': 1.0, 'inches': 1.0, '"': 1.0,
    'mm': 1 / 25.4, 'millimeter': 1 / 25.4, 'millimeters': 1 / 25.4,
    'cm': 1 / 2.54, 'centimeter': 1 / 2.54, 'centimeters': 1 / 2.54,
    'm': 1 / 0.0254, 'meter': 1 / 0.0254, 'meters': 1 / 0.0254,
    'ft': 12.0, 'foot': 12.0, 'feet': 12.0, "'": 12.0,
    'yd': 36.0, 'yard': 36.0, 'yards': 36.0,
}
_FRACTION_RE = re.compile(r'^([+-]?\d+)\s*/\s*(\d+)\s*(.*)$')
_DECIMAL_RE = re.compile(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(.*)$')

# Default tool when a config doesn't specify one: 4mm end mill (in inches).
DEFAULT_TOOL_DIAMETER_IN = 0.25   # 1/4" endmill: the cutter most jobs are run with


#: Keys that live at the ROOT of a config, not inside a machine: things the shop owns
#: rather than things a machine has. `team` is handled separately by _get.
ROOT_LEVEL_KEYS = frozenset({'tools', 'stock', 'team'})


def slugify_tool_id(name: str) -> str:
    """A stable, URL-safe id for a saved bit, derived from its name.

    The name is what the operator recognises ("the 1/4 two-flute"), so it is also the
    identity: saving a bit whose name matches an existing one updates that one rather
    than leaving two rows a shelf apart in the list."""
    slug = re.sub(r'[^a-z0-9]+', '_', str(name).strip().lower()).strip('_')
    return slug or 'bit'

# Leaf keys whose values are lengths and may therefore be given as unit strings. Only
# these are converted during config normalization, so material names, work offsets, feed
# rates, angles, and ratios are never misinterpreted. 'diameter' is intentionally absent:
# the default_tool_diameter properties parse it directly so the raw text stays available
# for display.
LENGTH_KEYS = frozenset({
    'x_max', 'y_max', 'z_max',                                   # machine.dimensions
    'x', 'y', 'z',                                               # machine.park_position
    'sacrifice_board_depth', 'clearance_height', 'safe_height',
    'tool_change_height',                                      # machining.z_reference
    'width', 'height', 'spacing',                               # machining.tabs
    'depth_margin', 'max_roughing_depth', 'max_finishing_depth',  # tube_facing
    'roughing_tool_edge', 'finishing_tool_edge', 'arc_advance', 'arc_radius',
    'ramp_start_clearance', 'max_slotting_depth', 'peck_drill_depth',  # materials
    'tab_width', 'tab_height',
})


def parse_length(value):
    """Parse a possibly-unit-bearing length into inches, or None if unparseable.

    Numbers pass through as inches. Strings may use mm/cm/m/in/"/ft/'/yd, plain numbers,
    or fractions (e.g. "1/8"); no unit means inches. Negative values are allowed because
    some config offsets (park Z, tube-facing edges) are legitimately negative.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s:
        return None
    m = _FRACTION_RE.match(s)
    if m:
        denom = float(m.group(2))
        if denom == 0:
            return None
        number = float(m.group(1)) / denom
        unit = m.group(3).strip()
    else:
        m = _DECIMAL_RE.match(s)
        if not m:
            return None
        number = float(m.group(1))
        unit = m.group(2).strip()
    factor = _LENGTH_TO_INCH.get(unit)
    if factor is None:
        return None
    return number * factor


def _normalize_lengths(node):
    """Recursively convert unit-string length values (on LENGTH_KEYS) to inch floats,
    in place. Numbers and non-length strings are left untouched."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _normalize_lengths(value)
            elif isinstance(value, str) and key in LENGTH_KEYS:
                parsed = parse_length(value)
                if parsed is not None:
                    node[key] = parsed
    elif isinstance(node, list):
        for item in node:
            _normalize_lengths(item)


# =============================================================================
# TEAM 6238 DEFAULTS
# These are used as fallbacks when config values are missing
# =============================================================================

TEAM_6238_DEFAULTS = {
    'team': {
        'number': 6238,
        'name': 'Popcorn Penguins'
    },
    'machine': {
        'name': 'Generic CNC Router',
        'manufacturer': 'Generic',
        'controller': 'Generic',
        'dimensions': {'x_max': 24.0, 'y_max': 24.0, 'z_max': 8.0},
        # park_position, coolant, and tube_work_coordinate_system are intentionally NOT
        # defaulted: they are opt-in. A config with no park_position emits portable G54-only
        # output (no G53); no coolant -> no M7/M8/M9; no tube_work_coordinate_system -> tube
        # ops use G54 (operator zeros it per tube), instead of a fixed jig WCS. Machines that
        # want these (e.g. Mach4 routers with a fixed jig) add them to their config. Keeps
        # output compatible with GRBL/WinCNC.
    },
    'machining': {
        'z_reference': {
            'sacrifice_board_depth': 0.008,
            'clearance_height': 0.5
        },
        'tabs': {
            'enabled': True,
            'width': 0.25,
            'height': 0.1,
            'spacing': 6.0,
            'remove_tabs': True
        },
        'fixturing': {
            'pause_before_perimeter': False
        },
        'holes': {
            'detection_tolerance': 0.02,
            'min_millable_multiplier': 1.2
        },
        'pockets': {
            # Contour threshold: feature area in multiples of tool cross-section (at 100% stepover)
            # Applies to both irregular pockets AND large circular holes (through-cuts only)
            # IMPORTANT: Only applies to through-cuts (Z <= 0); partial-depth features always cleared
            # Formula: threshold_area = contour_threshold × tool_diameter² × stepover_percentage
            # Actual threshold scales directly with stepover (tighter stepover = lower area threshold)
            # Default: 510 (aluminum: ~2.0" dia, plywood: ~3.2" dia, polycarbonate: ~3.0" dia)
            # Set to 0 to disable (always fully clear all features)
            'contour_threshold': 510
        },
        'default_tool': {
            'diameter': '1/4"'  # default when unspecified; parsed to inches for machining
        },
        # Which material the wizard opens on. A team that mostly cuts one thing should
        # not have to change the selector every session.
        'default_material': 'aluminum'
    },
    'tube_facing': {
        'depth_margin': 0.005,
        'max_roughing_depth': 0.3,
        'max_finishing_depth': 0.51,
        'phase_1': {
            'roughing_tool_edge': 0.05,
            'finishing_tool_edge': 0.0625
        },
        'phase_2': {
            'roughing_tool_edge': -0.0125,
            'finishing_tool_edge': 0.0
        },
        'arc_advance': 0.04,
        'arc_radius': 0.05
    },
    'materials': {
        'plywood': {
            'name': 'Plywood',
            'spindle_speed': 18000,
            'feed_rate': 75.0,
            'ramp_feed_rate': 50.0,
            'plunge_rate': 35.0,
            'traverse_rate': 200.0,
            'approach_rate': 50.0,
            'ramp_angle': 20.0,
            'ramp_start_clearance': 0.150,
            'stepover_percentage': 0.65,
            'helix_radius_multiplier': 0.75,
            'max_slotting_depth': 0.4,
            'peck_drill_depth': 0.05,
            'corner_min_feed_scale': 0.7,   # softer/heat-limited: keep feed up to preserve chip load
            'tab_width': 0.25,
            'tab_height': 0.15
        },
        'aluminum': {
            # Derated 2026-08-24: 55 IPM / 0.2" slot let a 1/8" plate be slotted in
            # one full-thickness pass and snapped real cutters. See MULTI_TOOL_STATUS.
            'name': 'Aluminum',
            'spindle_speed': 18000,
            'feed_rate': 30.0,
            'ramp_feed_rate': 19.0,
            'plunge_rate': 15.0,
            'traverse_rate': 200.0,
            'approach_rate': 35.0,
            'ramp_angle': 4.0,
            'ramp_start_clearance': 0.050,
            'stepover_percentage': 0.25,
            'helix_radius_multiplier': 0.5,
            'max_slotting_depth': 0.06,     # 0.38 x the 4mm reference diameter
            'peck_drill_depth': 0.05,
            'corner_min_feed_scale': 0.6,   # ease corners without crossing into aluminum rubbing
            'tab_width': 0.25,
            'tab_height': 0.15
        },
        'polycarbonate': {
            'name': 'Polycarbonate',
            'spindle_speed': 18000,
            'feed_rate': 75.0,
            'ramp_feed_rate': 50.0,
            'plunge_rate': 20.0,
            'traverse_rate': 200.0,
            'approach_rate': 50.0,
            'ramp_angle': 20.0,
            'ramp_start_clearance': 0.100,
            'stepover_percentage': 0.55,
            'helix_radius_multiplier': 0.75,
            'max_slotting_depth': 0.25,
            'peck_drill_depth': 0.05,
            'corner_min_feed_scale': 0.7,   # softer/heat-limited: keep feed up to preserve chip load
            'tab_width': 0.25,
            'tab_height': 0.15
        }
    },
    'integrations': {
        'google_drive': {
            'enabled': False,
            'folder_id': None
        }
    }
}


class TeamConfig:
    """
    Manages team-specific configuration for PenguinCAM.

    Config is loaded from a YAML file stored in the team's Onshape documents
    named "PenguinCAM-config.yaml". Falls back to Team 6238 defaults for any
    missing values.
    """

    def __init__(self, config_data: Optional[Dict[str, Any]] = None):
        """
        Initialize team config from YAML data.

        Args:
            config_data: Parsed YAML config dict, or None for defaults
        """
        if config_data is None:
            config_data = {}

        # Normalize to v2 structure internally for consistent API. Deep-copy first so we
        # never mutate the caller's dict (e.g. the session config), then convert any
        # unit-string length values (e.g. "4mm") to inch floats so all downstream readers
        # see plain numbers.
        self._data = copy.deepcopy(self._normalize_to_v2(config_data))
        # The shop-owned lists are parsed by their own readers, which keep the text the
        # user wrote ("600mm", '1/4"') alongside the inches so the UI can show it back
        # verbatim. Normalising them here would turn 600mm into 23.622 before anyone
        # saw it - the same reason `diameter` is deliberately absent from LENGTH_KEYS.
        held = {key: self._data.pop(key) for key in ROOT_LEVEL_KEYS if key in self._data}
        _normalize_lengths(self._data)
        self._data.update(held)

    def _normalize_to_v2(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert any config version to v2 structure internally.

        v1: Top-level machine/materials/integrations
        v2: machines -> machine_id -> machine/materials/integrations

        Args:
            data: Raw config data

        Returns:
            Normalized v2 structure
        """
        version = data.get('version', 1)

        if version == 1:
            # Wrap v1 config as single machine named 'default'
            # Copy all keys except 'version' into the default machine
            machine_config = {}
            for key, value in data.items():
                if key != 'version' and key not in ROOT_LEVEL_KEYS:
                    machine_config[key] = value

            # Ensure the wrapped machine has a display name. In a v1 config the
            # name lives nested under `machine.name`, not at the top level, so
            # promote it (falling back to a generic label only if truly absent).
            if 'name' not in machine_config:
                nested_name = machine_config.get('machine', {}).get('name')
                machine_config['name'] = nested_name or 'Default Machine'

            wrapped = {
                'version': 2,
                'default_machine': 'default',
                'machines': {
                    'default': machine_config
                }
            }
            # Shop-wide lists belong to the team, not to the one machine a v1 config
            # describes. Folding them into the machine block lost them entirely - a v1
            # team's saved bits and stock simply disappeared, silently.
            for key in ROOT_LEVEL_KEYS:
                if key in data:
                    wrapped[key] = data[key]
            return wrapped

        elif version == 2:
            # Already v2, use as-is
            return data

        else:
            raise ValueError(f"Unsupported config version: {version}")

    def _get(self, *keys, default=None):
        """
        Safely get nested dict value with fallback to Team 6238 defaults.

        For v2 configs, checks root level first (for 'team'), then machine config.

        Args:
            *keys: Path to nested value (e.g., 'machine', 'park_position', 'x')
            default: Optional override default (otherwise uses TEAM_6238_DEFAULTS)

        Returns:
            Value from config, or from TEAM_6238_DEFAULTS, or provided default
        """
        # Special case: 'team' is at root level in v2 configs, not in machine config
        if keys and keys[0] == 'team':
            value = self._data
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                    if value is None:
                        break
                else:
                    value = None
                    break

            if value is not None:
                return value

        # Get the default machine config (handles both v1 wrapped and v2 native)
        machine_config = self.get_machine_config(None)

        # Try to get from machine config
        value = machine_config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    break
            else:
                value = None
                break

        # If found in machine config, return it
        if value is not None:
            return value

        # Fall back to Team 6238 defaults
        default_value = TEAM_6238_DEFAULTS
        for key in keys:
            if isinstance(default_value, dict):
                default_value = default_value.get(key)
                if default_value is None:
                    break
            else:
                default_value = None
                break

        # Return default_value from TEAM_6238_DEFAULTS, or provided default
        return default_value if default_value is not None else default

    # ========================================================================
    # Machine Management (v2 Config Support)
    # ========================================================================

    def get_available_machines(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all available machines from config.

        Returns:
            Dictionary mapping machine_id to machine info (name, machine config, etc.)
        """
        return self._data.get('machines', {})

    @property
    def default_machine_id(self) -> str:
        """Get default machine ID"""
        return self._data.get('default_machine', 'default')

    def get_machine_config(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get config for a specific machine.

        Args:
            machine_id: Machine ID, or None for default machine

        Returns:
            Machine configuration dict
        """
        if machine_id is None:
            machine_id = self.default_machine_id

        machines = self._data.get('machines', {})
        return machines.get(machine_id, machines.get(self.default_machine_id, {}))

    # ========================================================================
    # Team Information
    # ========================================================================

    @property
    def team_number(self) -> int:
        """FRC team number"""
        return self._get('team', 'number')

    @property
    def team_name(self) -> str:
        """FRC team name"""
        return self._get('team', 'name')

    # ========================================================================
    # Machine Configuration
    # ========================================================================

    @property
    def machine_name(self) -> str:
        """Machine model name"""
        return self._get('machine', 'name')

    @property
    def machine_manufacturer(self) -> str:
        """Machine manufacturer"""
        return self._get('machine', 'manufacturer')

    @property
    def machine_controller(self) -> str:
        """Machine controller type (Mach3, Mach4, LinuxCNC, etc.)"""
        return self._get('machine', 'controller')

    @property
    def machine_park_x(self) -> float:
        """Machine park X position (machine coordinates)"""
        return self._get('machine', 'park_position', 'x')

    @property
    def machine_park_y(self) -> float:
        """Machine park Y position (machine coordinates)"""
        return self._get('machine', 'park_position', 'y')

    @property
    def machine_park_z(self) -> float:
        """Machine park Z position (machine coordinates, safe clearance)"""
        return self._get('machine', 'park_position', 'z')

    @property
    def park_position(self):
        """Optional machine-coordinate park as (x, y, z), or None if not configured.

        Only when this is set does the post-processor emit the G53 machine-coordinate
        park (raise Z, move gantry to a fixed spot). Absent -> no G53 anywhere, so the
        output stays portable across controllers (GRBL/Easel/WinCNC)."""
        x = self._get('machine', 'park_position', 'x')
        y = self._get('machine', 'park_position', 'y')
        z = self._get('machine', 'park_position', 'z')
        if x is None or y is None or z is None:
            return None
        return (x, y, z)

    @property
    def safe_clearance_height(self):
        """Configured "clear everything" height over Z=0 (sacrifice board) for the G54
        bed-crossing moves (first-cut approach, end retract, multi-part rapids, tube-flip
        pause). Set above the tallest fixture. None -> fall back to material_thickness +
        clearance (just above the stock). From z_reference.safe_height."""
        return self._get('machining', 'z_reference', 'safe_height', default=None)

    @property
    def tool_change_height(self):
        """Optional work-coordinate height reserved for manual tool changes.

        This is deliberately separate from ``safe_height``: a shop may want routine
        bed-crossing moves kept low while still lifting the spindle far enough to get
        two wrenches around an Omio collet.  It is measured above the sacrifice-board
        datum and shifted when stock-top Z is selected, exactly like ``safe_height``.
        ``None`` keeps the normal safe retract (or a configured G53 park) unchanged.
        """
        return self._get('machining', 'z_reference', 'tool_change_height', default=None)

    @property
    def machine_coolant(self) -> str:
        """Machine coolant type (Air, Flood, Mist) or None. Opt-in: no value -> no coolant
        M-codes are emitted (portable across controllers)."""
        return self._get('machine', 'coolant')

    @property
    def tube_work_coordinate_system(self) -> str:
        """Work coordinate system the tube jig is zeroed in. Defaults to 'G54' (portable:
        the operator zeros G54 to the tube for each job, exactly like flat work). Teams with
        a permanently-fixtured jig can set an alternate FIXED WCS (e.g. 'G55') so the jig
        zero persists in its own coordinate system while G54 stays their per-stock flat zero.
        Only G54-G59 (the bank supported by GRBL/Mach/WinCNC) are honored; anything else
        (including an unset G59.x sub-system) falls back to 'G54'."""
        value = self._get('machine', 'tube_work_coordinate_system')
        if value is None:
            return 'G54'
        normalized = str(value).strip().upper()
        return normalized if normalized in {'G54', 'G55', 'G56', 'G57', 'G58', 'G59'} else 'G54'

    @property
    def machine_x_max(self) -> float:
        """Machine maximum X travel (inches), for the DEFAULT machine."""
        return self._get('machine', 'dimensions', 'x_max')

    @property
    def machine_y_max(self) -> float:
        """Machine maximum Y travel (inches), for the DEFAULT machine."""
        return self._get('machine', 'dimensions', 'y_max')

    def machine_travel(self, machine_id: Optional[str] = None):
        """(x_max, y_max) travel in inches for one machine, not just the default one.

        The `machine_x_max` / `machine_y_max` properties resolve through `_get`, which
        always reads the default machine. A shop with two machines that posts a job for
        the smaller one therefore had its layout checked against the bigger one's
        envelope - a program accepted here runs into the limits there.
        """
        if machine_id is None:
            return self.machine_x_max, self.machine_y_max
        dims = (self.get_machine_config(machine_id) or {}).get('machine', {}) \
                   .get('dimensions', {}) or {}
        x = parse_length(dims.get('x_max'))
        y = parse_length(dims.get('y_max'))
        return (x if x else self.machine_x_max), (y if y else self.machine_y_max)

    @property
    def machine_z_max(self) -> float:
        """Machine maximum Z travel (inches)"""
        return self._get('machine', 'dimensions', 'z_max')

    # ========================================================================
    # General Machining Preferences
    # ========================================================================

    @property
    def sacrifice_board_depth(self) -> float:
        """How far to cut into sacrifice board (inches)"""
        return self._get('machining', 'z_reference', 'sacrifice_board_depth')

    @property
    def clearance_height(self) -> float:
        """Clearance above material for rapid moves (inches)"""
        return self._get('machining', 'z_reference', 'clearance_height')

    @property
    def z_datum(self) -> str:
        """Which surface the operator zeros Z on by default: 'board' (the sacrifice
        board - what every PenguinCAM program has used, and what the setup docs assume)
        or 'stock_top'. From machining.z_reference.datum; a job can still override it.

        An unrecognised value falls back to the board datum with a warning rather than
        refusing to start: a typo in one team's config should not take the app down, and
        the board datum is the behaviour every other part of the setup already assumes."""
        raw = self._get('machining', 'z_reference', 'datum', default=None)
        if raw is None:
            return 'board'
        try:
            from frc_cam_postprocessor import normalize_z_datum
            return normalize_z_datum(raw)
        except (ImportError, ValueError) as e:
            print(f"⚠️  machining.z_reference.datum: {e}. Using the sacrifice board.")
            return 'board'

    @property
    def tab_width(self) -> float:
        """Default tab width (inches)"""
        return self._get('machining', 'tabs', 'width')

    @property
    def tab_height(self) -> float:
        """Default tab height (inches)"""
        return self._get('machining', 'tabs', 'height')

    @property
    def tab_spacing(self) -> float:
        """Default desired tab spacing (inches)"""
        return self._get('machining', 'tabs', 'spacing')

    @property
    def tabs_enabled(self) -> bool:
        """Whether tabs are enabled for perimeter cutting"""
        return self._get('machining', 'tabs', 'enabled')

    @property
    def remove_tabs(self) -> bool:
        """Whether to automatically remove tabs at end of job"""
        return self._get('machining', 'tabs', 'remove_tabs')

    @property
    def pause_before_perimeter(self) -> bool:
        """Whether to pause before cutting perimeter (for screw fixturing)"""
        return self._get('machining', 'fixturing', 'pause_before_perimeter')

    @property
    def hole_detection_tolerance(self) -> float:
        """Tolerance for detecting circular holes (inches)"""
        return self._get('machining', 'holes', 'detection_tolerance')

    @property
    def min_millable_hole_multiplier(self) -> float:
        """Minimum hole diameter as multiple of tool diameter"""
        return self._get('machining', 'holes', 'min_millable_multiplier')

    def _raw_default_tool_diameter(self, machine_id: Optional[str] = None):
        """Raw default-tool diameter (may be a unit string). Checked at the machine top
        level (where the config docs/template put `default_tool`) and then nested under
        `machining` (the defaults location), falling back to the 6238 default."""
        machine_config = self.get_machine_config(machine_id)
        for path in (('default_tool', 'diameter'), ('machining', 'default_tool', 'diameter')):
            value = machine_config
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
                if value is None:
                    break
            if value is not None:
                return value
        return TEAM_6238_DEFAULTS['machining']['default_tool']['diameter']

    @property
    def default_tool_diameter(self) -> float:
        """Default tool diameter in inches - used as UI default. Accepts a unit string
        (e.g. "4mm") or a number; falls back to 4mm if missing or unparseable."""
        parsed = parse_length(self._raw_default_tool_diameter())
        return parsed if parsed and parsed > 0 else DEFAULT_TOOL_DIAMETER_IN

    # ========================================================================
    # Saved bits (the shop's cutters)
    # ========================================================================

    #: Bits live at the ROOT of the config, beside `team:` and `machines:`, because a
    #: cutter belongs to the shop rather than to one machine - and because a top-level
    #: block is the one thing the app can rewrite without touching a line of the
    #: hand-written config around it (see local_mode.save_tools_to_config).
    SAVED_TOOLS_KEY = 'tools'

    @property
    def saved_tools(self) -> List[Dict[str, Any]]:
        """The team's saved bits, validated and in file order.

        A malformed entry is dropped with a warning rather than raising: the tool list is
        a convenience, and one bad line typed into the YAML should not stop the app from
        starting or hide the other nine cutters.
        """
        # Root level only. A per-machine fallback was speculative and actively harmful:
        # the writer emits a single global `tools:`, so one save hoisted the default
        # machine's library into a block that then shadowed every other machine's.
        raw = self._data.get(self.SAVED_TOOLS_KEY)
        if not isinstance(raw, list):
            if raw is not None:
                print(f"⚠️  config `{self.SAVED_TOOLS_KEY}` should be a list of bits; ignoring it")
            return []
        tools, seen = [], set()
        for index, entry in enumerate(raw, start=1):
            tool = self._normalize_saved_tool(entry, index)
            if tool is None:
                continue
            if tool['id'] in seen:
                print(f"⚠️  saved bit {index} duplicates \"{tool['name']}\"; keeping the first")
                continue
            seen.add(tool['id'])
            tools.append(tool)
        return tools

    @staticmethod
    def _normalize_saved_tool(entry, index):
        """One saved bit -> the shape the wizard and the job spec both speak, or None."""
        if not isinstance(entry, dict):
            print(f"⚠️  saved bit {index} is not a mapping; ignoring it")
            return None
        name = str(entry.get('name') or '').strip()
        raw_diameter = entry.get('diameter')
        diameter = parse_length(raw_diameter)
        if not name:
            print(f"⚠️  saved bit {index} has no name; ignoring it")
            return None
        if not diameter or diameter <= 0:
            print(f"⚠️  saved bit \"{name}\" has no usable diameter ({raw_diameter!r}); ignoring it")
            return None
        tool_type = str(entry.get('type') or 'endmill').strip().lower()
        if tool_type not in ('endmill', 'vbit', 'drill'):
            print(f"⚠️  saved bit \"{name}\" has unknown type {tool_type!r}; treating it as an endmill")
            tool_type = 'endmill'
        try:
            flutes = int(entry.get('flutes') or 1)
        except (TypeError, ValueError):
            flutes = 1
        tool = {
            'id': slugify_tool_id(name),
            'name': name,
            'diameter': diameter,
            # The text as written, so "6mm" shows as 6mm rather than 0.2362".
            'diameter_text': raw_diameter if isinstance(raw_diameter, str) else f'{diameter:g}"',
            'flutes': max(1, flutes),
            'type': tool_type,
            'source': 'team',
        }
        angle = entry.get('included_angle')
        if tool_type == 'vbit':
            try:
                tool['included_angle'] = float(angle) if angle is not None else 90.0
            except (TypeError, ValueError):
                print(f"⚠️  saved bit \"{name}\" has an unreadable V angle; using 90 deg")
                tool['included_angle'] = 90.0
        elif angle is not None:
            try:
                tool['included_angle'] = float(angle)
            except (TypeError, ValueError):
                pass
        return tool

    #: Stock lives at the root beside `tools:` for the same reason: a sheet in the rack
    #: belongs to the shop, not to one machine.
    SAVED_STOCK_KEY = 'stock'

    @property
    def saved_stock(self) -> List[Dict[str, Any]]:
        """Sheets and offcuts the shop has, validated and in file order.

        Same forgiving contract as saved_tools: one malformed entry costs that sheet,
        not the list and not the app's startup.
        """
        raw = self._data.get(self.SAVED_STOCK_KEY)
        if not isinstance(raw, list):
            if raw is not None:
                print(f"⚠️  config `{self.SAVED_STOCK_KEY}` should be a list of sheets; ignoring it")
            return []
        sheets, seen = [], set()
        for index, entry in enumerate(raw, start=1):
            sheet = self._normalize_stock(entry, index)
            if sheet is None:
                continue
            if sheet['id'] in seen:
                print(f"⚠️  stock {index} duplicates \"{sheet['name']}\"; keeping the first")
                continue
            seen.add(sheet['id'])
            sheets.append(sheet)
        return sheets

    @staticmethod
    def _normalize_stock(entry, index):
        """One stock entry -> the shape the layout and the job spec both speak, or None."""
        if not isinstance(entry, dict):
            print(f"⚠️  stock {index} is not a mapping; ignoring it")
            return None
        name = str(entry.get('name') or '').strip()
        width = parse_length(entry.get('width'))
        height = parse_length(entry.get('height'))
        if not name:
            print(f"⚠️  stock {index} has no name; ignoring it")
            return None
        if not width or not height or width <= 0 or height <= 0:
            print(f"⚠️  stock \"{name}\" has no usable size; ignoring it")
            return None
        sheet = {
            'id': slugify_tool_id(name),
            'name': name,
            'width': width,
            'height': height,
            'width_text': entry.get('width') if isinstance(entry.get('width'), str) else f'{width:g}"',
            'height_text': entry.get('height') if isinstance(entry.get('height'), str) else f'{height:g}"',
            # A remnant is an offcut: same thing, but worth showing separately so the
            # shop uses one up before opening a fresh sheet.
            'remnant': bool(entry.get('remnant')),
            'source': 'team',
        }
        thickness = parse_length(entry.get('thickness'))
        if thickness and thickness > 0:
            sheet['thickness'] = thickness
            sheet['thickness_text'] = (entry.get('thickness')
                                       if isinstance(entry.get('thickness'), str)
                                       else f'{thickness:g}"')
        material = str(entry.get('material') or '').strip()
        if material:
            sheet['material'] = material
        return sheet

    def default_material_for(self, machine_id: Optional[str] = None) -> str:
        """Material id the UI opens on for this machine (machining.default_material).

        Read from the machine's own config first, the way default_tool is: _get does not
        descend into a per-machine block for this key, so a machine that names its own
        default would otherwise be ignored. Falls back to the 6238 default, and then to
        any material the machine actually has - a default naming a material with no
        feeds on this machine is worse than no default at all."""
        machine_config = self.get_machine_config(machine_id)
        chosen = None
        for path in (('machining', 'default_material'), ('default_material',)):
            value = machine_config
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
                if value is None:
                    break
            if value is not None:
                chosen = value
                break
        available = self.get_available_materials(machine_id)
        if chosen and chosen in available:
            return chosen
        for fallback in (TEAM_6238_DEFAULTS['machining']['default_material'], 'aluminum'):
            if fallback in available:
                return fallback
        return next(iter(available), 'aluminum')

    @property
    def default_material(self) -> str:
        """Material id the UI opens on, for the default machine."""
        return self.default_material_for()

    @property
    def default_tool_diameter_text(self) -> str:
        """Default tool diameter as the raw display text (e.g. "4mm"), so the UI can show
        it verbatim instead of a translated inch value. Bare numbers get an inch mark."""
        raw = self._raw_default_tool_diameter()
        return raw if isinstance(raw, str) else f'{raw}"'

    # ========================================================================
    # Tube Facing Parameters
    # ========================================================================

    def get_tube_facing_params(self) -> Dict[str, Any]:
        """Get all tube facing parameters as a dict"""
        return {
            'depth_margin': self._get('tube_facing', 'depth_margin'),
            'max_roughing_depth': self._get('tube_facing', 'max_roughing_depth'),
            'max_finishing_depth': self._get('tube_facing', 'max_finishing_depth'),
            'roughing_tool_edge_p1': self._get('tube_facing', 'phase_1', 'roughing_tool_edge'),
            'finishing_tool_edge_p1': self._get('tube_facing', 'phase_1', 'finishing_tool_edge'),
            'roughing_tool_edge_p2': self._get('tube_facing', 'phase_2', 'roughing_tool_edge'),
            'finishing_tool_edge_p2': self._get('tube_facing', 'phase_2', 'finishing_tool_edge'),
            'arc_advance': self._get('tube_facing', 'arc_advance'),
            'arc_radius': self._get('tube_facing', 'arc_radius')
        }

    # ========================================================================
    # Material Presets
    # ========================================================================

    def get_available_materials(self, machine_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        Get all available materials for a specific machine.

        Args:
            machine_id: Machine ID, or None for default machine

        Returns:
            Dictionary mapping material ID to material info (with 'name' and other params)
        """
        machine_config = self.get_machine_config(machine_id)

        # Start with Team 6238 defaults
        materials = dict(TEAM_6238_DEFAULTS['materials'])

        # Add/override with machine-specific materials
        machine_materials = machine_config.get('materials', {})
        for material_id, material_data in machine_materials.items():
            # Get complete material preset (with fallback)
            materials[material_id] = self.get_material_preset(material_id, machine_id)

        return materials

    def is_material_complete(self, material: str, machine_id: Optional[str] = None) -> bool:
        """
        Check if a material has all required parameters defined.

        Args:
            material: Material name
            machine_id: Machine ID, or None for default machine

        Returns:
            True if material has all required params, False if using fallback
        """
        import feeds_speeds

        material_key = ('aluminum' if feeds_speeds.is_aluminum_material(material)
                        else material)
        # Required parameters for a complete material definition
        required_params = {
            'name', 'spindle_speed', 'feed_rate', 'ramp_feed_rate', 'plunge_rate',
            'traverse_rate', 'approach_rate', 'ramp_angle', 'ramp_start_clearance',
            'stepover_percentage', 'helix_radius_multiplier', 'max_slotting_depth',
            'tab_width', 'tab_height'
        }

        # Check if material exists in defaults
        if material_key in TEAM_6238_DEFAULTS['materials']:
            return True

        # Check if machine config has all required parameters
        machine_config = self.get_machine_config(machine_id)
        machine_material = machine_config.get('materials', {}).get(material, {})
        return required_params.issubset(machine_material.keys())

    def get_material_preset(self, material: str, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get material preset parameters for a specific machine with fallback to Team 6238 defaults.

        Args:
            material: Material name ('plywood', 'aluminum', 'polycarbonate', or custom)
            machine_id: Machine ID, or None for default machine

        Returns:
            A complete dictionary of material parameters, or an EMPTY dict when nothing
            - neither this team's config nor the built-in defaults - knows the material.

            The empty dict matters. This used to hand back a full plywood preset
            relabelled with whatever string was asked for, so `--material al6061`
            produced a program that said "Al6061" and ran 75 IPM at 18000 RPM with none
            of the aluminum protections. Refusing is the only safe answer for a
            material nobody has quoted; the caller turns it into an error.
        """
        import feeds_speeds

        requested_material = material
        material_key = ('aluminum' if feeds_speeds.is_aluminum_material(material)
                        else material)
        machine_config = self.get_machine_config(machine_id)

        # Get machine-specific material config
        configured_materials = machine_config.get('materials', {})
        machine_material = (configured_materials.get(requested_material)
                            or configured_materials.get(material_key, {}))

        # Get Team 6238 default for this material
        default_preset = TEAM_6238_DEFAULTS['materials'].get(material_key, {})

        if not default_preset:
            if not machine_material:
                return {}
            # The team explicitly configured this material, so their numbers are the
            # authority; plywood only fills in the fields they left out.
            default_preset = TEAM_6238_DEFAULTS['materials']['plywood'].copy()
            if 'name' not in machine_material:
                machine_material = {**machine_material,
                                    'name': str(material).replace('_', ' ').title()}

        # Merge: defaults → machine overrides
        return {**default_preset, **machine_material}

    def known_material_ids(self, machine_id: Optional[str] = None) -> List[str]:
        """Every material id this config can produce a preset for, sorted.

        Used to make a refusal actionable: telling someone their material is unknown is
        only half the message, the other half is what they could have typed.
        """
        machine_config = self.get_machine_config(machine_id)
        ids = set(TEAM_6238_DEFAULTS['materials'])
        ids.update(machine_config.get('materials', {}))
        return sorted(ids)

    # ========================================================================
    # Integration Settings
    # ========================================================================

    @property
    def google_drive_enabled(self) -> bool:
        """Whether Google Drive integration is enabled for this team"""
        return self._get('integrations', 'google_drive', 'enabled')

    @property
    def google_drive_folder_id(self) -> Optional[str]:
        """
        Google Drive folder ID for uploading G-code.
        Accepts either a folder ID or a full Drive URL, returns just the ID.
        """
        folder_value = self._get('integrations', 'google_drive', 'folder_id')

        if not folder_value:
            return None

        # If it's a full URL, extract the ID
        if 'drive.google.com' in folder_value:
            # Format: https://drive.google.com/drive/folders/FOLDER_ID
            # or: https://drive.google.com/drive/u/0/folders/FOLDER_ID
            parts = folder_value.split('/folders/')
            if len(parts) == 2:
                # Remove any query parameters or trailing slashes
                folder_id = parts[1].split('?')[0].rstrip('/')
                return folder_id

        # Otherwise assume it's already just the ID
        return folder_value

    # ========================================================================
    # Helpers
    # ========================================================================

    def to_dict(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Return config as a dictionary for JSON serialization.

        Args:
            machine_id: Machine ID, or None for default machine

        Returns:
            Dictionary with machine-specific settings
        """
        machine_config = self.get_machine_config(machine_id)

        # Helper to get machine-specific setting with fallback
        def get_machine_setting(*keys, default=None):
            value = machine_config
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                    if value is None:
                        break
                else:
                    value = None
                    break
            # Fallback to TEAM_6238_DEFAULTS if not in machine config
            if value is None:
                fallback = TEAM_6238_DEFAULTS
                for key in keys:
                    if isinstance(fallback, dict):
                        fallback = fallback.get(key)
                        if fallback is None:
                            break
                    else:
                        fallback = None
                        break
                value = fallback if fallback is not None else default
            return value

        # Tool diameter for this machine: parse to inches, and keep the raw text for the
        # UI to show verbatim (e.g. "4mm"). Falls back to 4mm via TEAM_6238_DEFAULTS.
        raw_tool = self._raw_default_tool_diameter(machine_id)
        tool_in = parse_length(raw_tool)
        return {
            'team_number': self.team_number,
            'team_name': self.team_name,
            'machine_name': get_machine_setting('machine', 'name'),
            'machine_controller': get_machine_setting('machine', 'controller'),
            'machine_x_max': get_machine_setting('machine', 'dimensions', 'x_max'),
            'machine_y_max': get_machine_setting('machine', 'dimensions', 'y_max'),
            'machine_z_max': get_machine_setting('machine', 'dimensions', 'z_max'),
            'google_drive_enabled': get_machine_setting('integrations', 'google_drive', 'enabled'),
            'google_drive_folder_id': get_machine_setting('integrations', 'google_drive', 'folder_id'),
            'default_tool_diameter': tool_in if (tool_in and tool_in > 0) else DEFAULT_TOOL_DIAMETER_IN,
            'default_tool_diameter_text': raw_tool if isinstance(raw_tool, str) else f'{raw_tool}"',
            'default_material': self.default_material_for(machine_id),
        }

    @classmethod
    def from_yaml(cls, yaml_content: str) -> 'TeamConfig':
        """
        Create TeamConfig from YAML string.

        Args:
            yaml_content: YAML content as string

        Returns:
            TeamConfig instance (falls back to Team 6238 defaults on parse error)
        """
        try:
            data = yaml.safe_load(yaml_content)
            return cls(data)
        except yaml.YAMLError as e:
            print(f"⚠️  Error parsing team config YAML: {e}")
            print("   Using Team 6238 defaults")
            return cls()

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> 'TeamConfig':
        """
        Create TeamConfig from dictionary (e.g., from session storage).

        Args:
            config_dict: Configuration dictionary

        Returns:
            TeamConfig instance
        """
        return cls(config_dict)

    def __repr__(self):
        return f"TeamConfig(team={self.team_number}, name='{self.team_name}')"


# =============================================================================
# YAML TEMPLATE
# =============================================================================

CONFIG_TEMPLATE = """# PenguinCAM Team Configuration
# This file defines machine-specific settings and machining preferences
#
# All values are optional - any missing values will use Team 6238 defaults.
# You only need to specify values you want to override.
#
# UNITS: Dimensions default to inches, but you may add a unit to any dimension
# value (quote it so YAML reads it as text): e.g. diameter: "4mm", x_max: "600mm",
# width: "0.25in", or a fraction like "1/8". Supported units: in/", mm, cm, m, ft/', yd.
# A plain number (no quotes/unit) is inches. Non-dimension values (feed rates, RPM,
# angles, ratios) are always plain numbers.

# =============================================================================
# TEAM INFORMATION
# =============================================================================
team:
  number: 6238
  name: "Popcorn Penguins"

# =============================================================================
# MACHINE & CONTROLLER
# =============================================================================
machine:
  name: "Avid CNC Pro4896"           # Your machine model
  manufacturer: "Avid CNC"
  controller: "Mach4"                 # Mach3, Mach4, LinuxCNC, etc.

  # Machine work envelope. Plain numbers are inches; add a unit to use metric,
  # e.g. x_max: "1200mm" (quote unit values). See the UNITS note at the top.
  dimensions:
    x_max: 48.0
    y_max: 96.0
    z_max: 8.0

  # OPTIONAL end-of-program park in MACHINE coordinates (moves the gantry out of the way
  # for part access). This is the ONLY thing that puts G53 in the output, so OMIT this
  # whole block on controllers that don't support G53 (e.g. GRBL/Easel). Machine-specific.
  park_position:
    x: 0.5      # X position when parking (machine coords)
    y: 23.5     # Y position when parking (machine coords)
    z: -0.5     # safe machine Z (machine coords) to raise to before parking

  # OPTIONAL work coordinate system for TUBE operations. Default (omit this line): tube
  # jobs use G54, so the operator zeros G54 to each tube just like flat work - fully
  # portable. If you have a PERMANENTLY-FIXTURED tube jig, set an alternate fixed WCS
  # (G54-G59) so the jig zero persists in its own system while G54 stays your per-stock
  # flat zero. That fixed WCS must be pre-set in your controller (e.g. G10 L2 P2 for G55);
  # an unset WCS defaults to machine zero and will cut in the wrong place.
  tube_work_coordinate_system: "G55"

  # OPTIONAL coolant. If set (Air/Mist -> M7, Flood -> M8, with M9 off), coolant M-codes
  # are emitted. OMIT this line (or use None) on controllers without coolant M-codes
  # (stock GRBL rejects M7 unless compiled in). Air keeps an air-blast on/off.
  coolant: "Air"

# =============================================================================
# GENERAL MACHINING PREFERENCES
# =============================================================================
machining:
  # Z-axis reference system
  z_reference:
    sacrifice_board_depth: 0.008    # How far to cut into sacrifice board (inches)
    clearance_height: 0.5           # Clearance above material for rapid moves (inches)
    # OPTIONAL roomy G54 height used only for manual tool changes. Keep it within
    # verified Z travel; a configured G53 park takes precedence.
    # tool_change_height: 2.0
    # OPTIONAL: which surface the operator zeros Z on, and so what every Z in the
    # program is measured from. "sacrifice_board" (the default) or "stock_top".
    # The wizard can override it per job; tube jobs always use their jig zero.
    # datum: sacrifice_board
    # OPTIONAL: "clear everything" height above Z=0 (sacrifice board), in work
    # coordinates (G54). Used only for the moves that cross the machine bed: the
    # rapid to the first cut, the end retract, multi-part rapids, and the tube-flip
    # pause. Set this ABOVE YOUR TALLEST CLAMP/FIXTURE so those moves clear it.
    # If omitted, those moves fall back to material_thickness + clearance_height
    # (just above the stock) -- fine for edge clamps, but it will NOT clear tall
    # fixturing. Mid-part retracts always use material_thickness + clearance_height
    # regardless, since they stay over the stock.
    # safe_height: 2.0

  # Tab parameters (for perimeter operations)
  tabs:
    width: 0.25                     # Tab width (inches)
    height: 0.1                     # How much material to leave in tab (inches)
    spacing: 6.0                    # Desired spacing between tabs (inches)
    # Note: Actual spacing may be closer to ensure minimum 3 tabs

  # Hole detection and processing
  holes:
    detection_tolerance: 0.02       # Tolerance for detecting circular holes (inches)
    min_millable_multiplier: 1.2    # Minimum hole diameter as multiple of tool diameter
    # Note: Holes smaller than tool_diameter * 1.2 are skipped

  # Tool parameters (defaults - can be overridden per job)
  default_tool:
    diameter: "4mm"                 # e.g. "4mm", "1/8", or a plain number in inches (0.157)
    # Note: This sets the default in the UI, but user can override for each job

# =============================================================================
# TUBE FACING OPERATION PARAMETERS
# =============================================================================
tube_facing:
  # Depth calculations
  depth_margin: 0.005               # Extra depth beyond half tube height (inches)

  # Multi-pass depth limits
  max_roughing_depth: 0.3           # Maximum depth per roughing pass (inches)
  max_finishing_depth: 0.51         # Maximum depth per finishing pass (inches)

  # Tool edge positions for two-pass flip strategy
  # These are the Y positions where the tool leaves the final face
  phase_1:
    roughing_tool_edge: 0.05        # Phase 1 roughing position (inches)
    finishing_tool_edge: 0.0625     # Phase 1 finishing position (inches)

  phase_2:
    roughing_tool_edge: -0.0125     # Phase 2 roughing position (inches)
    finishing_tool_edge: 0.0        # Phase 2 finishing position (inches)

  # Arc clearing parameters
  arc_advance: 0.04                 # Arc advance distance in X (inches)
  arc_radius: 0.05                  # Arc radius for clearing moves (inches)

# =============================================================================
# MATERIAL-SPECIFIC SETTINGS
# =============================================================================
# You can override any or all materials. Only specify values you want to change.
# Any missing values will use Team 6238 defaults for that material.

materials:
  plywood:
    name: "Plywood"
    description: "Standard plywood settings - 18K RPM, 75 IPM cutting"

    # Speeds and feeds
    spindle_speed: 18000            # RPM
    feed_rate: 75.0                 # Cutting feed rate (IPM)
    ramp_feed_rate: 50.0            # Ramp feed rate (IPM)
    plunge_rate: 35.0               # Plunge feed rate for tab Z moves (IPM)
    traverse_rate: 200.0            # Lateral moves above material (IPM)
    approach_rate: 50.0             # Z approach to ramp start (IPM)

    # Toolpath parameters
    ramp_angle: 20.0                # Ramp angle in degrees
    ramp_start_clearance: 0.150     # Clearance above material to start ramping (inches)
    stepover_percentage: 0.65       # Radial stepover as fraction of tool diameter
    helix_radius_multiplier: 0.75   # Helix entry radius as fraction of tool radius

    # Multi-pass parameters
    max_slotting_depth: 0.4         # Maximum depth per pass for perimeter slotting (inches)

    # Tab parameters (can override defaults)
    tab_width: 0.25
    tab_height: 0.15

  aluminum:
    name: "Aluminum"
    description: "6061/6063 on an Omio router - protected tool-adjusted envelope"

    # Speeds and feeds
    spindle_speed: 18000
    feed_rate: 30.0
    ramp_feed_rate: 19.0
    plunge_rate: 15.0               # Slower for aluminum
    traverse_rate: 200.0
    approach_rate: 35.0

    # Toolpath parameters
    ramp_angle: 4.0                 # Shallow ramp for aluminum
    ramp_start_clearance: 0.050
    stepover_percentage: 0.25       # Conservative for aluminum
    helix_radius_multiplier: 0.5    # Conservative helix entry for aluminum

    # Multi-pass parameters
    max_slotting_depth: 0.06        # Safety ceiling for a 4mm cutter; smaller tools scale down
    peck_drill_depth: 0.05          # Peck ceiling; generated twist drills use D/3

    # OPTIONAL: feed floor at sharp pocket corners, as a fraction of feed_rate. At a sharp
    # corner the cutter wraps two edges and engagement spikes, so we ease the feed down (the
    # toolpath itself is unchanged). Aluminum cannot be slowed below a real chip, so the
    # protected default is 0.6 and RPM is coordinated with it. Softer materials use ~0.7.
    corner_min_feed_scale: 0.6

    # Tab parameters
    tab_width: 0.25
    tab_height: 0.15

  polycarbonate:
    name: "Polycarbonate"
    description: "Polycarbonate - same as plywood settings"

    # Speeds and feeds
    spindle_speed: 18000
    feed_rate: 75.0
    ramp_feed_rate: 50.0
    plunge_rate: 20.0
    traverse_rate: 200.0
    approach_rate: 50.0

    # Toolpath parameters
    ramp_angle: 20.0
    ramp_start_clearance: 0.100
    stepover_percentage: 0.55       # Moderate for polycarbonate
    helix_radius_multiplier: 0.75

    # Multi-pass parameters
    max_slotting_depth: 0.25

    # Tab parameters
    tab_width: 0.25
    tab_height: 0.15

# =============================================================================
# GOOGLE DRIVE INTEGRATION (optional)
# =============================================================================
integrations:
  google_drive:
    enabled: true
    folder_id: "https://drive.google.com/drive/folders/YOUR_FOLDER_ID"
    # To get your folder URL:
    # 1. Open Google Drive in your browser
    # 2. Navigate to: Shared drives → Your Team → CNC → G-code
    # 3. Copy the full URL from your browser (looks like above)
    # 4. Paste it here (you can paste either the full URL or just the folder ID)

# =============================================================================
# UI CUSTOMIZATION (optional - for future use)
# =============================================================================
ui:
  theme: "default"
  # Future: team logo, colors, branding
  # logo_url: "https://your-team-website.com/logo.png"
  # primary_color: "#FF6B35"
"""
