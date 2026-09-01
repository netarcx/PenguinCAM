"""Multi-tool operation model for PenguinCAM.

A single-tool program is the common case and stays exactly as it was: one
`FRCPostProcessor`, one tool diameter, one pass through `generate_gcode()`. This module
adds the other case - a part that needs *several* tools (drill the small holes, clear the
pockets with a big cutter, profile, break the edges with a V-tool) - without threading a
per-feature tool parameter through the 5,000-line post-processor.

The trick is that a tool is fixed at `FRCPostProcessor` construction, and almost
everything the post-processor derives (cutter compensation, minimum millable hole, helix
radius, stepover, contour-vs-clear threshold, corner slowdown) falls out of that one
number. So instead of making one post-processor cut with many tools, we build **one
post-processor per operation**, each already correct for its own tool, let each generate
just the features in its scope, and stitch the bodies together here with tool-change
blocks between them. Every toolpath in a multi-tool program therefore comes out of the
same tested code paths as the single-tool program.

Layout of the pipeline::

    survey_part()         one throwaway post-processor per part, smallest tool loaded,
                          reports the holes/pockets/perimeter available to scope ops to
    generate_operation()  one post-processor per (part, operation): load, place, filter
                          the features down to the operation's scope, emit that body
    order_operations()    flatten (part, operation) pairs into one sequence that groups
                          work by tool WITHOUT reordering any part's own operations
    assemble_job()        header + bodies + a manual tool-change pause at each switch

Tool changes are manual: the program parks, stops the spindle, prints what to load, and
waits on M0 for CYCLE START. There is no T/M6 or G43 in the output, because the routers
this targets have no tool changer and no tool-length offset table - the operator re-zeros
Z to the job's zero surface (the sacrifice board by default, the stock top if the job asks
for it) after each swap, which is also why X/Y zero must not be touched.

Feeds and speeds are re-derived per tool from `feeds_speeds` rather than taken from the
material preset, since the presets are all tuned for one 4 mm single-flute cutter and stop
being meaningful the moment the diameter or flute count changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from shapely.geometry import Polygon
from shapely.ops import polylabel

import drill_sizes
import feeds_speeds
from frc_cam_postprocessor import (
    ENGRAVE_DEPTH_IN,
    ENGRAVE_HEIGHT_IN,
    FRCPostProcessor,
    PostProcessorResult,
    build_output_filename,
    normalize_z_datum,
    sanitize_comment,
)
from team_config import TeamConfig

# --------------------------------------------------------------------------- vocabulary

TOOL_TYPES = ('endmill', 'vbit', 'drill')

#: Operation kinds. `holes`/`pockets`/`interior` cut interior features, `perimeter` cuts
#: the outer profile with tabs, `chamfer` breaks top edges with a V-tool.
OP_TYPES = ('holes', 'pockets', 'interior', 'perimeter', 'chamfer')

OP_LABELS = {
    'holes': 'Holes',
    'pockets': 'Pockets',
    'interior': 'Interior features',
    'perimeter': 'Perimeter',
    'chamfer': 'Chamfer',
}

#: Operations whose cut depth is not the user's to choose (see Operation.depth).
DEPTHLESS_OP_TYPES = ('perimeter', 'chamfer')

#: How far a drawn hole may sit from a drill's diameter and still be drilled with it.
#: A twist drill makes exactly one size of hole - its own - so a drawing that asks for
#: 0.196 and a crib that holds a #10 (0.1935) or a 13/64 (0.2031) have to meet somewhere.
#: 0.010 in is under a 64th, which is the granularity a fractional drill index actually
#: offers, and comfortably inside the clearance an FRC bolt hole is drawn with. Beyond
#: this the difference is a real design intent, not a stocking gap, so it is an error.
#: Override per operation with `scope: {size_tolerance: ...}`.
DEFAULT_DRILL_SIZE_TOLERANCE = 0.010

#: Ceilings on job size. Each operation builds its own post-processor, which re-loads and
#: re-processes the whole DXF - about 0.2 s on a 2,000-circle file - so cost is roughly
#: O(parts x operations) where /process-job is O(parts). At 10 requests/minute an
#: unbounded job is minutes of CPU per request. These are far above any real plate (the
#: shipped example uses 5 operations) and exist only to stop a runaway.
MAX_OPERATIONS_PER_JOB = 120
MAX_PARTS_PER_JOB = 60

#: How close a drill must be to the correct TAP drill to count as it. Independent of
#: the clearance-snap tolerance above, and much tighter, because a tap drill is not a
#: size you round to: at +/-0.010 a 10-32 accepted #25 (0.1495) through #19 (0.1660),
#: five drill sizes. The wrong end strips the threads; the other end breaks the tap.
#: drill_sizes.tap_drill_for already works to 0.002, so acceptance matches it.
TAP_DRILL_TOLERANCE = 0.002

#: The most a per-operation or job-level `size_tolerance` may widen tap acceptance to.
#: Widening the CLEARANCE tolerance is legitimate - a shop stocking fractional drills
#: only genuinely substitutes across a few thou - but the same number must not be
#: allowed to loosen a tap drill into the next thread's size.
MAX_TAP_DRILL_TOLERANCE = 0.003

#: Beyond this the difference between the drawn hole and the drill is big enough to
#: matter to whatever goes through it, so it is called out with the consequence even
#: when the configured tolerance allows it. Half a 64th.
DRILL_SIZE_WARN_THRESHOLD = 0.008

#: Below this the substitution is not worth mentioning - it is DXF/unit-conversion noise
#: rather than a hole that will measure differently from the drawing.
DRILL_SIZE_NOTE_THRESHOLD = 0.0005

#: What each operation type actually does to the stock, so a tool can be checked against
#: it. A V-tool has no flat bottom and no side flutes worth the name: asked to clear a
#: pocket it plunges on its point and leaves a field of grooves where a floor should be.
#: A drill only cuts on its tip and must not be fed sideways at all.
MILLING_OP_TYPES = ('holes', 'pockets', 'interior', 'perimeter')

#: Which `feeds_speeds` operation model each op kind is cutting under. Holes are bored
#: helically or pecked straight down, so the cutter is engaged on all sides - that is
#: slotting, and it gets the material's slotting derate. Pocket clearing steps over at
#: partial engagement and can run the full reference chipload. A profile pass is a slot
#: through the stock; a chamfer is a light finishing pass but is quoted conservatively.
FEEDS_OPERATION = {
    'holes': 'slot',
    'pockets': 'pocket',
    'interior': 'pocket',
    'perimeter': 'profile',
    'chamfer': 'profile',
}

#: `feeds_speeds` material keys differ slightly from PenguinCAM's material ids.
FEEDS_MATERIAL_ALIASES = {
    'aluminum': 'aluminum_6063',
    'aluminum_tube': 'aluminum_6063',
    'polycarb': 'polycarbonate',
    'plywood': 'plywood',
}

DEFAULT_FEEDS_MACHINE = 'omio_x8'

#: End mill assumed when suggesting a plan and the caller names no preference.
#: 4 mm is PenguinCAM's default cutter and what the material presets are tuned for.
DEFAULT_MILL_DIAMETER = 0.157

#: Starter tools offered by the UI. Diameters in inches.
TOOL_LIBRARY = {
    '3mm_1f':  {'name': '3mm 1-flute endmill',   'diameter': 0.118, 'flutes': 1, 'type': 'endmill'},
    '4mm_1f':  {'name': '4mm 1-flute endmill',   'diameter': 0.157, 'flutes': 1, 'type': 'endmill'},
    '125_1f':  {'name': '1/8 in 1-flute endmill', 'diameter': 0.125, 'flutes': 1, 'type': 'endmill'},
    '156_drill': {'name': '5/32 in twist drill',  'diameter': 0.15625, 'flutes': 2, 'type': 'drill'},
    '250_1f':  {'name': '1/4 in 1-flute endmill', 'diameter': 0.250, 'flutes': 1, 'type': 'endmill'},
    '250_2f':  {'name': '1/4 in 2-flute endmill', 'diameter': 0.250, 'flutes': 2, 'type': 'endmill'},
    '375_2f':  {'name': '3/8 in 2-flute endmill', 'diameter': 0.375, 'flutes': 2, 'type': 'endmill'},
    'vbit_90': {'name': '1/2 in 90 deg V-bit',    'diameter': 0.500, 'flutes': 2, 'type': 'vbit',
                'included_angle': 90.0},
    'vbit_60': {'name': '1/2 in 60 deg V-bit',    'diameter': 0.500, 'flutes': 2, 'type': 'vbit',
                'included_angle': 60.0},
    'engrave_vbit_30': {'name': '1/4 in 30 deg engraving V-bit', 'diameter': 0.250,
                        'flutes': 2, 'type': 'vbit', 'included_angle': 30.0},
    'engrave_vbit_60': {'name': '1/4 in 60 deg engraving V-bit', 'diameter': 0.250,
                        'flutes': 2, 'type': 'vbit', 'included_angle': 60.0},
}


def merge_tool_library(saved_tools=None):
    """The bits offered in the UI: the shop's saved cutters first, then the built-ins.

    A saved bit whose id collides with a built-in replaces it - if a team has written
    down what their 1/4 in 2-flute actually is, that is the authority, not the generic
    entry shipped with the app.
    """
    library = {}
    for tool in (saved_tools or []):
        entry = {k: tool[k] for k in ('name', 'diameter', 'flutes', 'type')
                 if k in tool}
        if tool.get('included_angle') is not None:
            entry['included_angle'] = tool['included_angle']
        entry['diameter_text'] = tool.get('diameter_text')
        entry['source'] = 'team'
        library[tool['id']] = entry
    for key, entry in TOOL_LIBRARY.items():
        if key not in library:
            library[key] = dict(entry, source='builtin')
    return library


class ToolingError(ValueError):
    """A multi-tool job/operation spec that cannot be honoured as written."""


# Every number in a job spec eventually becomes a coordinate, a feed, or a depth in the
# G-code. `json.loads` accepts bare NaN and Infinity literals, and float('nan') compares
# false against every bound, so a NaN slips through any `if x <= 0` guard and surfaces
# hundreds of lines later as an unformattable coordinate - or worse, as a real one.
# Everything numeric that reaches a toolpath goes through these two.

def _finite(value: Any, what: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a real number, got {value!r}")
    return number


def _positive_finite(value: Any, what: str) -> float:
    number = _finite(value, what)
    if number <= 0:
        raise ValueError(f"{what} must be greater than zero, got {number:g}")
    return number


def _integer(value: Any, what: str) -> int:
    """A finite whole number, without silently truncating fractions.

    ``int(1.9)`` is ``1``.  That is particularly dangerous for tool slots: a typo in
    an operations file can select a different physical cutter while still producing a
    valid-looking program.  Numeric strings remain accepted because config and form
    values legitimately arrive that way.
    """
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a whole number, got {value!r}")
    number = _finite(value, what)
    if number != math.trunc(number):
        raise ValueError(f"{what} must be a whole number, got {value!r}")
    return int(number)


# ------------------------------------------------------------------------------- models

@dataclass
class Tool:
    """One physical cutter, identified by the slot number the operator loads it as.

    `slot` is only a label for the operator - nothing emits a T word, since the machine
    has no tool changer. Feeds are derived from diameter/flutes unless explicitly
    overridden here, in which case the override wins (an override is how a mentor pins a
    known-good number for an unusual cutter).
    """
    slot: int
    name: str
    diameter: float
    flutes: int = 1
    type: str = 'endmill'
    included_angle: float = 90.0        # V-tools only: full included angle, degrees
    #: Usable cutting length, inches. Optional, because most tool listings state it and
    #: most tool boxes do not. It is the depth this cutter can actually reach; past it
    #: the shank is rubbing the wall, which is how a bit is snapped in a deep pocket.
    flute_length: Optional[float] = None
    spindle_speed: Optional[int] = None  # explicit overrides; None = derive
    feed_rate: Optional[float] = None
    plunge_rate: Optional[float] = None

    def __post_init__(self):
        try:
            self.slot = _integer(self.slot, 'slot')
            self.diameter = _positive_finite(self.diameter, 'diameter')
            self.flutes = _integer(self.flutes, 'flutes')
            self.included_angle = _finite(self.included_angle, 'included angle')
        except (TypeError, ValueError) as exc:
            raise ToolingError(f"Tool {self.name!r} has a bad field: {exc}") from exc
        if self.slot < 1:
            raise ToolingError(f"Tool slot numbers start at 1, got {self.slot}")
        if self.flutes < 1:
            raise ToolingError(f"Tool T{self.slot} needs at least one flute, got {self.flutes}")
        if self.type not in TOOL_TYPES:
            raise ToolingError(f"Tool T{self.slot} has unknown type {self.type!r}; "
                               f"expected one of {', '.join(TOOL_TYPES)}")
        if self.type == 'vbit' and not (0.0 < self.included_angle < 180.0):
            raise ToolingError(f"Tool T{self.slot} is a V-tool, so its included angle must be "
                               f"between 0 and 180 degrees, got {self.included_angle}")

        # Feed/speed overrides go straight into F and S words. A negative, zero, or NaN
        # value here reaches the controller verbatim - a negative F is not a slow cut, it
        # is undefined behaviour on the machine - so they are rejected at the door rather
        # than trusted because someone typed them.
        for field_name, value, limit in (('spindle speed', self.spindle_speed, 100000),
                                         ('feed rate', self.feed_rate, 2000),
                                         ('plunge rate', self.plunge_rate, 2000)):
            if value is None:
                continue
            try:
                checked = _positive_finite(value, field_name)
            except (TypeError, ValueError) as exc:
                raise ToolingError(f"Tool T{self.slot} has a bad {field_name}: {exc}") from exc
            if checked > limit:
                raise ToolingError(f"Tool T{self.slot} has a {field_name} of {checked:g}, "
                                   f"which is past any plausible machine limit ({limit:g}).")
        if self.flute_length is not None:
            try:
                self.flute_length = _positive_finite(self.flute_length, 'flute length')
            except (TypeError, ValueError) as exc:
                raise ToolingError(
                    f"Tool T{self.slot} has a bad flute length: {exc}") from exc
        if self.spindle_speed is not None:
            self.spindle_speed = int(float(self.spindle_speed))
        if self.feed_rate is not None:
            self.feed_rate = float(self.feed_rate)
        if self.plunge_rate is not None:
            self.plunge_rate = float(self.plunge_rate)

        self.name = str(self.name or f'T{self.slot}')

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Tool':
        if not isinstance(data, dict):
            raise ToolingError(f"Expected a tool object, got {type(data).__name__}")
        for required in ('diameter',):
            if data.get(required) is None:
                raise ToolingError(
                    f"Tool {data.get('name') or data.get('slot') or '?'} has no "
                    f"{required}. A blank row in the tool table cannot be used.")
        known = {f: data[f] for f in
                 ('slot', 'name', 'diameter', 'flutes', 'type', 'included_angle',
                  'flute_length', 'spindle_speed', 'feed_rate', 'plunge_rate')
                 if f in data}
        known.setdefault('name', f"T{known.get('slot', '?')}")
        return cls(**known)

    def to_dict(self) -> Dict[str, Any]:
        return {'slot': self.slot, 'name': self.name, 'diameter': self.diameter,
                'flutes': self.flutes, 'type': self.type,
                'included_angle': self.included_angle,
                'flute_length': self.flute_length,
                'spindle_speed': self.spindle_speed, 'feed_rate': self.feed_rate,
                'plunge_rate': self.plunge_rate}

    @property
    def label(self) -> str:
        """Short operator-facing identity, safe for a G-code comment."""
        return sanitize_comment(f"T{self.slot} {self.name}", f"T{self.slot}")

    #: What each tool type IS, in the words an operator (and the audit) reads. A tool
    #: name is free text - "#7 drill", "1/8 EM", "spot" - so the program has to state
    #: the kind separately, or nothing downstream can tell a twist drill from a cutter.
    KIND_NAMES = {'endmill': 'end mill', 'drill': 'twist drill', 'vbit': 'V-bit'}

    @property
    def kind(self) -> str:
        """This tool's type as a phrase, e.g. 'twist drill' or '90 deg V-bit'."""
        name = self.KIND_NAMES.get(self.type, self.type)
        if self.type == 'vbit':
            return f"{self.included_angle:.0f} deg {name}"
        return name

    def description(self) -> str:
        """The line this tool gets in the header's tool table."""
        bits = [
            f"T{self.slot} - {sanitize_comment(self.name, 'tool')}",
            f"{self.diameter:.4f} in diameter",
            f"{self.flutes} flute" + ('s' if self.flutes != 1 else ''),
            self.kind,
        ]
        if self.flute_length:
            bits.append(f"{self.flute_length:.3f} in flute length")
        return ', '.join(bits)


@dataclass
class Operation:
    """One cutting operation on one part, performed with one tool.

    `scope` narrows which of the part's features this operation cuts, so two operations
    can split the holes between a small and a large cutter:

        holes    {'indices': [...]} or {'min_diameter': x, 'max_diameter': y}
        pockets  {'indices': [...]} or {'min_area': x, 'max_area': y}
        chamfer  {'targets': ['perimeter', 'holes', 'pockets'], 'width': 0.02,
                  'indices': [...]}

    `depth` is the depth of cut below the material top, for a pocket that should not go
    all the way through. Left as None the operation cuts through the stock and into the
    sacrifice board, which is the normal case for a flat plate. It applies only to the
    feature-cutting op types: a profile that stops short would not free the part, and a
    chamfer's depth is a consequence of its width and the V-tool's angle, so both reject
    it rather than quietly cutting to the wrong Z.
    """
    op_type: str
    tool_slot: int
    name: str = ''
    depth: Optional[float] = None
    scope: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.op_type not in OP_TYPES:
            raise ToolingError(f"Unknown operation type {self.op_type!r}; "
                               f"expected one of {', '.join(OP_TYPES)}")
        try:
            self.tool_slot = _integer(self.tool_slot, 'tool slot')
        except (TypeError, ValueError) as exc:
            raise ToolingError(f"Operation {self.op_type!r} has a non-numeric tool slot") from exc
        if self.depth is not None:
            if self.op_type in DEPTHLESS_OP_TYPES:
                raise ToolingError(
                    f"Operation {self.label!r} is a {self.op_type} and cannot take a depth. "
                    + ("A profile must go through the stock, or the part is never cut free."
                       if self.op_type == 'perimeter' else
                       "A chamfer's depth follows from its width and the V-tool's angle."))
            try:
                self.depth = _positive_finite(self.depth, 'depth')
            except (TypeError, ValueError) as exc:
                raise ToolingError(f"Operation {self.label!r} has a bad depth: {exc}. Depths "
                                   f"are measured down from the material top.") from exc
        if not isinstance(self.scope, dict):
            raise ToolingError(f"Operation {self.op_type!r} scope must be an object")
        if self.scope.get('purpose') not in (None, ''):
            purpose = str(self.scope['purpose']).strip().lower()
            if purpose not in drill_sizes.PURPOSES:
                raise ToolingError(
                    f"Operation {self.label!r} has unknown drill purpose "
                    f"{self.scope['purpose']!r}; expected one of "
                    f"{', '.join(drill_sizes.PURPOSES)}")
            self.scope['purpose'] = purpose
        if self.op_type == 'chamfer':
            # Reached here rather than in _chamfer_rings because a non-positive width
            # inverts the geometry silently: the cut lands above the stock (an air pass)
            # and the "pocket too narrow" buffer test becomes a dilation that can never
            # fail. Catch it before any of that runs.
            try:
                self.scope['width'] = _positive_finite(self.scope.get('width', 0.02), 'width')
            except (TypeError, ValueError) as exc:
                raise ToolingError(f"Chamfer {self.label!r} has a bad width: {exc}") from exc
        self._validate_scope_numbers()
        self.name = str(self.name or OP_LABELS[self.op_type])

    #: Deepest a spot/centre drill may go, whatever the stock. A spot only has to break
    #: the surface enough to stop the twist drill walking; anything deeper is either a
    #: typo or a job for the drill itself.
    MAX_SPOT_DEPTH = 0.25

    #: Included point angles a real twist drill is ground to. 118 is the general-purpose
    #: default and 135 the split point; outside this band the tip-length compensation
    #: stops being a small correction and starts driving the tool through the table.
    MIN_POINT_ANGLE = 60.0
    MAX_POINT_ANGLE = 150.0

    #: Every numeric scope key, and whether zero is allowed. `float(raw)` with no check
    #: was how `spot_depth: 100` reached the machine as a commanded 99.75 in feed move
    #: and `point_angle: 5` put the final peck 2.2 in below the sacrifice board - both
    #: in programs that reported success.
    _NUMERIC_SCOPE_FIELDS = ('spot_depth', 'point_angle', 'size_tolerance',
                             'min_diameter', 'max_diameter', 'min_area', 'max_area')

    def _validate_scope_numbers(self) -> None:
        """Check every number in `scope` before anything downstream reads it."""
        for key in self._NUMERIC_SCOPE_FIELDS:
            raw = self.scope.get(key)
            if raw is None:
                continue
            if key == 'point_angle':
                self.scope[key] = self._checked_point_angle(raw)
                continue
            try:
                value = _positive_finite(raw, key)
            except (TypeError, ValueError) as exc:
                raise ToolingError(
                    f"Operation {self.label!r} has a bad {key}: {exc}") from exc
            self.scope[key] = value

        depth = self.scope.get('spot_depth')
        if depth is not None and depth > self.MAX_SPOT_DEPTH:
            raise ToolingError(
                f"Operation {self.label!r} asks for a spot_depth of {depth:g} in. A "
                f"centre drill only marks the location; anything past "
                f"{self.MAX_SPOT_DEPTH:g} in is a drilling operation, not a spot.")

    def _checked_point_angle(self, raw: Any) -> float:
        """One message for every way a point angle can be wrong, and it names the range.

        The tip-length compensation divides by tan(angle / 2), so a small angle is not a
        small error: `point_angle: 5` computed a tip 1.4 in long and put the final peck
        at G1 Z-2.2239, two inches below the sacrifice board, in a program that reported
        success.
        """
        bad = (f"Operation {self.label!r} has a point_angle of {raw!r}. A twist drill's "
               f"included point angle must be a number between {self.MIN_POINT_ANGLE:g} "
               f"and {self.MAX_POINT_ANGLE:g} degrees; 118 is the general-purpose grind "
               f"and 135 the split point.")
        try:
            angle = _finite(raw, 'point_angle')
        except (TypeError, ValueError) as exc:
            raise ToolingError(bad) from exc
        if not (self.MIN_POINT_ANGLE <= angle <= self.MAX_POINT_ANGLE):
            raise ToolingError(bad)
        return angle

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Operation':
        if not isinstance(data, dict):
            raise ToolingError(f"Expected an operation object, got {type(data).__name__}")
        raw_scope = data.get('scope')
        scope = {} if raw_scope is None else raw_scope
        if not isinstance(scope, dict):
            raise ToolingError(f"Operation scope must be an object, got "
                               f"{type(scope).__name__}")
        for key in ('indices',):
            if key in scope and scope[key] is not None:
                if not isinstance(scope[key], (list, tuple)):
                    raise ToolingError(f"Operation scope '{key}' must be a list, got "
                                       f"{type(scope[key]).__name__}")
                for index in scope[key]:
                    if not isinstance(index, (int, float)) or isinstance(index, bool):
                        raise ToolingError(f"Operation scope '{key}' must contain "
                                           f"numbers, found {index!r}")
        return cls(op_type=data.get('op_type') or data.get('type') or '',
                   tool_slot=data.get('tool_slot', data.get('tool', 0)),
                   name=data.get('name', ''),
                   depth=data.get('depth'),
                   scope=scope)

    def to_dict(self) -> Dict[str, Any]:
        return {'op_type': self.op_type, 'tool_slot': self.tool_slot, 'name': self.name,
                'depth': self.depth, 'scope': dict(self.scope)}

    @property
    def label(self) -> str:
        return self.name or OP_LABELS[self.op_type]

    @property
    def chamfer_width(self) -> float:
        return float(self.scope.get('width', 0.02))

    @property
    def drill_purpose(self) -> str:
        """What a drilled hole is FOR: 'clearance' (default), 'tap' or 'spot'.

        It decides which drill suits the hole, and the three disagree by more than any
        tolerance. A 0.190 hole drawn for a 10-32 CLEARANCE wants a #10 (0.1935, over);
        the same 0.190 drawn to be TAPPED 10-32 wants a #21 (0.1590, well under); and as
        a SPOT it wants whatever centre drill is in the spindle, because the tool is only
        marking a location for someone else to finish.
        """
        raw = str(self.scope.get('purpose') or drill_sizes.PURPOSE_CLEARANCE).lower()
        return raw if raw in drill_sizes.PURPOSES else drill_sizes.PURPOSE_CLEARANCE

    @property
    def spot_depth(self) -> Optional[float]:
        """How deep a spot/centre drill goes. None = derive from the tool diameter.

        Already range-checked by `_validate_scope_numbers`; nothing unvalidated can
        reach here.
        """
        return self.scope.get('spot_depth')

    @property
    def drill_point_angle(self) -> Optional[float]:
        """Included point angle for a drilled hole, or None for the standard 118 deg.

        It sets how much deeper than the stock a through hole must go for the exit to be
        full diameter, so a 135 deg split point drills measurably shallower than a 118.
        Range-checked at parse time - see `_validate_scope_numbers`.
        """
        return self.scope.get('point_angle')


@dataclass
class PartOps:
    """One part on the stock: where its DXF is, where it sits, and what to do to it."""
    dxf_path: str
    name: str = 'part'
    place_x: float = 0.0
    place_y: float = 0.0
    rotation: float = 0.0
    mirror: bool = False
    engrave_text: Optional[str] = None
    engrave_anchor: Optional[Tuple[float, float]] = None
    engrave_height: float = ENGRAVE_HEIGHT_IN
    engrave_depth: float = ENGRAVE_DEPTH_IN
    engrave_strict_size: bool = False
    operations: List[Operation] = field(default_factory=list)


@dataclass
class MultiToolJob:
    """Everything shared by every operation in one program."""
    material: str
    thickness: float
    tools: List[Tool]
    parts: List[PartOps]
    machine_id: Optional[str] = None
    feeds_machine: Optional[str] = None   # None = fall back to machine_id, then the default
    tab_spacing: float = 6.0
    sacrifice_depth: Optional[float] = None
    #: Which surface the operator zeros Z on - 'board' (sacrifice board) or 'stock_top'.
    #: None takes the team config's default. It has to be job-wide rather than per
    #: operation: every tool change in the program re-zeros Z to the same surface.
    z_datum: Optional[str] = None
    #: Raise the whole program clear of the work and leave the spindle off, for proving
    #: a setup before committing to a cut. Inches; 0 is a real cutting program.
    dry_run_lift: float = 0.0
    #: Cut each part's name into its own face, before anything frees it. Job-wide, like
    #: the Z datum: it is a property of the nest, not of one operation.
    engrave: bool = False
    #: A validated, request-scoped font upload. None selects the built-in CNC
    #: single-line font. Paths never come from job JSON.
    engraving_font_path: Optional[str] = None
    engraving_font_name: Optional[str] = None
    #: Operator ceiling on the depth of one contour pass, inches. Applied to every
    #: milling tool AFTER the model/preset/power clamps, and only ever downward - it
    #: buys more, shallower passes for fragile or multi-flute cutters.
    max_pass_depth: Optional[float] = None
    drill_size_tolerance: float = DEFAULT_DRILL_SIZE_TOLERANCE
    config: Optional[TeamConfig] = None
    user_name: Optional[str] = None
    name: str = 'job'
    units: str = 'inch'

    def __post_init__(self):
        if self.units != 'inch':
            # feeds_speeds is an inch/IPM model and every tool diameter that reaches here
            # has already been parsed to inches by team_config.parse_length.
            raise ToolingError("Multi-tool jobs are inch-only; convert the job to inches first")
        if self.config is None:
            self.config = TeamConfig()
        if not self.tools:
            raise ToolingError("A multi-tool job needs at least one tool")
        slots = [t.slot for t in self.tools]
        duplicates = sorted({s for s in slots if slots.count(s) > 1})
        if duplicates:
            raise ToolingError("Two tools share slot " +
                               ', '.join(f"T{s}" for s in duplicates) +
                               ". Give each tool its own slot number.")
        if not self.parts:
            raise ToolingError("A multi-tool job needs at least one part")
        if len(self.parts) > MAX_PARTS_PER_JOB:
            raise ToolingError(f"This job has {len(self.parts)} parts; the limit is "
                               f"{MAX_PARTS_PER_JOB}. Split it into several jobs.")
        total_operations = sum(len(p.operations) for p in self.parts)
        if total_operations > MAX_OPERATIONS_PER_JOB:
            raise ToolingError(
                f"This job has {total_operations} operations across {len(self.parts)} "
                f"parts; the limit is {MAX_OPERATIONS_PER_JOB}. Every operation re-reads "
                f"the DXF, so a job this size takes minutes. Split it up.")
        for part in self.parts:
            if not part.operations:
                raise ToolingError(f"Part {part.name!r} has no operations")
            for op in part.operations:
                if op.tool_slot not in slots:
                    raise ToolingError(f"Operation {op.label!r} on {part.name!r} asks for "
                                       f"T{op.tool_slot}, which is not in the tool list")
                # The absolute cap lives on Operation; the stock is only known here.
                spot = op.spot_depth
                if spot is not None and spot > self.thickness:
                    raise ToolingError(
                        f"Operation {op.label!r} on {part.name!r} asks for a spot_depth "
                        f"of {spot:g} in in {self.thickness:g} in stock. A spot that "
                        f"goes through the material is a drilled hole, and this one "
                        f"would cut the table.")

    def tool(self, slot: int) -> Tool:
        for t in self.tools:
            if t.slot == slot:
                return t
        raise ToolingError(f"No tool in slot T{slot}")

    @property
    def used_tools(self) -> List[Tool]:
        """Tools actually referenced by an operation, in slot order. A tool listed but
        never used must not appear in the header's table - the operator would go looking
        for a cutter the program never asks for."""
        used = {op.tool_slot for part in self.parts for op in part.operations}
        return [t for t in sorted(self.tools, key=lambda t: t.slot) if t.slot in used]

    @property
    def smallest_tool_diameter(self) -> float:
        """Smallest hole any tool in the job could make. This is what surveys load.

        Using the smallest *assigned* tool instead would report a hole that no operation
        happens to claim as "too small for the tool" - blaming the geometry for what is
        really a gap in the plan, and hiding the fact that a cutter already on the list
        would make it.

        A drill counts for slightly less than its diameter. The survey gate is an end-mill
        rule (the cutter must fit inside the hole), but a drill does not cut a hole, it
        IS the hole - and a drawn hole a few thou UNDER the drill is a stocking difference
        that plan_drilled_holes resolves by snapping to the drill. Without this, a 13/64
        drill against a 0.196 in drawing was rejected as "hole too small for tool" before
        the snapping tolerance ever got a say.
        """
        return min(t.diameter - (self.drill_size_tolerance if t.type == 'drill' else 0.0)
                   for t in self.tools)


# ------------------------------------------------------------------------- feeds/speeds

def resolve_feeds_material(material: str, config: Optional[TeamConfig] = None,
                           machine_id: Optional[str] = None) -> Optional[str]:
    """Map a PenguinCAM material id onto a `feeds_speeds` material key.

    Three outcomes, and the difference between them matters:

    * a key, when the feeds model carries numbers for this material;
    * ``None``, when it does not but the TEAM CONFIG defines a preset. Those are the
      team's own tested feeds and the caller must use them as they stand. Falling back
      to ``'plywood'`` here re-derived brass, delrin and garolite from wood's chipload
      model and overwrote the tuned preset with it;
    * ``ToolingError``, when nothing knows the material at all. There is no safe guess.
    """
    key = feeds_speeds.canonical_material_key(material)
    if key is None:
        key = FEEDS_MATERIAL_ALIASES.get(str(material or '').lower(), '')
    if key in feeds_speeds.MATERIALS:
        return key
    cfg = config or TeamConfig()
    if cfg.get_material_preset(material, machine_id):
        return None
    known = sorted(set(feeds_speeds.MATERIALS) | set(cfg.known_material_ids(machine_id)))
    raise ToolingError(
        f"Unknown material {material!r}: neither the feeds model nor the team config "
        f"has numbers for it, and a guessed feed rate is how bits break. Known "
        f"materials: {', '.join(known)}.")


def resolve_feeds_machine(machine_id: Optional[str]) -> str:
    """Map a team-config machine id onto a `feeds_speeds` machine key, falling back to the
    default router when the team's machine isn't one the feeds model knows."""
    key = str(machine_id or '').lower()
    return key if key in feeds_speeds.MACHINES else DEFAULT_FEEDS_MACHINE


def compute_tool_feeds(tool: Tool, material: str, machine_id: Optional[str],
                       op_type: str, feeds_machine: Optional[str] = None,
                       config: Optional[TeamConfig] = None
                       ) -> Tuple[Dict[str, Any], List[str]]:
    """Derive this tool's feeds/speeds for this operation.

    Returns (feeds dict, warnings). The material presets in `team_config` are all quoted
    for one 4 mm single-flute cutter, so they are the wrong numbers for any other tool;
    `feeds_speeds` re-derives them from chipload, which is the whole reason that module
    exists. Explicit per-tool overrides are applied last and are never second-guessed.
    """
    # First key the feeds model actually knows, in order of specificity. Two things made
    # the machine_id fallback unreachable before: testing the RESOLVED key against the
    # default, and defaulting feeds_machine to a truthy value so "unspecified" and
    # "explicitly omio_x8" were the same thing. A team on an Avid silently got Omio's
    # 150 IPM feed ceiling instead of its own 400.
    machine_key = next(
        (resolve_feeds_machine(candidate) for candidate in (feeds_machine, machine_id)
         if candidate and str(candidate).lower() in feeds_speeds.MACHINES),
        DEFAULT_FEEDS_MACHINE)
    material_key = resolve_feeds_material(material, config, machine_id)

    if material_key is None:
        # The team defined this material; the feeds model has never heard of it. Their
        # preset - already applied to the post-processor - IS the answer. Overlaying the
        # model here would quote a different material's chipload over tested numbers.
        feeds = {
            'unmodelled_material': True,
            'machine_key': machine_key,
            'material_key': None,
            'operation': 'preset',
            'warnings': [],
        }
        return feeds, [
            f"{material} has no entry in the feeds model, so this job runs the team "
            f"config's preset feeds for it unchanged. Verify them against a test cut "
            f"before trusting a long program."]

    if tool.type == 'drill':
        # Drilling is quoted on surface speed and feed per revolution, not chipload per
        # tooth at some radial engagement. Running the milling model here produced a
        # lateral feed (150 IPM on a clamped 1/4 in cutter) being used as a plunge rate.
        drill = feeds_speeds.calculate_drill_feeds(
            machine_key, material_key, {'diameter': tool.diameter})
        feeds = {
            'rpm': drill['rpm'],
            # A drill makes no lateral cut; the only feed it has is the plunge. The
            # lateral entries exist because the rest of the pipeline reads them.
            'feed_xy': drill['plunge_feed'],
            'ramp_feed': drill['plunge_feed'],
            'peck_feed': drill['plunge_feed'],
            'stepover': tool.diameter,
            'stepover_percentage': 1.0,
            'slot_stepdown': tool.diameter,
            'operation': 'drill',
            'warnings': drill['warnings'],
            'drill': drill,
            'machine_key': machine_key,
            'material_key': material_key,
        }
        return feeds, [f"{tool.label}: {w}" for w in drill['warnings']]

    feeds = feeds_speeds.calculate_feeds(
        machine_key,
        material_key,
        {'diameter': tool.diameter, 'flutes': tool.flutes},
        operation=FEEDS_OPERATION.get(op_type, 'profile'),
    )
    feeds['machine_key'] = machine_key
    feeds['material_key'] = material_key
    warnings = [f"{tool.label}: {w}" for w in feeds.get('warnings', [])]
    return feeds, warnings


def _power_limited_depth(tool: Tool, feeds: Dict[str, Any]) -> Optional[float]:
    """Deepest full-width pass this spindle can drive with this tool and feed."""
    machine = feeds.get('machine_key')
    material = feeds.get('material_key')
    if not machine or not material or tool.type == 'drill':
        return None
    return feeds_speeds.max_depth_for_power(machine, material, tool.diameter,
                                            float(feeds['feed_xy']))


def _anchor_metal_feed(pp: FRCPostProcessor, tool: Tool,
                       feeds: Dict[str, Any]) -> Optional[str]:
    """In metal, hold the model's feed to the material preset's TESTED rate, scaled
    for this tool's diameter. Mutates `feeds` in place; returns a warning when it binds.

    The chipload model is theory; the preset feed is bounded by the router safety
    envelope (30 IPM for the 4 mm reference). The model once quoted a 1/8 in cutter
    85.9 IPM, and a real bit broke at it.
    The same never-raise-above-tested doctrine that already governs depth of cut now
    governs feed in the materials that seize (those with feed_flutes_max); wood and
    plastics keep the model's numbers. Explicit per-tool overrides are accepted only
    inside the aluminum safety envelope.

    Must run BEFORE apply_tool_feeds: pp.feed_rate still holds the material preset's
    feed at that point, and the power-limited depth inside apply_tool_feeds must be
    computed from the feed the program will actually command.
    """
    material_key = feeds.get('material_key')
    is_metal = bool(feeds_speeds.MATERIALS.get(material_key, {}).get('feed_flutes_max'))
    preset_feed = getattr(pp, 'feed_rate', None)
    if feeds_speeds.is_aluminum_material(material_key) and preset_feed:
        # Defense in depth: apply_material_preset already clamps the generic aluminum
        # id, but this makes the tool overlay safe even if a caller supplies a partially
        # initialized post-processor or that shared clamp is ever refactored.
        preset_feed = min(
            preset_feed,
            feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['feed_rate'])
    if not is_metal or not preset_feed or tool.type == 'drill' or tool.feed_rate:
        return None
    d_ref = feeds_speeds.REFERENCE_TOOL['diameter']
    # Floored, not rounded: a clamp must never nudge itself upward, and the single-tool
    # path's scale_feeds_to_tool floors identically (46.8, not 46.9, for a 1/8" tool).
    anchor = math.floor(preset_feed
                        * min(1.0, (tool.diameter / d_ref) ** feeds_speeds.DIAMETER_EXPONENT)
                        * 10.0) / 10.0
    original = feeds['feed_xy']
    feed_note = ''
    if anchor < feeds['feed_xy']:
        scale = anchor / feeds['feed_xy']
        feeds['feed_xy'] = anchor
        feeds['ramp_feed'] = round(feeds['ramp_feed'] * scale, 1)
        feeds['peck_feed'] = round(feeds['peck_feed'] * scale, 1)
        feed_note = (f"feed held to {anchor:.1f} ipm, the tested "
                     f"{feeds_speeds.MATERIALS[material_key]['name']} preset rate "
                     f"scaled for this diameter. The model wanted {original:.1f} ipm.")
    rpm_note = ''
    minimum = feeds_speeds.MATERIALS[material_key].get('chipload_min')
    if minimum and not tool.spindle_speed:
        machine = feeds.get('machine_key') or DEFAULT_FEEDS_MACHINE
        spindle_floor = feeds_speeds.MACHINES[machine]['rpm_min']
        corner_floor = getattr(pp, 'corner_min_feed_scale', 1.0)
        rpm_ceiling = feeds['feed_xy'] * corner_floor / (tool.flutes * minimum)
        protected_rpm = max(spindle_floor, min(feeds['rpm'], math.floor(rpm_ceiling)))
        if protected_rpm < feeds['rpm']:
            feeds['rpm'] = int(protected_rpm)
            rpm_note = (f"spindle reduced to {feeds['rpm']} RPM so {tool.flutes} flutes "
                        f"stay at or above {minimum:.4f} in/tooth.")
    notes = ' '.join(n for n in (feed_note, rpm_note) if n)
    return f"{tool.label}: {notes}" if notes else None


def apply_tool_feeds(pp: FRCPostProcessor, tool: Tool, feeds: Dict[str, Any]) -> None:
    """Overlay tool-derived feeds/speeds onto a post-processor that has already had its
    material preset applied.

    Only the values that genuinely track the tool are replaced. Ramp angle, ramp start
    clearance, tab sizes and the corner-slowdown floor stay as the material preset set
    them, because those describe the material and the fixture, not the cutter.
    """
    d_ref = feeds_speeds.REFERENCE_TOOL['diameter']
    material_key = feeds.get('material_key')

    if feeds.get('unmodelled_material'):
        # Nothing to overlay: the post-processor already carries the team's own preset
        # for this material, which is the only tested number anyone has. Explicit
        # per-tool overrides still win, as they do everywhere else.
        if tool.spindle_speed:
            pp.spindle_speed = int(tool.spindle_speed)
        if tool.feed_rate:
            pp.feed_rate = float(tool.feed_rate)
        if tool.plunge_rate:
            pp.plunge_rate = float(tool.plunge_rate)
        return

    # Final defense in depth: callers outside generate_operation historically skipped
    # the anchoring helper. Mutate the shared feed record so every downstream consumer
    # sees the exact protected values that will be emitted.
    if feeds_speeds.is_aluminum_material(material_key):
        diameter_factor = min(
            1.0, (tool.diameter / d_ref) ** feeds_speeds.DIAMETER_EXPONENT)
        if tool.type == 'endmill' and tool.feed_rate is None:
            ceiling = feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['feed_rate'] * diameter_factor
            if feeds['feed_xy'] > ceiling:
                scale = ceiling / feeds['feed_xy']
                feeds['feed_xy'] = math.floor(ceiling * 10.0) / 10.0
                feeds['ramp_feed'] *= scale
                feeds['peck_feed'] *= scale
        if tool.plunge_rate is None:
            feeds['peck_feed'] = min(
                feeds['peck_feed'],
                feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['plunge_rate'] * diameter_factor)
        if tool.type == 'endmill' and tool.spindle_speed is None:
            minimum = feeds_speeds.MATERIALS[material_key]['chipload_min']
            machine = feeds_speeds.MACHINES[feeds.get('machine_key') or DEFAULT_FEEDS_MACHINE]
            rpm_ceiling = (feeds['feed_xy'] * pp.corner_min_feed_scale
                           / (tool.flutes * minimum))
            feeds['rpm'] = int(max(machine['rpm_min'],
                                   min(feeds['rpm'], math.floor(rpm_ceiling))))

    # Per-tool overrides replace values that feeds_speeds.calculate_feeds had already
    # clamped to the machine, so an override slipped straight past every machine limit:
    # a V-bit in aluminium with feed_rate 400 ran F400 on a 150 IPM machine, and
    # spindle_speed 30000 ran S30000 on a 24000 RPM spindle - with no warning at all,
    # because the aluminium guard below is written `elif tool.type == 'endmill'` and
    # validate_aluminum_cutting_parameters is never reached on this path. An override is
    # the operator's call about CUTTING, never permission to exceed what the machine can
    # physically do, so re-apply the machine's own ceilings on top of it.
    _machine = feeds_speeds.MACHINES.get(feeds.get('machine_key') or DEFAULT_FEEDS_MACHINE)

    # apply_tool_feeds returns nothing, so the notice goes on the post-processor's
    # config_warnings - the list that survives generation and is surfaced by
    # generate_gcode, generate_part_phases and generate_operation alike.
    def _note(message):
        getattr(pp, 'config_warnings', []).append(message)

    # CEILINGS ONLY. A value over the machine's maximum is physically impossible, so
    # clamping it is the only honest reading of the request. A value UNDER the spindle's
    # minimum is a different thing entirely: quietly raising the RPM changes the chipload
    # the operator was reasoning about, so that case stays a refusal - see the
    # 'refuse/rpm-below-machine' audit, which this clamp initially broke.
    def _capped(value, ceiling, what, unit):
        if ceiling and value > ceiling:
            _note(f'{tool.label}: {what} {value:.0f} {unit} exceeds this machine\'s '
                  f'{ceiling:.0f} {unit} limit; using {ceiling:.0f}.')
            return ceiling
        return value

    _rpm = float(tool.spindle_speed or feeds['rpm'])
    _feed = float(tool.feed_rate or feeds['feed_xy'])
    _plunge = float(tool.plunge_rate or feeds['peck_feed'])
    if _machine:
        if tool.spindle_speed:
            _rpm = _capped(_rpm, _machine.get('rpm_max'), 'spindle speed', 'RPM')
        if tool.feed_rate:
            _feed = _capped(_feed, _machine.get('xy_feed_max'), 'feed rate', 'IPM')
        if tool.plunge_rate:
            _plunge = _capped(_plunge, _machine.get('z_feed_max'), 'plunge rate', 'IPM')

    pp.spindle_speed = int(_rpm)
    pp.feed_rate = _feed
    pp.ramp_feed_rate = float(feeds['ramp_feed'])
    pp.plunge_rate = _plunge
    pp.stepover_percentage = float(feeds['stepover_percentage'])

    # Depth of cut is CLAMPED to the material preset, never raised by the model.
    #
    # feeds_speeds derives slot_stepdown as a fixed multiple of diameter (1.27x in
    # aluminium, 2.55x in plywood). That matches the preset exactly at the 4 mm reference
    # tool and then diverges hard: a 3/8" cutter in 6061 gets 0.476" against the preset's
    # tested 0.200", and since the pass count comes straight off this number, a 0.258"
    # profile collapses from two passes to one full-width 0.258"-deep slot. Combined with
    # the (legitimately) higher feed for a bigger cutter that is roughly 14 in^3/min of
    # aluminium on a hobby router - a stalled spindle or a snapped end mill.
    #
    # The preset value is what the team has actually run. Scaling it DOWN for a smaller,
    # more fragile cutter is safe; scaling it up on the strength of a chipload model is
    # not. Taking more passes than the model thinks necessary costs time, not tools.
    preset_stepdown = getattr(pp, 'max_slotting_depth', None)
    model_stepdown = float(feeds['slot_stepdown'])
    stepdown = min(model_stepdown, preset_stepdown) if preset_stepdown else model_stepdown

    # ...and clamped again by what the spindle can actually drive.
    #
    # The chipload model looks at one tooth at a time and never at total load, so it will
    # happily hand a 3/8 in cutter a legitimate 0.0042 in/tooth and a 150 IPM feed - which
    # in a full-width profile cut at 0.129 in deep is 2.2 hp out of a 2.2 kW spindle that
    # usefully delivers about 2.1. On a router that does not fail gracefully: the spindle
    # bogs, the cutter grabs, the tool snaps. The reference 4 mm cutter asks for 0.33 hp,
    # so a big end mill in aluminium is where this bites and nowhere else - in plywood the
    # unit power is six times lower and this never binds.
    # Use the feed the program will ACTUALLY command. An explicit tool override is
    # applied to pp.feed_rate just above; calculating power from feeds['feed_xy'] here
    # let a 100 IPM override keep the depth limit computed for 30 IPM.
    power_inputs = dict(feeds, feed_xy=pp.feed_rate)
    power_limit = _power_limited_depth(tool, power_inputs)
    if power_limit and power_limit < stepdown:
        pp.max_slotting_depth = power_limit
        pp.power_limited_depth = True
    else:
        pp.max_slotting_depth = stepdown

    # If the user pinned a cutting feed, the ramp/plunge feeds derived alongside it no
    # longer belong to that number - rescale them by the same factor so the relationship
    # the material preset encodes (ramp and plunge are fractions of the cutting feed)
    # survives the override. Rounded to match the precision feeds_speeds returns, so an
    # override doesn't emit F30.083061889250814 into the program.
    if tool.feed_rate:
        scale = pp.feed_rate / max(feeds['feed_xy'], 1e-6)
        pp.ramp_feed_rate = round(pp.ramp_feed_rate * scale, 1)
        if not tool.plunge_rate:
            pp.plunge_rate = round(pp.plunge_rate * scale, 1)

    if feeds_speeds.is_aluminum_material(material_key):
        diameter_factor = min(
            1.0,
            (tool.diameter / feeds_speeds.REFERENCE_TOOL['diameter'])
            ** feeds_speeds.DIAMETER_EXPONENT)
        pp.ramp_feed_rate = min(
            pp.ramp_feed_rate,
            feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['ramp_feed_rate'] * diameter_factor)
        if tool.plunge_rate is None:
            pp.plunge_rate = min(
                pp.plunge_rate,
                feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['plunge_rate'] * diameter_factor)

    # Peck depth per plunge is a fraction of the cutter's diameter; the preset value is
    # quoted for the 4 mm reference tool, so scale it to this one.
    if hasattr(pp, 'peck_drill_depth'):
        pp.peck_drill_depth = pp.peck_drill_depth * (tool.diameter / d_ref)

    if tool.type == 'drill':
        # Peck depth for real drilling is conventionally a multiple of the DRILL's
        # diameter, not of a milling reference tool. Scaling the preset happened to land
        # near 0.3 x D, but by coincidence rather than by rule - and it would drift the
        # moment anyone retuned the milling preset. State it directly instead: a third of
        # a diameter per peck clears chips from a deep hole in wood or aluminium without
        # making the cycle needlessly slow.
        pp.peck_drill_depth = tool.diameter / 3.0

    if tool.type == 'endmill' and feeds_speeds.is_aluminum_material(material_key):
        minimum = feeds_speeds.MATERIALS[material_key]['chipload_min']
        required_corner_scale = min(
            1.0, pp.spindle_speed * tool.flutes * minimum / pp.feed_rate)
        if required_corner_scale > pp.corner_min_feed_scale + 1e-9:
            old_scale = pp.corner_min_feed_scale
            pp.corner_min_feed_scale = required_corner_scale
            pp.chipload_corner_floor_adjusted = (old_scale, required_corner_scale)

    # Corner slowdown reaches one tool diameter either side of a corner (set in __init__
    # from the constructor diameter, so it is already right - restated here so the
    # dependency is visible if these two ever drift apart).
    pp.corner_slowdown_zone = tool.diameter


# ------------------------------------------------------------------------ feature keys
# Operations are scoped by feature *identity*, not by list position: each operation builds
# its own post-processor, and a post-processor sorts its features into travel order for
# the tool it is holding, so positions are not comparable between operations. Rounding to
# 4 decimals (1e-4 in) is far below any real feature tolerance and far above the float
# noise from the placement transform.

def _round(value: float) -> float:
    return round(float(value), 4)


def circle_key(circle: Dict[str, Any]) -> Tuple[float, float, float]:
    cx, cy = circle['center']
    diameter = circle.get('diameter')
    if diameter is None:
        diameter = 2.0 * circle.get('radius', 0.0)
    return (_round(cx), _round(cy), _round(diameter))


def pocket_key(points: Sequence[Tuple[float, float]]) -> Tuple[float, float, float]:
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    centroid = poly.centroid
    return (_round(centroid.x), _round(centroid.y), _round(poly.area))


# ------------------------------------------------------------------------------ surveys

def build_part_postprocessor(job: MultiToolJob, part: PartOps, tool_diameter: float) -> FRCPostProcessor:
    """Load and place one part with a given tool diameter, up to (and including) perimeter
    and pocket identification. Stops short of `classify_holes` so the caller can first
    narrow `pp.circles` to the operation's scope - a hole outside this operation's scope
    must not be rejected as "too small for the tool" when a later operation with a smaller
    cutter is the one that will drill it."""
    pp = FRCPostProcessor(material_thickness=job.thickness, tool_diameter=tool_diameter,
                          units=job.units, config=job.config, z_datum=job.z_datum)
    if job.dry_run_lift:
        pp.set_dry_run(job.dry_run_lift)
    pp.apply_material_preset(job.material, job.machine_id)
    if job.user_name:
        pp.user_name = job.user_name
    pp.tab_spacing = job.tab_spacing
    if job.sacrifice_depth is not None:
        pp.sacrifice_board_depth = job.sacrifice_depth
        pp._apply_z_frame()   # a deeper overcut moves the bottom of every through-cut
    pp.load_dxf(part.dxf_path)
    pp.transform_coordinates('bottom-left', part.rotation,
                             placement_offset=(part.place_x, part.place_y),
                             enforce_bounds=False, mirror=part.mirror)
    pp.identify_perimeter_and_pockets()
    return pp


def _duplicate_feature_errors(kind: str, listed: List[Dict[str, Any]], describe) -> List[str]:
    """Report features that share an identity key, i.e. coincident duplicates in the DXF."""
    seen, duplicated = set(), []
    for feature in listed:
        if feature['key'] in seen:
            duplicated.append(feature)
        seen.add(feature['key'])
    if not duplicated:
        return []
    what = ', '.join(sorted({describe(f) for f in duplicated}))
    return [f"{len(duplicated)} duplicate {kind}(s) sit exactly on top of each other "
            f"({what}). They would be cut twice. Remove the duplicate geometry in CAD."]


def _inscribed_diameter(poly) -> float:
    """Diameter of the largest circle that fits inside a pocket - which is also the
    largest TOOL that can machine it: the pocket generator refuses when the inward
    buffer by the tool radius comes up empty, and that happens exactly when the tool
    diameter exceeds this number. Surveyed so scope pickers (the standard setups
    especially) can split pockets between cutters without re-deriving the geometry."""
    if poly.geom_type == 'MultiPolygon':
        if poly.is_empty:
            return 0.0
        poly = max(poly.geoms, key=lambda g: g.area)
    try:
        centre = polylabel(poly, tolerance=1e-3)
        return _round(2.0 * poly.exterior.distance(centre))
    except Exception:
        return 0.0


def survey_part(job: MultiToolJob, part: PartOps) -> Dict[str, Any]:
    """Report the features of one part so operations can be scoped to them.

    Surveyed with the *smallest* tool in the job loaded, so a hole that only the small
    cutter can make still shows up. A hole smaller than even that tool is still LISTED
    (flagged `too_small`) rather than rejected here: a spot operation may legitimately
    centre-mark it for hand drilling, and rejecting it in the survey both blocked that
    workflow and hid the hole from the scope pickers that would set it up. Whether an
    undersized hole is an error is decided by `_validate_feature_coverage`, which knows
    the operation plan.

    A 2.5D (multi-layer) DXF is rejected here. `load_dxf` routes one to the multi-layer
    reader, which keeps only the shallowest layer's geometry and overwrites the operator's
    stated thickness from the layer depths - so a multi-tool job would silently machine
    one layer of a stepped part at the wrong thickness. 2.5D is single-part, single-tool
    for now (see the roadmap); the browser hides it, and this is the matching server-side
    guard, since the route can be posted to directly.
    """
    pp = build_part_postprocessor(job, part, job.smallest_tool_diameter)

    if pp.layer_data:
        return {'name': part.name, 'holes': [], 'pockets': [], 'has_perimeter': False,
                'bbox': None, 'hole_sizes': [],
                'errors': [f"this is a 2.5D DXF with {len(pp.layer_data)} depth layers. "
                           f"Multi-tool jobs handle flat (2D) parts only - run this part "
                           f"through the single-tool 2.5D mode instead."]}

    pp.classify_holes(reject_undersized=False)

    holes = [{'index': i,
              'x': _round(h['center'][0]), 'y': _round(h['center'][1]),
              'diameter': _round(h['diameter']),
              'too_small': bool(h.get('too_small')),
              'key': circle_key(h)}
             for i, h in enumerate(pp.holes or [])]

    pockets = []
    for i, points in enumerate(pp.pockets or []):
        poly = Polygon(points)
        if not poly.is_valid:
            poly = poly.buffer(0)
        pockets.append({'index': i, 'area': _round(poly.area),
                        'x': _round(poly.centroid.x), 'y': _round(poly.centroid.y),
                        'inscribed': _inscribed_diameter(poly),
                        'key': pocket_key(points)})

    errors = list(pp.errors)

    # Features are matched to operation scopes by rounded geometry, so two features that
    # round to the same key are indistinguishable to every downstream check: they cannot
    # be put in different operations, and the "cut twice" guard collapses them into one
    # and stays silent while the machine bores the hole a second time. Coincident
    # duplicates are a CAD mistake (a pasted-over circle), so say so rather than guessing
    # which copy was meant.
    errors.extend(_duplicate_feature_errors('hole', holes,
                                            lambda f: f"{f['diameter']:.3f} in dia at "
                                                      f"X{f['x']:.3f} Y{f['y']:.3f}"))
    errors.extend(_duplicate_feature_errors('pocket', pockets,
                                            lambda f: f"X{f['x']:.3f} Y{f['y']:.3f}"))

    bbox = pp.bounding_box()
    return {
        'name': part.name,
        'holes': holes,
        'pockets': pockets,
        'has_perimeter': bool(pp.perimeter),
        'bbox': bbox,
        'errors': errors,
        # Distinct hole sizes, which is how the UI offers hole selection ("all 0.196 in
        # holes go to T1") rather than making anyone click 40 individual circles.
        'hole_sizes': sorted({h['diameter'] for h in holes}),
    }


# ----------------------------------------------------------------------- scope matching

def _in_range(value: float, low: Optional[float], high: Optional[float],
              tol: float = 1e-4) -> bool:
    if low is not None and value < float(low) - tol:
        return False
    if high is not None and value > float(high) + tol:
        return False
    return True


def selected_hole_keys(features: Dict[str, Any], scope: Dict[str, Any]) -> set:
    """Which surveyed holes an operation's scope selects.

    An ABSENT `indices` key means "select by size range" (and an empty range means every
    hole, which is what a single-hole-operation part wants). An `indices` key that is
    present but EMPTY means the user picked nothing, and must select nothing - testing it
    for truthiness instead made a cleared feature picker fall through to the range branch
    and quietly cut every hole in the part with that tool.
    """
    indices = scope.get('indices')
    if indices is not None:
        wanted = {int(i) for i in indices}
        return {h['key'] for h in features['holes'] if h['index'] in wanted}
    return {h['key'] for h in features['holes']
            if _in_range(h['diameter'], scope.get('min_diameter'), scope.get('max_diameter'))}


def selected_pocket_keys(features: Dict[str, Any], scope: Dict[str, Any]) -> set:
    """Which surveyed pockets an operation's scope selects. Same empty-vs-absent
    `indices` rule as selected_hole_keys."""
    indices = scope.get('indices')
    if indices is not None:
        wanted = {int(i) for i in indices}
        return {p['key'] for p in features['pockets'] if p['index'] in wanted}
    return {p['key'] for p in features['pockets']
            if _in_range(p['area'], scope.get('min_area'), scope.get('max_area'))}


# -------------------------------------------------------------------- operation bodies

def _apply_depth(pp: FRCPostProcessor, op: Operation) -> Optional[str]:
    """Set the operation's cut depth. Returns a warning string, or None.

    With no depth given the operation cuts through the stock into the sacrifice board
    (`cut_depth` stays at its negative default), which is what a flat plate wants. A depth
    shallower than the stock leaves a floor, and the post-processor already treats any
    non-through cut as one that must be fully cleared rather than contoured.

    Only the feature-cutting op types get here with a depth at all - `Operation` rejects
    one on a perimeter or chamfer - but the guard is restated because getting it wrong
    means a profile that silently never separates the part.
    """
    if op.depth is None or op.op_type in DEPTHLESS_OP_TYPES:
        return None
    if op.depth >= pp.material_thickness:
        return (f"{op.label}: depth {op.depth:.4f} in is at or past the "
                f"{pp.material_thickness:.4f} in stock thickness, cutting through instead.")
    pp.cut_depth = pp.material_top - op.depth
    return None


def drill_size_tolerance(op: Operation, job_default: float = None) -> float:
    """How far from the drill's diameter a hole may sit and still be drilled with it.

    Per-operation `scope.size_tolerance` wins, then the job's setting, then the built-in
    default. Widening it is legitimate - a shop that stocks fractional drills only will
    genuinely substitute across a few thou - but see the warning in plan_drilled_holes:
    past DRILL_SIZE_WARN_THRESHOLD the substitution is reported with its consequence,
    because "the hole is 0.037 undersize" is the sort of thing you find out at assembly.
    """
    for raw in (op.scope.get('size_tolerance'), job_default):
        if raw is None:
            continue
        try:
            return _positive_finite(raw, 'size_tolerance')
        except (TypeError, ValueError):
            continue
    return DEFAULT_DRILL_SIZE_TOLERANCE


def plan_drilled_holes(op: Operation, tool: Tool, holes: Sequence[Dict[str, Any]],
                       job_tolerance: float = None
                       ) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Decide which of `holes` this drill may make, snapping near-size holes to the drill.

    What counts as "may make" depends on what the hole is for (see Operation.drill_purpose):

      clearance  the finished hole - the drill must be its size, and never undersize,
                 or the fastener it was drawn for will not pass through
      tap        the hole gets threaded afterwards, so the drill is DELIBERATELY under
                 the drawn nominal by about the thread depth
      spot       a locating dimple only, so size does not matter at all and every hole
                 in scope is accepted whatever the tool

    A twist drill cuts exactly one diameter: its own. So a hole within
    `size_tolerance` of the drill is drilled AT THE DRILL'S SIZE and the substitution is
    reported - the finished part will measure the drill, not the drawing, and whoever
    reads the program should know that before the bolt does not fit. A hole outside the
    tolerance is refused: too small and the drill will not enter it at all, too large and
    the drawing wants a bigger drill or a bored hole, and guessing which is not the
    post-processor's call.

    Returns (holes to drill, notes, errors). Each returned hole carries the DRILL's
    diameter, since that is the hole that will exist.
    """
    purpose = op.drill_purpose
    tolerance = drill_size_tolerance(op, job_tolerance)
    planned, notes, errors = [], [], []
    substituted = {}

    if purpose == drill_sizes.PURPOSE_SPOT:
        # A centre drill only marks where a hole goes; it is not making the hole, so it
        # is not held to the hole's size. Every hole in scope gets a dimple, and the
        # drawn diameter is carried through untouched for the record.
        for hole in holes:
            planned.append({**hole, 'drawn_diameter': hole['diameter']})
        if planned:
            notes.append(f"{op.label}: spotting {len(planned)} hole(s) with {tool.label} - "
                         f"a locating dimple only. The holes still have to be drilled.")
        return planned, notes, errors

    if purpose == drill_sizes.PURPOSE_TAP:
        # Tap acceptance has its OWN tolerance, and a widened clearance tolerance may
        # only loosen it as far as MAX_TAP_DRILL_TOLERANCE.
        tap_tolerance = min(max(tolerance, TAP_DRILL_TOLERANCE), MAX_TAP_DRILL_TOLERANCE)
        for hole in holes:
            drawn = hole['diameter']
            advice = drill_sizes.tap_drill_for(drawn)
            if not advice:
                errors.append(
                    f"{op.label}: {drawn:.4f} in is not a thread size PenguinCAM knows, "
                    f"so it cannot work out the tap drill. Set the operation to a "
                    f"clearance hole, or draw the hole at the tap drill size directly.")
                continue
            sizes = [d.diameter for d in advice['tap_drills'] if d]
            if not any(abs(size - tool.diameter) <= tap_tolerance for size in sizes):
                wanted = ', '.join(d.describe() for d in advice['tap_drills'] if d)
                errors.append(
                    f"{op.label}: to tap {'/'.join(advice['threads'])} at {drawn:.4f} in "
                    f"you need {wanted}, not {tool.label} ({tool.diameter:.4f} in).")
                continue
            planned.append({**hole, 'diameter': tool.diameter, 'drawn_diameter': drawn})
            notes.append(
                f"{op.label}: {drawn:.4f} in drilled at {tool.diameter:.4f} in "
                f"({tool.label}) as a TAP DRILL for {'/'.join(advice['threads'])} - "
                f"undersize on purpose; the hole is finished by tapping."
                + ('  Two threads share this nominal, so check which one you meant.'
                   if advice['ambiguous'] else ''))
        return planned, _dedupe(notes), errors

    for hole in holes:
        drawn = hole['diameter']
        difference = drawn - tool.diameter
        if abs(difference) <= tolerance:
            planned.append({**hole, 'diameter': tool.diameter, 'drawn_diameter': drawn})
            if abs(difference) > DRILL_SIZE_NOTE_THRESHOLD:
                substituted[round(drawn, 4)] = substituted.get(round(drawn, 4), 0) + 1
        elif difference < 0:
            errors.append(
                f"{op.label}: a {drawn:.4f} in hole is smaller than {tool.label} "
                f"({tool.diameter:.4f} in), which cannot enter it. Try this: "
                f"{drill_sizes.describe_suggestion(drawn, tolerance)}.")
        else:
            errors.append(
                f"{op.label}: a {drawn:.4f} in hole is {difference:.4f} in larger than "
                f"{tool.label} ({tool.diameter:.4f} in) - past the {tolerance:.4f} in "
                f"tolerance. A hole drilled undersize will not pass the fastener it was "
                f"drawn for. Try this: "
                f"{drill_sizes.describe_suggestion(drawn, tolerance)}."
                f"{_tap_hint(drawn, tool)}"
                f" If you meant to substitute anyway, raise the operation's "
                f"size_tolerance above {abs(difference):.4f} in.")

    for drawn, count in sorted(substituted.items()):
        difference = tool.diameter - drawn
        note = (f"{op.label}: {count} hole(s) drawn at {drawn:.4f} in will be drilled at "
                f"{tool.diameter:.4f} in ({tool.label}), a difference of "
                f"{abs(difference):.4f} in.")
        if abs(difference) > DRILL_SIZE_WARN_THRESHOLD:
            note += ("  That is a big substitution: " + (
                f"the hole ends up {abs(difference):.4f} in UNDERSIZE, so a fastener "
                f"drawn for {drawn:.4f} in will not pass through it."
                if difference < 0 else
                f"the hole ends up {difference:.4f} in oversize, so the fastener will "
                f"have that much slop."))
        notes.append(note)

    return planned, notes, errors


def _tap_hint(drawn: float, tool: Tool) -> str:
    """If this tool is a plausible TAP drill for the drawn hole, say so.

    A hole drawn at a clearance size with a much smaller drill assigned is usually not a
    mistake about size - it is a hole meant to be tapped, planned as a clearance hole.
    """
    advice = drill_sizes.tap_drill_for(drawn)
    if not advice:
        return ''
    close = [d for d in advice['tap_drills']
             if d and abs(d.diameter - tool.diameter) <= DEFAULT_DRILL_SIZE_TOLERANCE]
    if not close:
        return ''
    return (f" Note {tool.label} is a tap drill for "
            f"{'/'.join(advice['threads'])} - if this hole is going to be tapped, set the "
            f"operation's purpose to 'tap' instead of widening the tolerance.")


def _dedupe(items: List[str]) -> List[str]:
    return list(dict.fromkeys(items))


def _check_tool_suits_operation(op: Operation, tool: Tool) -> Optional[str]:
    """Error string when this tool cannot perform this operation, else None.

    Assigning the wrong tool shape is not caught anywhere downstream: the post-processor
    takes a diameter and generates the same helical entry and contour-parallel clearing
    whatever the cutter actually looks like, so a V-tool sent to clear a pocket produces a
    perfectly well-formed program that ruins the part and probably the tool.
    """
    if op.op_type == 'chamfer':
        if tool.type != 'vbit':
            return (f"{op.label} is a chamfer but {tool.label} is a "
                    f"{tool.type}. A chamfer needs a pointed V-tool; set the tool's type "
                    f"to 'vbit' and give it an included angle.")
        return None

    if op.op_type in MILLING_OP_TYPES and tool.type == 'vbit':
        return (f"{op.label} would be cut with {tool.label}, a V-tool. A V-tool has no "
                f"flat bottom, so it cannot cut a pocket floor, a hole, or a profile - "
                f"use an end mill, or make this a chamfer operation.")
    if op.op_type in ('pockets', 'interior', 'perimeter') and tool.type == 'drill':
        return (f"{op.label} would be cut with {tool.label}, a drill. A drill only cuts "
                f"on its tip and must not be fed sideways - use an end mill.")
    return None


def _chamfer_fits(poly: Polygon, width: float) -> bool:
    """Whether a `width` chamfer fits everywhere around this contour.

    The erosion test itself lives on FRCPostProcessor.chamfer_fits (the standard-mode
    deburr pass needs the identical judgement); see its docstring for why "eroded to
    more pieces than it started with" matters as much as "eroded to nothing".
    """
    return FRCPostProcessor.chamfer_fits(poly, width)


def _chamfer_rings(pp: FRCPostProcessor, op: Operation, features: Dict[str, Any],
                   errors: List[str]) -> List[Dict[str, Any]]:
    """Build the contours a chamfer operation traces.

    The V-tool rides centred on the *true* edge (see FRCPostProcessor._generate_chamfer_gcode),
    so these are the uncompensated contours: the perimeter is climb-milled clockwise as an
    outside edge, holes and pockets counter-clockwise as inside edges.
    """
    targets = op.scope.get('targets') or ['perimeter']
    width = op.chamfer_width
    rings: List[Dict[str, Any]] = []

    if 'perimeter' in targets:
        if pp.perimeter:
            # The perimeter needs the same "does the break physically fit" test as a
            # pocket, and for the same reason: the V-tool reaches `width` sideways from
            # the edge, so a part with a neck narrower than 2 x width has its two
            # chamfers meet in the middle and the top of the neck disappears. Eroding the
            # part by `width` finds the tightest waist without needing to measure it.
            if not _chamfer_fits(Polygon(pp.perimeter), width):
                errors.append(f"{op.label}: this part is too narrow somewhere for a "
                              f"{width:.4f} in chamfer on both sides. The V-tool would cut "
                              f"away the material between the two edges.")
            else:
                rings.append({'points': pp.perimeter, 'clockwise': True,
                              'label': 'Perimeter', 'min_radius': None})
        else:
            errors.append(f"{op.label}: this part has no perimeter to chamfer.")

    if 'holes' in targets:
        wanted = selected_hole_keys(features, op.scope)
        for circle in (pp.circles or []):
            if circle_key(circle) not in wanted:
                continue
            cx, cy = circle['center']
            radius = circle.get('radius') or circle.get('diameter', 0.0) / 2.0
            rings.append({'points': pp._tessellate_circle(cx, cy, radius),
                          'clockwise': False,
                          'label': f"Hole {2 * radius:.3f} in dia at X{cx:.3f} Y{cy:.3f}",
                          'min_radius': radius})

    if 'pockets' in targets:
        wanted = selected_pocket_keys(features, op.scope)
        for points in (pp.pockets or []):
            if pocket_key(points) not in wanted:
                continue
            poly = Polygon(points)
            if not poly.is_valid:
                poly = poly.buffer(0)
            # Same fit test as the perimeter: catches both a uniformly narrow pocket and
            # a wide one with a narrow arm or waist.
            if not _chamfer_fits(poly, width):
                errors.append(f"{op.label}: a pocket at X{poly.centroid.x:.3f} "
                              f"Y{poly.centroid.y:.3f} is too narrow somewhere for a "
                              f"{width:.4f} in chamfer on both walls.")
                continue
            rings.append({'points': points, 'clockwise': False,
                          'label': f"Pocket at X{poly.centroid.x:.3f} Y{poly.centroid.y:.3f}",
                          'min_radius': None})

    return rings


def generate_operation(job: MultiToolJob, part: PartOps, op: Operation,
                       features: Dict[str, Any],
                       defer_tabs: bool = False) -> Dict[str, Any]:
    """Emit one operation's toolpath body.

    Builds a post-processor holding this operation's tool, narrows the part's features to
    the operation's scope, and runs the ordinary generator for that feature kind. The
    result carries the post-processor too, since the assembler needs one per tool to emit
    a tool change at that tool's spindle speed and safe height.

    `defer_tabs` splits a perimeter operation's tab-removal pass out into a separate body
    returned under `deferred`, for the caller to emit at the very end of the program. Tabs
    are the only thing holding a profiled part in the stock, so cutting them away inline
    would leave a loose part on the table for every operation that follows - a chamfer
    pass on the same part, or another part's profile on the same sheet.
    """
    tool = job.tool(op.tool_slot)
    feeds, warnings = compute_tool_feeds(tool, job.material, job.machine_id, op.op_type,
                                         feeds_machine=job.feeds_machine,
                                         config=job.config)

    pp = build_part_postprocessor(job, part, tool.diameter)
    anchor_warning = _anchor_metal_feed(pp, tool, feeds)
    if anchor_warning:
        warnings.append(anchor_warning)
    apply_tool_feeds(pp, tool, feeds)
    if job.max_pass_depth is not None:
        # Operator ceiling, applied after every automatic clamp and only downward:
        # more, shallower passes to baby a fragile or multi-flute cutter.
        pp.apply_max_pass_depth(job.max_pass_depth)

    if getattr(pp, 'power_limited_depth', False):
        warnings.append(
            f"{tool.label} in {job.material}: depth of cut reduced to "
            f"{pp.max_slotting_depth:.4f} in so a full-width pass stays inside what the "
            f"spindle can drive. The cut takes more passes; a bigger cutter is not "
            f"always a faster one on a router.")

    if getattr(pp, 'chipload_corner_floor_adjusted', None):
        old_floor, new_floor = pp.chipload_corner_floor_adjusted
        warnings.append(
            f"{tool.label}: corner slowdown floor raised from {old_floor:.2f} to "
            f"{new_floor:.2f} so the cutter still makes a chip instead of rubbing at "
            f"{pp.spindle_speed} RPM.")

    depth_warning = _apply_depth(pp, op)
    if depth_warning:
        warnings.append(depth_warning)

    reach_warning = _check_tool_reach(pp, tool)
    if reach_warning:
        warnings.append(reach_warning)

    errors: List[str] = []
    lines: List[str] = []
    deferred: Optional[Dict[str, Any]] = None
    pp._pending_clearance_rapid = True

    mismatch = _check_tool_suits_operation(op, tool)
    material_key = resolve_feeds_material(job.material, job.config, job.machine_id)
    flute_cap = feeds_speeds.MATERIALS.get(material_key, {}).get('feed_flutes_max')
    if (not mismatch and tool.type == 'endmill' and flute_cap
            and tool.flutes > flute_cap):
        mismatch = (
            f"{op.label} uses {tool.label}, a {tool.flutes}-flute cutter in "
            f"{feeds_speeds.MATERIALS[material_key]['name']}. Use a 1- or 2-flute "
            f"aluminum end mill on this router so chips can evacuate; packed chips "
            f"weld to the cutter and snap it.")
    if not mismatch and feeds_speeds.is_aluminum_material(material_key):
        machine_key = feeds.get('machine_key') or DEFAULT_FEEDS_MACHINE
        machine = feeds_speeds.MACHINES[machine_key]
        actual_feed = float(pp.feed_rate)
        actual_rpm = int(pp.spindle_speed)
        actual_plunge = float(pp.plunge_rate)
        diameter_factor = min(
            1.0,
            (tool.diameter / feeds_speeds.REFERENCE_TOOL['diameter'])
            ** feeds_speeds.DIAMETER_EXPONENT)
        feed_ceiling = feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['feed_rate'] * diameter_factor
        plunge_ceiling = feeds_speeds.ALUMINUM_ROUTER_SAFETY_MAX['plunge_rate'] * diameter_factor
        if not machine['rpm_min'] <= actual_rpm <= machine['rpm_max']:
            mismatch = (
                f"{op.label} asks {tool.label} for {actual_rpm} RPM; {machine['name']} "
                f"must stay between {machine['rpm_min']} and {machine['rpm_max']} RPM.")
        elif tool.type == 'endmill' and actual_feed > feed_ceiling + 1e-9:
            mismatch = (
                f"{op.label} asks {tool.label} to cut aluminum at {actual_feed:g} ipm, "
                f"above its diameter-scaled {feed_ceiling:.1f} ipm router ceiling.")
        elif actual_plunge > plunge_ceiling + 1e-9:
            mismatch = (
                f"{op.label} asks {tool.label} to plunge at {actual_plunge:g} ipm, "
                f"above its aluminum ceiling of {plunge_ceiling:.1f} ipm.")

    if (not mismatch and feeds_speeds.is_aluminum_material(material_key)
            and tool.type == 'endmill'):
        actual_feed = float(pp.feed_rate)
        actual_rpm = int(pp.spindle_speed)
        minimum = feeds_speeds.MATERIALS[material_key]['chipload_min']
        maximum = feeds_speeds.MATERIALS[material_key]['chipload_max']
        actual_chipload = actual_feed / (actual_rpm * tool.flutes)
        if actual_chipload + 1e-12 < minimum:
            mismatch = (
                f"{op.label} would run {tool.label} at {actual_chipload:.4f} in/tooth, "
                f"below the aluminum minimum {minimum:.4f}. That rubs and heats instead "
                f"of making a chip. Lower RPM, use fewer flutes, or remove the tool "
                f"override.")
        elif actual_chipload > maximum + 1e-12:
            mismatch = (
                f"{op.label} would run {tool.label} at {actual_chipload:.4f} in/tooth, "
                f"above the aluminum maximum {maximum:.4f}. Raise RPM or lower feed.")
    if mismatch:
        # A hard error, not a warning: every one of these produces a program that looks
        # correct and cuts wrongly, so there is nothing useful to hand the operator.
        return {'part': part, 'op': op, 'tool': tool, 'pp': pp, 'lines': [],
                'errors': [mismatch], 'warnings': warnings, 'feeds': feeds,
                'deferred': None, 'min_z': pp.cut_depth}

    if op.op_type == 'chamfer':
        rings = _chamfer_rings(pp, op, features, errors)
        if rings:
            lines = pp._generate_chamfer_gcode(rings, op.chamfer_width, tool.included_angle)

    elif op.op_type == 'perimeter':
        pp.circles = []
        pp.holes = []
        pp.pockets = []
        if not pp.perimeter:
            errors.append(f"{op.label}: this part has no perimeter to cut.")
        else:
            pp._deferred_tab_positions = []
            lines.append("(===== PERIMETER WITH TABS =====)" if pp.tabs_enabled
                         else "(===== PERIMETER (NO TABS) =====)")
            lines.extend(pp._generate_perimeter_gcode(pp.perimeter,
                                                      defer_tab_removal=defer_tabs))
            if defer_tabs and pp.config.remove_tabs and pp._deferred_tab_positions:
                deferred = {
                    'part': part, 'tool': tool, 'pp': pp, 'feeds': feeds,
                    'op': Operation('perimeter', op.tool_slot, f'{op.label} - tab removal'),
                    'lines': pp._generate_tab_removal_gcode(pp._deferred_tab_positions),
                    'errors': [], 'warnings': [],
                }

    elif tool.type == 'drill':
        # A drill never reaches the milling generator. That generator enters helically and
        # feeds sideways to open a bore, which a twist drill physically cannot do - it has
        # no side cutting edges - so routing a drill through it emitted a well-formed
        # program that would snap the tool. `_check_tool_suits_operation` has already
        # confined drills to hole operations by the time we get here.
        wanted = selected_hole_keys(features, op.scope)
        in_scope = [{'center': (h['x'], h['y']), 'diameter': h['diameter']}
                    for h in features['holes'] if h['key'] in wanted]

        planned, notes, size_errors = plan_drilled_holes(
            op, tool, in_scope, job_tolerance=job.drill_size_tolerance)
        warnings.extend(notes)
        errors.extend(size_errors)

        if not in_scope:
            warnings.append(f"{op.label} on {part.name}: no features matched this "
                            f"operation's scope, so it emits nothing.")
        if planned and not size_errors:
            lines = pp.generate_drill_operation_gcode(
                planned, point_angle=op.drill_point_angle,
                spot_only=(op.drill_purpose == drill_sizes.PURPOSE_SPOT),
                spot_depth=op.spot_depth)
        pp.holes = planned
        pp.pockets = []

    else:  # holes / pockets / interior, cut with an end mill
        want_holes = op.op_type in ('holes', 'interior')
        want_pockets = op.op_type in ('pockets', 'interior')

        # Narrow the circle list BEFORE classification, so classify_holes only judges (and
        # only complains about) the holes this operation is responsible for.
        if want_holes:
            wanted = selected_hole_keys(features, op.scope)
            pp.circles = [c for c in (pp.circles or []) if circle_key(c) in wanted]
        else:
            pp.circles = []
        pp.classify_holes()

        if want_pockets:
            wanted = selected_pocket_keys(features, op.scope)
            pp.pockets = [p for p in (pp.pockets or []) if pocket_key(p) in wanted]
        else:
            pp.pockets = []

        if not pp.holes and not pp.pockets and not pp.errors:
            warnings.append(f"{op.label} on {part.name}: no features matched this "
                            f"operation's scope, so it emits nothing.")
        lines = pp._generate_interior_gcode(emit_contour_pauses=False)

    # `holes` is only created by classify_holes, which the chamfer and perimeter paths
    # never call. Several read-only helpers on the post-processor (the header's operations
    # summary, the stats block) reach for it, so give it a value rather than leaving the
    # attribute missing on an instance the assembler will keep using.
    if not hasattr(pp, 'holes'):
        pp.holes = []

    errors.extend(pp.errors)

    # The post-processor's own advisories, which this path used to drop on the floor.
    # `geometry_warnings` is where load_dxf reports a $INSUNITS mismatch ("every dimension
    # in the program is off by a factor of 25.4") and a boundary that had to be closed
    # across a gap or could not be machined at all; `config_warnings` carries the dry-run
    # over-travel notice. The single-tool path surfaces all three through generate_gcode,
    # and /process-job through generate_part_phases - but the wizard sends every flat 2D
    # job here, so on the default path nobody ever saw them. Prefixed with the part name
    # because a job stitches several parts into one program.
    for note in (list(getattr(pp, 'geometry_warnings', ()))
                 + list(getattr(pp, 'config_warnings', ()))
                 + list(getattr(pp, 'warnings', ()))):
        warnings.append(f'{part.name}: {note}')

    # The deepest Z this body actually reaches, for the header's ZMIN. Two op types do
    # not cut to `cut_depth` and must say so, or the operator checks clearance below the
    # stock against a number the program goes straight past:
    #   - a chamfer works out its own shallow Z from the width and the tool angle;
    #   - a through-drilled hole goes DEEPER than cut_depth by the drill's point length,
    #     so reading cut_depth under-reports it (0.075 in on a 1/4 in drill).
    if op.op_type == 'chamfer':
        min_z = pp.material_top - FRCPostProcessor.chamfer_depth(op.chamfer_width,
                                                                 tool.included_angle)
    elif tool.type == 'drill' and pp.is_through_cut():
        deepest = max((h['diameter'] for h in (pp.holes or [])), default=tool.diameter)
        min_z = pp.cut_depth - FRCPostProcessor.drill_point_length(
            deepest, op.drill_point_angle or FRCPostProcessor.DEFAULT_DRILL_POINT_ANGLE)
    else:
        min_z = pp.cut_depth

    return {'part': part, 'op': op, 'tool': tool, 'pp': pp, 'lines': lines,
            'errors': errors, 'warnings': warnings, 'feeds': feeds,
            'deferred': deferred, 'min_z': min_z}


# ------------------------------------------------------------------------------ ordering

def _group_queues_by_tool(parts: Sequence[PartOps],
                          queues: List[List[int]]) -> List[Tuple[int, int]]:
    """Drain per-part operation queues into one sequence, grouped by tool.

    Repeatedly takes the tool the next ready operation needs and drains every part's
    leading run of operations using it. Each queue is consumed strictly in order, so no
    part's own sequence is ever rearranged.
    """
    order: List[Tuple[int, int]] = []
    while any(queues):
        current = None
        for pi, queue in enumerate(queues):
            if queue:
                current = parts[pi].operations[queue[0]].tool_slot
                break
        if current is None:
            break
        for pi, queue in enumerate(queues):
            while queue and parts[pi].operations[queue[0]].tool_slot == current:
                order.append((pi, queue.pop(0)))
    return order


def order_operations(parts: Sequence[PartOps],
                     split_after_holes: bool = False,
                     split_before_perimeter: bool = False) -> List[Tuple[int, int]]:
    """Flatten every part's operation list into one job sequence, grouped by tool.

    Each part's operations run in the order they were written - that order encodes intent
    (rough before finish, profile before chamfer) and is never rearranged. What *is* free
    is the interleaving between parts, so the sequence repeatedly picks the tool the next
    ready operation needs and drains every part's leading run of operations using it. For
    the common case of several parts sharing one operation list, that turns N parts x M
    tools worth of swaps into M swaps total.

    `split_after_holes` keeps every operation through each part's last hole operation in
    a first phase. That guarantees the shared fastening stop occurs only after all holes
    exist and before the usual pockets/profile phase. An unusual plan that deliberately
    puts another operation before its last hole keeps that relative order; this function
    never rewrites a part's stated process.

    `split_before_perimeter` is the older, independent option that keeps every part's
    interior work ahead of every profile.

    Returns [(part_index, op_index), ...].
    """
    if not split_after_holes and not split_before_perimeter:
        return _group_queues_by_tool(parts, [list(range(len(p.operations))) for p in parts])

    phases = []
    if split_after_holes:
        through_holes, after_holes = [], []
        for part in parts:
            hole_indices = [i for i, op in enumerate(part.operations)
                            if op.op_type == 'holes']
            cut = (max(hole_indices) + 1) if hole_indices else 0
            through_holes.append(list(range(cut)))
            after_holes.append(list(range(cut, len(part.operations))))
        phases.append(through_holes)
        remaining = after_holes
    else:
        remaining = [list(range(len(p.operations))) for p in parts]

    # The legacy perimeter split can compose with the hole split. It only divides the
    # operations that remain, so no operation is duplicated or moved within its part.
    if not split_before_perimeter:
        phases.append(remaining)
        ordered = []
        for phase in phases:
            ordered.extend(_group_queues_by_tool(parts, phase))
        return ordered

    interior, profile = [], []
    for part, queue in zip(parts, remaining):
        cut = next((i for i, op_index in enumerate(queue)
                    if part.operations[op_index].op_type == 'perimeter'), len(queue))
        interior.append(queue[:cut])
        profile.append(queue[cut:])
    phases.extend([interior, profile])
    ordered = []
    for phase in phases:
        ordered.extend(_group_queues_by_tool(parts, phase))
    return ordered


def build_tool_table(bodies: Sequence[Dict[str, Any]]) -> List[str]:
    """The header's tool list, built from the operations that actually EMITTED toolpath.

    Not from the requested operation list: an operation whose scope matched nothing is
    dropped from the program, and if that was the only use of a tool, listing it would
    send the operator hunting for a cutter the program never pauses for.
    """
    seen = {}
    for body in bodies:
        seen.setdefault(body['tool'].slot, body['tool'])
    return [seen[slot].description() for slot in sorted(seen)]


# ------------------------------------------------------------------------------ assembly

def _tool_change_gcode(pp: FRCPostProcessor, previous: Optional[Tool], nxt: Tool,
                       extra_instructions: Sequence[str] = (),
                       checkpoint_id: str = None) -> List[str]:
    """The manual tool-change block between two operations.

    Reuses the standard pause-and-park sequence, so a tool change parks and stops exactly
    the way every other operator pause in a PenguinCAM program does - and restarts the
    spindle at the *incoming* tool's speed, because `pp` is the post-processor built for
    the operation that follows.
    """
    instructions = [f"Remove {previous.label}" ] if previous else []
    instructions += [
        f"Install {nxt.label}, {nxt.diameter:.4f} in diameter, {nxt.kind}",
        f"Re-zero G54 Z to {pp.z_zero_surface()} with the new tool, not with G92",
        "Do NOT change the X or Y zero",
    ]
    material_key = feeds_speeds.canonical_material_key(
        getattr(pp, 'material_id', getattr(pp, 'material_name', '')))
    if feeds_speeds.is_aluminum_material(material_key):
        instructions += [
            'Confirm incoming cutter is sharp, clean, and approved for aluminum',
            'Clean collet, minimize stickout, and verify low runout',
            'Confirm continuous directed air and a clear chip escape path before restart',
        ]
        if material_key == 'aluminum_6063':
            instructions.append(
                'Confirm proven aluminum-compatible lubricant or MQL is ready for 6063')
    instructions.extend(sanitize_comment(i) for i in extra_instructions)
    title = sanitize_comment(f"TOOL CHANGE - {nxt.label}", 'TOOL CHANGE')
    return pp._generate_pause_and_park_gcode(
        title, instructions, tool_change=True,
        resume_checkpoint=checkpoint_id,
        resume_description=nxt.label,
    )


def assemble_job(job: MultiToolJob, bodies: Sequence[Dict[str, Any]],
                 timestamp: Optional[str] = None,
                 suggested_filename: Optional[str] = None,
                 extra_warnings: Optional[Sequence[str]] = None) -> PostProcessorResult:
    """Stitch the ordered operation bodies into one program with manual tool changes."""
    import datetime

    if not timestamp:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Operations that produced nothing are dropped from the program, but their warnings
    # are kept: "this operation matched no features" is exactly what the person who
    # mistyped a size range needs to hear, and dropping the body would silence it.
    all_warnings = [w for b in bodies for w in b['warnings']]
    all_warnings.extend(extra_warnings or [])
    # Every operation on a part loads that part's DXF into its own post-processor, so a
    # warning about the DRAWING (wrong units, a boundary that would not close) is raised
    # once per operation. The operator needs to read it once. Order is preserved so the
    # first mention keeps its place in the list.
    all_warnings = list(dict.fromkeys(all_warnings))
    bodies = [b for b in bodies if b['lines']]
    if not bodies:
        return PostProcessorResult(success=False,
                                   errors=["No operation produced any toolpath. Check that "
                                           "the operations' scopes match features the part "
                                           "actually has."])

    # The header is written by the FIRST operation's post-processor, so the spindle speed
    # it starts with is the first tool's. Its own cut depth, though, only describes that
    # one operation - so give it the deepest cut in the whole program before the header is
    # built, or a job that happens to start with a shallow pocket would advertise a ZMIN
    # the program goes well past. Safe to mutate: every body is already generated, and
    # nothing else the header or footer reads depends on cut_depth.
    header_pp = bodies[0]['pp']
    header_pp.cut_depth = min(b.get('min_z', b['pp'].cut_depth) for b in bodies)
    multi_part = len(job.parts) > 1
    op_summary = ', '.join(dict.fromkeys(sanitize_comment(b['op'].label) for b in bodies))
    tool_table = build_tool_table(bodies)

    # Describe how the tools actually enter the work. The stock header promises helical
    # entry and "no straight plunges", which is true of every milling path here and the
    # exact opposite of what a twist drill does.
    drilling = [b for b in bodies if b['tool'].type == 'drill']
    milling = [b for b in bodies if b['tool'].type != 'drill']
    entry_notes = None
    if drilling:
        entry_notes = []
        if milling:
            entry_notes.append(f"(Milled features: helical entry, ~{int(header_pp.ramp_angle)} deg)")
        entry_notes.append("(Drilled holes: straight axial plunge, pecked)")
        entry_notes.append("(Through-drilled holes go past the stock to clear the drill point)")

    gcode = header_pp._generate_gcode_header(
        timestamp,
        is_job=multi_part,
        job_part_count=len(job.parts) if multi_part else None,
        tool_table=tool_table,
        operations_override=op_summary,
        entry_notes=entry_notes,
    )

    # A tool that cannot reach the bottom of its own cut is something the operator has
    # to read at the machine, not only in the browser response - they may be running a
    # file someone else generated.
    reach_notes = [w for w in dict.fromkeys(all_warnings)
                   if 'flute' in w.lower() and 'deep' in w.lower()]
    if reach_notes:
        gcode.append('')
        gcode.append('(** CHECK TOOL REACH **)')
        for note in reach_notes:
            gcode.append(f'({sanitize_comment(note)})')

    # Default multi-tool fixturing boundary: once every emitted hole operation is done,
    # stop before the next operation so the operator can use those holes to fasten the
    # stock. The ordering phase above normally makes this the holes -> pockets/profile
    # boundary across the whole sheet. Empty scoped operations have already been dropped,
    # so a plan that did not actually make a hole does not advertise nonexistent holes.
    last_holes = max((i for i, b in enumerate(bodies) if b['op'].op_type == 'holes'),
                     default=None)
    after_holes = (last_holes + 1
                   if last_holes is not None and last_holes + 1 < len(bodies) else None)
    hole_fixturing_instructions = [
        'All fastening hole operations complete',
        'Install fasteners through the completed holes into the sacrifice board',
        'Fixture every part securely before pockets and profiles',
    ] if (header_pp.config.pause_after_holes and after_holes is not None) else []

    # Retain the older optional stop immediately before the first profile. When the
    # after-holes stop already occurred, do not ask the operator to fasten the same sheet
    # twice.
    first_perimeter = next((i for i, b in enumerate(bodies)
                            if b['op'].op_type == 'perimeter'), None)
    # Truthful because order_operations was asked to put every part's interior work ahead
    # of every part's profile whenever this pause is enabled (split_before_perimeter).
    fixturing_instructions = [
        'Internal features complete on every part',
        'Install screws through holes into the sacrifice board',
        'Fixture every part securely before the profile cut',
    ] if (header_pp.pause_before_perimeter and first_perimeter is not None
          and not hole_fixturing_instructions) else []

    current_tool: Optional[Tool] = None
    current_rpm: Optional[int] = None   # what the spindle was last actually commanded
    change_number = 0
    for i, body in enumerate(bodies):
        pp, tool, op, part = body['pp'], body['tool'], body['op'], body['part']
        needs_change = current_tool is None or current_tool.slot != tool.slot
        if i == after_holes:
            fixturing_here = hole_fixturing_instructions
        elif i == first_perimeter:
            fixturing_here = fixturing_instructions
        else:
            fixturing_here = []

        if needs_change and current_tool is not None:
            change_number += 1
            gcode.extend(_tool_change_gcode(
                pp, current_tool, tool, fixturing_here,
                checkpoint_id=f'TC{change_number:02d}',
            ))
        else:
            if fixturing_here:
                gcode.extend(pp._generate_pause_and_park_gcode(
                    'PAUSE FOR FIXTURING', fixturing_here,
                    safe_z=pp._tool_change_safe_z()))
            if needs_change:   # very first tool: nothing to swap out, just say what to load
                gcode.append(f"(Load {tool.label}, {tool.kind}, before starting "
                             f"this program)")
                gcode.append("")
            elif int(pp.spindle_speed) != current_rpm:
                # Same tool, different operation, different derived RPM. The feeds model
                # quotes a slot, a pocket and a profile differently, and each body's
                # header printed its own number - but S was only ever emitted at a tool
                # change, so the spindle kept turning at the FIRST body's speed while
                # later bodies fed to their own. Verified: a pockets body announcing
                # 12000 RPM ran at 9320, putting the chipload 29% above what the
                # aluminum guard validated. S alone is legal with M3 already active.
                gcode.append("")
                gcode.append(f"S{int(pp.spindle_speed)}  ; Spindle to "
                             f"{int(pp.spindle_speed)} RPM for this operation")
                gcode.append("G4 P1  ; Wait for the spindle to settle")
        current_tool = tool
        current_rpm = int(pp.spindle_speed)

        gcode.append("")
        gcode.append(f"(===== {sanitize_comment(op.label).upper()} - "
                     f"{sanitize_comment(part.name, 'part')} - {tool.label} =====)")
        gcode.append(f"(Tool {tool.diameter:.4f} in diameter, feed {pp.feed_rate:.1f} ipm, "
                     f"spindle {pp.spindle_speed} rpm)")
        gcode.append(f"G0 Z{pp._safe_z():.4f}  ; Safe Z before operation")
        gcode.extend(body['lines'])

    gcode.extend(header_pp._generate_gcode_footer())

    time_estimate = header_pp._estimate_cycle_time(gcode)
    header_pp._insert_cycle_time_comment(gcode, time_estimate)

    tool_changes = sum(1 for i, b in enumerate(bodies)
                       if i and b['tool'].slot != bodies[i - 1]['tool'].slot)

    return PostProcessorResult(
        success=True,
        gcode='\n'.join(gcode),
        filename=build_output_filename(suggested_filename or job.name, timestamp, 'job',
                                       dry_run=bool(job.dry_run_lift)),
        # Deduped: a feeds warning about one tool repeats for every part and every
        # operation that tool touches, and ten copies of one sentence reads as noise.
        warnings=list(dict.fromkeys(all_warnings)),
        stats={
            'num_parts': len(job.parts),
            'num_operations': len(bodies),
            'num_tools': len(tool_table),
            'tool_changes': tool_changes,
            'tools': list(tool_table),
            'total_lines': len(gcode),
            'cycle_time_seconds': time_estimate['total'],
            'cycle_time_display': header_pp._format_time(time_estimate['total']),
            'cutting_time': header_pp._format_time(time_estimate['cutting']),
            'rapid_time': header_pp._format_time(time_estimate['rapid']),
            'dwell_time': header_pp._format_time(time_estimate['dwell']),
            # M0 waits are operator time and can't be estimated, so say so rather than
            # quoting a cycle time that silently assumes instant tool changes.
            'excludes_tool_change_time': tool_changes > 0,
        },
    )


def _validate_feature_coverage(part: PartOps, features: Dict[str, Any]
                               ) -> Tuple[List[str], List[str]]:
    """Every machinable feature must be CUT by exactly one operation.

    Returns (errors, warnings).

    A feature no operation claims is silently absent from the program, and the operator
    discovers it with the part already off the machine - so this is an error, not a
    warning. A feature TWO operations claim gets cut twice, which at best wastes time and
    at worst bores an oversized hole.

    A SPOT operation is not a cutting operation. It marks where a hole goes and leaves
    the hole to be made elsewhere, so it neither satisfies coverage nor conflicts with
    the op that does. Counting it did both harms at once: a plan of just
    [spot, perimeter] passed in silence and shipped a plate of dimples, while
    spot-then-drill - the documented workflow - was rejected as "would be cut twice".

    Both holes and pockets are checked, and neither check is conditional on the part
    happening to have an operation of that kind. Gating the hole check on "does this part
    have a holes operation" (as this first did) meant a plan of just [pockets, perimeter]
    on a 5-hole plate passed silently and drilled nothing - the exact failure the check
    exists to prevent.
    """
    errors: List[str] = []
    warnings: List[str] = []

    for kind, listed, select, describe in (
        ('hole', features['holes'], selected_hole_keys,
         lambda f: f"{f['diameter']:.3f} in"),
        ('pocket', features['pockets'], selected_pocket_keys,
         lambda f: f"{f['area']:.3f} sq in at X{f['x']:.2f} Y{f['y']:.2f}"),
    ):
        if not listed:
            continue
        relevant = ('holes', 'interior') if kind == 'hole' else ('pockets', 'interior')
        by_key = {f['key']: f for f in listed}
        claimed, double_claimed, spotted = set(), set(), set()
        for op in part.operations:
            if op.op_type not in relevant:
                continue
            selected = select(features, op.scope)
            if op.drill_purpose == drill_sizes.PURPOSE_SPOT:
                spotted |= selected
                continue
            double_claimed |= (selected & claimed)
            claimed |= selected

        if double_claimed:
            what = ', '.join(sorted({describe(by_key[k]) for k in double_claimed}))
            errors.append(f"{part.name}: {kind}(s) of {what} are claimed by more than one "
                          f"operation and would be cut twice. Narrow one operation's scope.")

        unclaimed = [f for f in listed if f['key'] not in claimed]
        spot_only = [f for f in unclaimed if f['key'] in spotted]
        if spot_only:
            what = ', '.join(sorted({describe(f) for f in spot_only}))
            warnings.append(
                f"{part.name}: {len(spot_only)} {kind}(s) are spotted but never drilled "
                f"in this job ({what}). They will come off the machine as dimples - a "
                f"drill press is assumed.")

        missing = [f for f in unclaimed if f['key'] not in spotted]
        # A hole smaller than every tool in the job (the survey lists it flagged rather
        # than rejecting it) has two real fixes, and "widen a scope" is not one of them:
        # widening a scope onto it just moves the failure into that operation.
        undersized = [f for f in missing if f.get('too_small')]
        missing = [f for f in missing if not f.get('too_small')]
        if undersized:
            what = ', '.join(sorted({describe(f) for f in undersized}))
            errors.append(
                f"{part.name}: {len(undersized)} {kind}(s) are smaller than every tool "
                f"in this job ({what}). Add a drill that size, or centre-mark them for "
                f"hand drilling with a spot operation covering them.")
        if missing:
            what = ', '.join(sorted({describe(f) for f in missing}))
            errors.append(
                f"{part.name}: {len(missing)} {kind}(s) are not cut by any operation "
                f"({what}). Add a {'holes' if kind == 'hole' else 'pockets'} operation for "
                f"them, or widen an existing operation's scope.")

    return errors, warnings


def _validate_profile_order(job: MultiToolJob) -> List[str]:
    """Refuse plans that would keep cutting after a part has been freed from the stock.

    Tabs are what make "profile, then chamfer" safe: the part stays anchored until a
    deliberate tab-removal pass at the very end. When the team config turns tabs off, a
    perimeter cuts clean through, so anything scheduled afterwards - a chamfer on that
    part, or another part's profile beside it - runs next to (or on) a part lying loose
    under a spinning cutter. There is no toolpath that makes that safe, so it is an error
    at planning time rather than a warning in the output.

    With tabs enabled this is all fine and nothing is reported: `defer_tabs` holds the
    removal pass back to the end of the program.

    `remove_tabs: false` is fine too, and used to be refused. The deferral in
    generate_operation is gated on remove_tabs, so with it off no removal pass is emitted
    ANYWHERE and the part comes off the machine still held by its tabs, for someone to
    cut out by hand. The plan is safe and the message the check produced - "the part is
    cut free and left loose on the table" - was simply untrue. Only the absence of tabs
    frees the part.
    """
    if job.config.tabs_enabled:
        return []

    errors = []
    multi_part = len(job.parts) > 1
    reason = "tabs are disabled in your configuration"

    for part in job.parts:
        perimeter_at = next((i for i, op in enumerate(part.operations)
                             if op.op_type == 'perimeter'), None)
        if perimeter_at is None:
            continue
        later = part.operations[perimeter_at + 1:]
        if later:
            errors.append(
                f"{part.name}: {len(later)} operation(s) run after the profile "
                f"({', '.join(op.label for op in later)}), but {reason}, so the part is "
                f"cut free and left loose on the table. Move them before the profile, or "
                f"enable tabs.")
        elif multi_part:
            errors.append(
                f"{part.name}: its profile cuts the part free ({reason}) while other "
                f"parts on the sheet are still being machined. Enable tabs, or run this "
                f"part as its own job.")
    return errors


#: When a tool does not state its flute length, this many diameters is taken as the
#: point past which the cut is worth mentioning. Stub-length end mills are commonly
#: around 3xD and standard ones 3-4xD, so a cut deeper than 4xD is past most cutters.
ASSUMED_FLUTE_LENGTH_DIAMETERS = 4.0


def _check_tool_reach(pp: FRCPostProcessor, tool: Tool) -> Optional[str]:
    """Warn when the cut is deeper than the cutter can reach. Never refuses.

    Flute length is the depth a cutter can actually cut to; past it the shank is rubbing
    the wall, which is how a bit gets snapped in a deep pocket. Nothing in PenguinCAM
    knew the number, so a program could ask a stub-length bit for a half-inch cut and
    only the shank found out.

    Advice rather than refusal on purpose: PenguinCAM cannot see how far the tool sticks
    out of the collet, and a shop that knows its own reach knows better than the program.
    """
    depth = pp.material_top - pp.cut_depth
    if depth <= 0:
        return None
    if tool.flute_length:
        if depth <= tool.flute_length + 1e-9:
            return None
        return (f"{tool.label} cuts {depth:.3f} in deep but its flutes are only "
                f"{tool.flute_length:.3f} in long. Past that the shank rubs the wall. "
                f"Use a longer cutter, or machine the part in two setups.")
    assumed = tool.diameter * ASSUMED_FLUTE_LENGTH_DIAMETERS
    if depth <= assumed + 1e-9:
        return None
    return (f"{tool.label} cuts {depth:.3f} in deep, which is more than 4x its "
            f"{tool.diameter:.3f} in diameter. Most end mills that size do not have "
            f"that much flute. Check the cutter reaches, and set its flute length in "
            f"the tool list so PenguinCAM can check for you.")


def _engrave_lines(job: MultiToolJob, part: PartOps, tool_diameter: float):
    """The part's name, cut with the tool that is already in the spindle.

    Built on its OWN post-processor rather than the operation's: an operation narrows
    `pp.holes` and `pp.pockets` to its own scope (a perimeter operation clears them
    entirely), and the engraving has to see every one of them to keep the label out of
    a bore that gets machined away later.
    """
    pp = build_part_postprocessor(job, part, tool_diameter)
    pp.classify_holes()
    pp.engrave = {
        'text': part.engrave_text or part.name,
        'height': part.engrave_height,
        'depth': part.engrave_depth,
        'anchor': part.engrave_anchor,
        'font_path': job.engraving_font_path,
        'font_name': job.engraving_font_name,
        'strict_size': part.engrave_strict_size or bool(job.engraving_font_path),
    }
    try:
        lines = pp._engrave_body()
    except Exception as exc:            # a label is never worth failing a job over
        return [], [f'{part.name}: the name could not be engraved ({exc}).']
    return lines, list(pp.warnings)


def generate_multitool_job(job: MultiToolJob, timestamp: Optional[str] = None,
                           suggested_filename: Optional[str] = None) -> PostProcessorResult:
    """Full pipeline: survey every part, run every operation, order and stitch them.

    On failure the result carries every part's errors at once rather than stopping at the
    first, so a student fixes one list instead of rediscovering problems one run at a time.
    """
    # Plan-level checks that need no geometry run first, so an obviously wrong tool
    # assignment is reported as such instead of surfacing later as whatever downstream
    # complaint happens to fire first.
    errors: List[str] = []
    # Before anything reads a DXF: does anyone have feeds for this material? A material
    # nobody knows used to reach the generator and come back out as plywood numbers.
    try:
        resolve_feeds_material(job.material, job.config, job.machine_id)
    except ToolingError as exc:
        return PostProcessorResult(success=False, errors=[str(exc)])
    for part in job.parts:
        for op in part.operations:
            mismatch = _check_tool_suits_operation(op, job.tool(op.tool_slot))
            if mismatch:
                errors.append(f"{part.name}: {mismatch}")
    if errors:
        return PostProcessorResult(success=False, errors=errors)

    surveys = []
    for part in job.parts:
        features = survey_part(job, part)
        surveys.append(features)
        errors.extend(f"{part.name}: {e}" for e in features['errors'])

    coverage_warnings: List[str] = []
    for part, features in zip(job.parts, surveys):
        part_errors, part_warnings = _validate_feature_coverage(part, features)
        errors.extend(part_errors)
        coverage_warnings.extend(part_warnings)

    # A profile is the cut that frees the part, and multi-tool is the first mode that can
    # schedule work AFTER one - so before generating anything, refuse any plan where a
    # part would still be loose on the table while the machine keeps cutting. Tabs are the
    # only thing that makes "profile, then chamfer" safe; with tabs turned off there is
    # nothing holding the part and no toolpath change can rescue it.
    errors.extend(_validate_profile_order(job))
    if errors:
        return PostProcessorResult(success=False, errors=errors)

    # Tabs must survive until nothing else will be cut near the part. That means holding
    # a perimeter's tab-removal pass back whenever the same part has further operations
    # (a chamfer would otherwise run on a part loose on the table), and whenever the sheet
    # carries other parts still to be profiled.
    multi_part = len(job.parts) > 1

    bodies = []
    deferred = []
    engraved = set()
    sequence = order_operations(
        job.parts,
        split_after_holes=job.config.pause_after_holes,
        split_before_perimeter=job.config.pause_before_perimeter,
    )
    for part_index, op_index in sequence:
        part = job.parts[part_index]
        defer_tabs = multi_part or op_index < len(part.operations) - 1
        body = generate_operation(job, part, part.operations[op_index],
                                  surveys[part_index], defer_tabs=defer_tabs)
        if body['errors']:
            errors.extend(f"{part.name} / {body['op'].label}: {e}" for e in body['errors'])
        # The name goes on before anything frees the part, so it rides the FIRST body
        # this part produces - `order_operations` puts interior work ahead of perimeters,
        # so that is the earliest point the part is still solid stock. Multi-tool jobs
        # used to drop the engraving on the floor entirely while the summary said
        # "names engraved".
        # ...and on a body whose tool can actually WRITE. A twist drill has no
        # peripheral cutting edge and no radial rigidity, and drilled holes are ordered
        # first, so attaching to the literal first body loaded a drill, promised "axial
        # plunge only", then fed it sideways at 75 IPM to draw the part name.
        if job.engrave and part_index not in engraved and body['tool'].type != 'drill':
            engraved.add(part_index)
            lines, warnings = _engrave_lines(job, part, body['tool'].diameter)
            body['lines'] = lines + body['lines']
            body['warnings'] = list(body['warnings']) + warnings
        bodies.append(body)
        if body.get('deferred'):
            deferred.append(body['deferred'])

    if errors:
        return PostProcessorResult(success=False, errors=errors)

    # A part machined entirely with drills has no tool that can write. Say so - a name
    # that never got cut is exactly the kind of silence this feature exists to avoid.
    job_warnings: List[str] = list(coverage_warnings)
    if job.engrave:
        for index, part in enumerate(job.parts):
            if index not in engraved:
                job_warnings.append(
                    f"{part.name}: the name was not engraved. Every operation on this "
                    f"part uses a twist drill, which cannot cut sideways. Add an end "
                    f"mill operation to engrave it.")

    # Order the held-back tab removals so the tool already in the spindle goes first; the
    # rest group by tool, so the run costs at most one change per perimeter tool.
    if deferred:
        last_slot = bodies[-1]['tool'].slot if bodies else None
        deferred.sort(key=lambda b: (b['tool'].slot != last_slot, b['tool'].slot))
        bodies.extend(deferred)

    return assemble_job(job, bodies, timestamp=timestamp,
                        suggested_filename=suggested_filename,
                        extra_warnings=job_warnings)


# --------------------------------------------------------------------------- job specs

def _expect_list(value: Any, what: str) -> list:
    """A list, or a ToolingError naming the field. `None` and missing mean empty."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ToolingError(f"{what} must be a list, got {type(value).__name__}")
    return list(value)


def _expect_number(value: Any, what: str) -> float:
    try:
        return _finite(value, what)
    except (TypeError, ValueError) as exc:
        raise ToolingError(f"{what} must be a number: {exc}") from exc


def _expect_positive(value: Any, what: str) -> float:
    try:
        return _positive_finite(value, what)
    except (TypeError, ValueError) as exc:
        raise ToolingError(f"{what}: {exc}") from exc


def _expect_z_datum(value: Any) -> Optional[str]:
    """Validate the Z datum here rather than letting the post-processor raise mid-build:
    a bad datum is a bad request, and it should read as one."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return normalize_z_datum(value)
    except ValueError as exc:
        raise ToolingError(f"z_datum: {exc}") from exc


def _expect_int(value: Any, what: str) -> int:
    try:
        return _integer(value, what)
    except (TypeError, ValueError) as exc:
        raise ToolingError(f"{what} must be a whole number: {exc}") from exc


def _expect_bool(value: Any, what: str) -> bool:
    """Require a JSON boolean instead of applying Python's truthiness rules.

    In particular, ``bool('false')`` is true.  Accepting that value for ``mirror``
    silently flips a part and accepting it for ``engrave`` silently adds machining.
    """
    if not isinstance(value, bool):
        raise ToolingError(f"{what} must be true or false, got {value!r}")
    return value


# Bad input here is ordinary - a blank row in the tool table, a field the UI left null,
# a hand-written job file with a typo - and every one of those used to escape as a
# TypeError or AttributeError and surface as an HTTP 500 with a traceback (one of them
# leaking a server temp path). They are all ToolingError now, which the routes already
# render as a 400 the user can act on.
def job_from_dict(spec: Dict[str, Any], dxf_paths: Dict[int, str],
                  config: Optional[TeamConfig] = None,
                  user_name: Optional[str] = None,
                  engraving_font_path: Optional[str] = None,
                  engraving_font_name: Optional[str] = None) -> MultiToolJob:
    """Build a `MultiToolJob` from the JSON the web UI and the CLI both speak.

    `dxf_paths` maps each part's `file_index` to a DXF already on disk, so the web route
    can hand over uploaded temp files and the CLI can hand over paths from the job file.
    """
    if not isinstance(spec, dict):
        raise ToolingError("The job specification must be an object")

    tools = [Tool.from_dict(t) for t in _expect_list(spec.get('tools'), 'tools')]

    parts: List[PartOps] = []
    for i, raw in enumerate(_expect_list(spec.get('parts'), 'parts')):
        if not isinstance(raw, dict):
            raise ToolingError(f"Part {i + 1} must be an object, got "
                               f"{type(raw).__name__}")
        file_index = _expect_int(raw.get('file_index', i), f'part {i + 1} file_index')
        if file_index not in dxf_paths:
            raise ToolingError(f"Part {i + 1} references DXF #{file_index}, which was not provided")
        anchor = None
        if raw.get('engrave_anchor_x') is not None or raw.get('engrave_anchor_y') is not None:
            anchor = (_expect_number(raw.get('engrave_anchor_x'),
                                     f'part {i + 1} engrave_anchor_x'),
                      _expect_number(raw.get('engrave_anchor_y'),
                                     f'part {i + 1} engrave_anchor_y'))
        parts.append(PartOps(
            dxf_path=dxf_paths[file_index],
            name=raw.get('name') or f'part{i + 1}',
            place_x=_expect_number(raw.get('place_x', 0.0), f'part {i + 1} place_x'),
            place_y=_expect_number(raw.get('place_y', 0.0), f'part {i + 1} place_y'),
            rotation=_expect_number(raw.get('rotation', 0.0), f'part {i + 1} rotation'),
            mirror=_expect_bool(raw.get('mirror', False), f'part {i + 1} mirror'),
            engrave_text=(str(raw.get('engrave_text'))[:200]
                          if raw.get('engrave_text') is not None else None),
            engrave_anchor=anchor,
            engrave_height=_expect_positive(raw.get('engrave_height', ENGRAVE_HEIGHT_IN),
                                             f'part {i + 1} engrave_height'),
            engrave_depth=_expect_positive(raw.get('engrave_depth', ENGRAVE_DEPTH_IN),
                                            f'part {i + 1} engrave_depth'),
            engrave_strict_size=('engrave_height' in raw),
            operations=[Operation.from_dict(o)
                        for o in _expect_list(raw.get('operations'),
                                              f'part {i + 1} operations')],
        ))

    return MultiToolJob(
        material=spec.get('material', 'plywood'),
        thickness=_expect_positive(spec.get('thickness', 0.25), 'thickness'),
        tools=tools,
        parts=parts,
        machine_id=spec.get('machine_id'),
        feeds_machine=spec.get('feeds_machine'),
        tab_spacing=_expect_positive(spec.get('tab_spacing', 6.0), 'tab_spacing'),
        drill_size_tolerance=_expect_positive(
            spec.get('drill_size_tolerance', DEFAULT_DRILL_SIZE_TOLERANCE),
            'drill_size_tolerance'),
        sacrifice_depth=(_expect_positive(spec['sacrifice_depth'], 'sacrifice_depth')
                         if spec.get('sacrifice_depth') is not None else None),
        z_datum=_expect_z_datum(spec.get('z_datum')),
        dry_run_lift=(_expect_positive(spec['dry_run_lift'], 'dry_run_lift')
                      if spec.get('dry_run_lift') else 0.0),
        engrave=_expect_bool(spec.get('engrave', False), 'engrave'),
        engraving_font_path=engraving_font_path,
        engraving_font_name=engraving_font_name,
        max_pass_depth=(_expect_positive(spec['max_pass_depth'], 'max_pass_depth')
                        if spec.get('max_pass_depth') is not None else None),
        config=config,
        user_name=user_name,
        name=spec.get('name', 'job'),
    )


#: Above this a hole is bored with an end mill rather than drilled, even when a standard
#: drill of that size exists. A 1/2 in twist drill in a router is a lot of tool for a
#: hole an end mill can bore accurately, and big drills are where a light spindle bogs.
MAX_SUGGESTED_DRILL = 0.4


def suggest_tooling(features: Dict[str, Any], available: Sequence[Tool] = None,
                    mill_diameter: float = None,
                    include_chamfer: bool = False) -> Dict[str, Any]:
    """Propose a COMPLETE plan for a part: the tools to load and the operations to run.

    Starts from the geometry, not from a tool list: reads the hole sizes, picks the drills
    that make them, adds one end mill for the pockets and profile. `available` tools are
    REUSED where they fit, so the plan does not ask anyone to load a second 1/4 in end
    mill they already have; anything the available tools cannot do is proposed as a new
    tool, flagged by `is_new` on the returned Tool list.

    This replaced a second, tool-constrained suggester. Having both was a trap: the
    tool-constrained one could only assign work to cutters that already existed, so a part
    surveyed with just a default end mill in the table got a plan that milled every hole -
    and it was the one that auto-filled, while the better answer sat behind a button. Two
    suggestion paths that can disagree is exactly how the wrong drill gets proposed.

    Nothing here is binding. It is a starting point the user edits.
    """
    available = list(available or [])
    mill_diameter = mill_diameter or DEFAULT_MILL_DIAMETER
    tools: List[Tool] = []
    operations: List[Operation] = []
    notes: List[str] = []
    used_slots = {t.slot for t in available}

    def next_slot() -> int:
        slot = 1
        while slot in used_slots:
            slot += 1
        used_slots.add(slot)
        return slot

    def adopt(match, kind: str, diameter: float, **kwargs) -> Tool:
        """Reuse an available tool of the right kind and size, else propose a new one."""
        for tool in available:
            if tool.type == kind and abs(tool.diameter - diameter) < 5e-4:
                return tool
        tool = Tool(next_slot(), match, diameter, kwargs.pop('flutes', 2),
                    type=kind, **kwargs)
        tools.append(tool)
        return tool

    hole_sizes = list(features.get('hole_sizes') or [])
    drilled, milled = [], []
    for size in hole_sizes:
        match = drill_sizes.nearest_drill(size)
        if match and size <= MAX_SUGGESTED_DRILL:
            drilled.append((size, match))
        else:
            milled.append(size)

    for size, match in drilled:
        tool = adopt(f'{match.label} drill', 'drill', match.diameter)
        operations.append(Operation(
            'holes', tool.slot, f'Drill {match.label}',
            scope={'min_diameter': size - 1e-4, 'max_diameter': size + 1e-4}))
        if abs(match.diameter - size) > 1e-6:
            notes.append(f'{size:.4f} in holes will be drilled at {match.diameter:.4f} in '
                         f'({match.label}).')

    # `milled` holds every size no drill matches - but an end mill can only BORE a hole
    # it fits inside. A size smaller than the mill (the survey lists such holes rather
    # than rejecting them) cannot be cut by anything here, so it gets a spot operation:
    # a centre-mark for the drill press, which is the one thing the machine CAN do to it.
    # Sweeping it into the bore range instead proposed a plan that failed at generation.
    existing = sorted([t for t in available if t.type == 'endmill'],
                      key=lambda t: t.diameter)
    planned_mill_dia = existing[-1].diameter if existing else mill_diameter
    bored = [s for s in milled if s >= planned_mill_dia - 1e-4]
    tiny = [s for s in milled if s < planned_mill_dia - 1e-4]

    needs_mill = bool(bored) or features.get('pockets') or features.get('has_perimeter')
    mill = None
    if needs_mill:
        # Prefer the biggest end mill already loaded: it is what the operator has, and a
        # bigger cutter clears a pocket faster. Only propose a new one if there is none.
        mill = (existing[-1] if existing
                else adopt(f'{mill_diameter:.4f} in end mill', 'endmill', mill_diameter))

    if tiny:
        # Any drill will do for a dimple - size is irrelevant to a spot - so reuse one
        # already loaded or already proposed before asking for a centre drill.
        spot_tool = (next((t for t in available if t.type == 'drill'), None)
                     or next((t for t in tools if t.type == 'drill'), None)
                     or adopt('1/8 in centre drill', 'drill', 0.125))
        operations.append(Operation(
            'holes', spot_tool.slot, 'Centre-mark undersized holes',
            scope={'min_diameter': min(tiny) - 1e-4, 'max_diameter': max(tiny) + 1e-4,
                   'purpose': drill_sizes.PURPOSE_SPOT}))
        notes.append('No tool here can make these holes, so they are centre-marked for '
                     'the drill press instead: '
                     + ', '.join(f'{d:.4f} in' for d in tiny) + '. The dimples are not '
                     'holes - they still have to be drilled by hand.')

    if bored:
        operations.append(Operation(
            'holes', mill.slot, 'Bore large holes',
            scope={'min_diameter': min(bored) - 1e-4, 'max_diameter': max(bored) + 1e-4}))
        notes.append('Holes too large to drill are bored with the end mill: '
                     + ', '.join(f'{d:.4f} in' for d in bored) + '.')
    if features.get('pockets'):
        operations.append(Operation('pockets', mill.slot, 'Pockets'))
    if features.get('has_perimeter'):
        operations.append(Operation('perimeter', mill.slot, 'Profile'))

    if include_chamfer and features.get('has_perimeter'):
        vbit = adopt('1/2 in 90 deg V-bit', 'vbit', 0.5, included_angle=90.0)
        operations.append(Operation('chamfer', vbit.slot, 'Edge break',
                                    scope={'targets': ['perimeter'], 'width': 0.02}))

    return {'tools': tools, 'operations': operations, 'notes': notes,
            'reused': [t for t in available if any(o.tool_slot == t.slot
                                                   for o in operations)]}
