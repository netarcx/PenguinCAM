"""Feeds & speeds calculation core for FRC CNC routers.

This module is intentionally free of any web-framework or PenguinCAM dependency so
that the same model can later be imported directly by ``frc_cam_postprocessor.py``
(the v3 config path) as well as served by the standalone web calculator.

The core problem this addresses: PenguinCAM's config stores *fixed* feeds/speeds per
material, all tuned for a 4mm single-flute tool. Those numbers stop working when the
tool changes (diameter or flute count). Here we *derive* feeds/speeds from
machine + material + tool inputs so that varying the tool still yields sane numbers.

Model (all lengths in inches, feeds in IPM, RPM in rev/min)::

    chipload_target = chipload_ref * (D / D_ref) ** DIAMETER_EXPONENT * op_factor
    rpm             = clamp(material.preferred_rpm, machine.rpm_min, machine.rpm_max)
    feed_flutes     = min(flutes, material.feed_flutes_max)   # evacuation limit (metals)
    feed_raw        = rpm * feed_flutes * chipload_target * rigidity_factor
    feed            = min(feed_raw, machine.xy_feed_max)          # machine limit
    chipload_done   = feed / (rpm * flutes)                       # achieved chipload
    ramp_feed       = feed * ramp_multiplier
    peck_feed       = min(feed * plunge_multiplier, machine.z_feed_max)
    stepover        = stepover_ratio * D
    slot_stepdown   = slot_stepdown_ratio * D

``op_factor`` is the material ``slotting_multiplier`` for full-engagement slot/profile
cuts (what the PenguinCAM presets represent) and 1.0 for pocket/clearing, where the
lower radial engagement lets the tool run at the full reference chipload.

Constants are seeded from published tooling references and validated against the
existing PenguinCAM presets by ``validate_feeds_speeds.py`` (they land within ~10%).
"""

import math
import re

# --- Reference tool the material chipload constants are quoted for --------------
REFERENCE_TOOL = {'diameter': 0.157, 'flutes': 1}   # 4mm single-flute

# How chipload scales with tool diameter (from docs/FEEDSandSPEEDS.md).
DIAMETER_EXPONENT = 0.70

# Machine rigidity multiplies feed: a stiffer machine can push a bigger chip.
RIGIDITY_FACTOR = {'light': 0.85, 'medium': 1.00, 'heavy': 1.10}

# Operations that engage the tool at full width (slotting). Others (pocket/clearing)
# run at lower radial engagement and can use the full reference chipload.
FULL_SLOT_OPERATIONS = {'profile', 'slot'}

# Hard ceiling for the generic router 6061/6063 preset. This is deliberately separate from
# the chipload model: team config files can outlive releases, and the old generated
# template wrote 55 IPM / 0.200 in into every new config.  Because machine config wins
# over built-in defaults, those stale values silently defeated the 2026-08-24 derate.
# Treat these as a safety envelope, not tuning targets; a config may always ask for less.
ALUMINUM_ROUTER_SAFETY_MAX = {
    'feed_rate': 30.0,
    'ramp_feed_rate': 19.0,
    'plunge_rate': 15.0,
    'ramp_angle': 4.0,
    'stepover_percentage': 0.25,
    'helix_radius_multiplier': 0.5,
    'max_slotting_depth': 0.06,
    'peck_drill_depth': 0.05,
}


# Fraction of a spindle's plate rating that is actually available at the cutter. Router
# spindles are rated optimistically, lose output under load, and drive through a belt or
# a collet that slips before the motor stalls. 70% is the usual working assumption.
USABLE_POWER_FRACTION = 0.70
KW_TO_HP = 1.341

MACHINES = {
    'omio_x8': {
        'name': 'Omio X8-2200',
        'rpm_min': 6000, 'rpm_max': 24000,
        'xy_feed_max': 150.0, 'z_feed_max': 60.0,
        'rigidity': 'medium',
        'spindle_kw': 2.2,
    },
    'avid_pro2424': {
        'name': 'Avid CNC Pro2424',
        'rpm_min': 6000, 'rpm_max': 24000,
        'xy_feed_max': 400.0, 'z_feed_max': 100.0,
        'rigidity': 'heavy',
        'spindle_kw': 2.2,
    },
    'generic_light_router': {
        'name': 'Generic light router',
        'rpm_min': 8000, 'rpm_max': 30000,
        'xy_feed_max': 100.0, 'z_feed_max': 40.0,
        'rigidity': 'light',
        'spindle_kw': 1.25,
    },
}


def usable_horsepower(machine):
    """Horsepower actually available at the cutter, or None if the spindle is unrated."""
    kw = _resolve(machine, MACHINES, 'machine').get('spindle_kw')
    return kw * KW_TO_HP * USABLE_POWER_FRACTION if kw else None


def max_depth_for_power(machine, material, diameter, feed, radial_engagement=None):
    """Deepest axial cut this spindle can drive, in inches, or None if unlimited.

    Cutting power is roughly ``MRR x unit_power``, and MRR is ``axial x radial x feed`` -
    so for a given cutter and feed there is a depth beyond which the spindle simply bogs.
    On a router that is not a graceful failure: the cutter grabs, the tool deflects, and
    an end mill snaps. It is the binding limit in aluminium with a large cutter, and it
    is invisible to a chipload model, which only ever looks at one tooth at a time.

    `radial_engagement` defaults to the full diameter - a profile cut is a slot, with the
    part on one side and the offcut on the other, and that is the worst case the same
    depth setting has to survive.
    """
    unit_power = _resolve(material, MATERIALS, 'material').get('unit_power_hp')
    available = usable_horsepower(machine)
    if not unit_power or not available or feed <= 0:
        return None
    radial = radial_engagement or diameter
    if radial <= 0:
        return None
    max_mrr = available / unit_power
    return max_mrr / (radial * feed)


# chipload_ref values are in/tooth for the REFERENCE_TOOL (4mm 1F). slotting_multiplier
# derates them for full-width slotting; the product is what reproduces the presets.
#
# ramp_multiplier derated 2026-09-01 (0.64 -> 0.40) for the materials that do not
# seize: a ramp is a full-width slot with AXIAL engagement stacked on top - the worst
# cut in the program - and running it at nearly two-thirds of the cutting feed was
# snapping real bits on perimeter entry. Wood and plastics tolerate a slow ramp.
#
# ALUMINUM IS THE OPPOSITE. Slower is NOT safer in metal: a feed below the minimum
# chipload rubs instead of cutting - the cutter heats, aluminum welds to the flutes,
# and the seized tool shatters. The first derate here took the aluminum ramp to
# 9 ipm at 12000 RPM (0.0008 in/tooth, half the floor) and a real 1/4 in end mill
# shattered on entry the same day. Aluminum keeps its tested 0.64 ratio, and
# apply_tool_feeds floors the final ramp feed at rpm x flutes x chipload_min so no
# clamp or rescale can push an entry into the rubbing regime again.
MATERIALS = {
    'plywood': {
        'unit_power_hp': 0.05,
        'name': 'Plywood',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0050, 'chipload_min': 0.0020, 'chipload_max': 0.0090,
        'slotting_multiplier': 0.80,
        'ramp_multiplier': 0.40, 'plunge_multiplier': 0.46,
        'stepover_ratio': 0.65, 'slot_stepdown_ratio': 2.55,
        'max_flutes_soft': 2,
    },
    'polycarbonate': {
        'unit_power_hp': 0.08,
        'name': 'Polycarbonate',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0050, 'chipload_min': 0.0025, 'chipload_max': 0.0090,
        'slotting_multiplier': 0.80,
        'ramp_multiplier': 0.40, 'plunge_multiplier': 0.26,
        'stepover_ratio': 0.55, 'slot_stepdown_ratio': 1.59,
        'max_flutes_soft': 1,
    },
    'hdpe': {
        'unit_power_hp': 0.05,
        'name': 'HDPE',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0060, 'chipload_min': 0.0030, 'chipload_max': 0.0110,
        'slotting_multiplier': 0.83,
        'ramp_multiplier': 0.40, 'plunge_multiplier': 0.30,
        'stepover_ratio': 0.55, 'slot_stepdown_ratio': 1.60,
        'max_flutes_soft': 1,
    },
    'srpp': {
        'unit_power_hp': 0.06,
        'name': 'SRPP (polypropylene composite)',
        'preferred_rpm': 18000,
        'chipload_ref': 0.0050, 'chipload_min': 0.0025, 'chipload_max': 0.0090,
        'slotting_multiplier': 0.80,
        'ramp_multiplier': 0.40, 'plunge_multiplier': 0.28,
        'stepover_ratio': 0.55, 'slot_stepdown_ratio': 1.59,
        'max_flutes_soft': 1,
    },
    'aluminum_6061': {
        # Derated 2026-08-24 in lockstep with the aluminum preset (30 IPM, 0.06"
        # slot at the 4 mm reference): the old constants reproduced the 55 IPM /
        # 1.27 x D numbers that snapped real 1/8" cutters. slotting_multiplier and
        # slot_stepdown_ratio now land on the derated preset; chipload_ref stays for
        # partial-engagement work, and chipload_min drops to 0.0010 - conservative
        # single-flute practice on light routers runs there without rubbing, and a
        # A 0.0015 floor is protected by coordinating RPM with both straight and
        # slowed-corner feed, rather than allowing a low feed to rub at 18,000 RPM.
        'unit_power_hp': 0.3,
        'name': '6061 Aluminum',
        'preferred_rpm': 14000,
        'chipload_ref': 0.0032, 'chipload_min': 0.0015, 'chipload_max': 0.0050,
        'slotting_multiplier': 0.52,
        'ramp_multiplier': 0.64, 'plunge_multiplier': 0.28,
        'stepover_ratio': 0.25, 'slot_stepdown_ratio': 0.38,
        'max_flutes_soft': 3,
        # Feed never scales past this many flutes. Gummy 6061 in a slot cannot clear
        # chips from more gullets than this: the extra flutes recut and weld their
        # chips instead of evacuating them, the tool seizes, and it snaps - so feeding
        # a 4-flute at 4x the per-tooth rate commanded 150+ IPM on a 1/8 in cutter and
        # broke it. Metals seize where plastics melt and wood clears, which is why
        # only this entry carries the cap; elsewhere max_flutes_soft stays advisory.
        'feed_flutes_max': 2,
    },
}

# 6063 forms a built-up edge more readily than 6061. There is no honest universal
# numeric derate without the exact cutter, temper, and lubricant, so use the same
# deliberately conservative router envelope and distinguish the alloy so preflight can
# require lubricant. Keeping a separate key also prevents 6063 becoming plywood.
MATERIALS['aluminum_6063'] = {
    **MATERIALS['aluminum_6061'],
    'name': '6063 Aluminum',
}


#: Word-tokens that name the aluminum family. Matched as whole words after splitting
#: on anything that is not a letter, never as substrings - "alder" contains "al" and is
#: a wood, and a wood running at aluminum feeds is as wrong as the reverse.
ALUMINUM_WORDS = frozenset({'al', 'alu', 'aluminum'})

#: Alloy designations that identify aluminum wherever they appear in an id, so
#: "al6061", "6061-T6" and "AL 7075" all land in the family.
ALUMINUM_ALLOY_MARKERS = ('6061', '6063', '7075')


def canonical_material_key(material):
    """Return the feeds-model key for a known material spelling, or ``None``.

    Alloy names arrive from YAML, the web API, saved jobs, and the CLI. Treat every
    normal spelling of 6061/6063 as aluminum before any fallback can select plywood.
    An unspecified ``aluminum`` resolves as 6063, the less-machinable alloy, so the
    generic preset is conservative for either alloy - and so does 7075, which the model
    does not carry its own numbers for.

    Returning ``None`` is a real answer: nothing in the model knows this material, and
    callers must refuse rather than substitute a table they have no reason to trust.
    """
    token = re.sub(r'[^a-z0-9]+', '_', str(material or '').strip().lower()).strip('_')
    token = token.replace('aluminium', 'aluminum')
    if not token:
        return None
    words = set(w for w in re.split(r'[^a-z]+', token) if w)
    if words & ALUMINUM_WORDS or any(m in token for m in ALUMINUM_ALLOY_MARKERS):
        # 6061 is the one grade with its own numbers. 6063, 7075 and unspecified
        # aluminum all take the 6063 preset: it is the most conservative of the three.
        return 'aluminum_6061' if '6061' in token else 'aluminum_6063'
    aliases = {'polycarb': 'polycarbonate'}
    resolved = aliases.get(token, token)
    return resolved if resolved in MATERIALS else None


def is_aluminum_material(material):
    """Whether ``material`` denotes 6061/6063 or the generic aluminum family."""
    return canonical_material_key(material) in ('aluminum_6061', 'aluminum_6063')


# --- Twist drilling -------------------------------------------------------------
# Drilling is quoted differently from milling, and the milling model above does not
# transfer: there is no chipload per tooth to speak of, no radial engagement, and the
# only motion is axial. The two numbers that matter are surface speed (which sets RPM
# for a given diameter) and feed per revolution (which sets the plunge rate).
#
# `sfm` is a conservative HSS figure - carbide will take more, but a twist drill in a
# team's tool crib is usually HSS and running one too fast is how it gets burned.
# `ipr_ref` is feed per revolution at the reference diameter; it scales with the square
# root of diameter, since a bigger drill takes a proportionally lighter cut per unit of
# its own size.
DRILL_REFERENCE_DIAMETER = 0.25

DRILLING = {
    'plywood':       {'sfm': 300, 'ipr_ref': 0.006},
    'polycarbonate': {'sfm': 200, 'ipr_ref': 0.004},   # melts if it rubs
    'hdpe':          {'sfm': 300, 'ipr_ref': 0.007},
    'srpp':          {'sfm': 250, 'ipr_ref': 0.005},
    'aluminum_6061': {'sfm': 250, 'ipr_ref': 0.004},
    'aluminum_6063': {'sfm': 200, 'ipr_ref': 0.0035},
}

DRILL_IPR_EXPONENT = 0.5


def calculate_drill_feeds(machine, material, tool):
    """RPM and plunge feed for a twist drill.

    Returns the same shape as `calculate_feeds` for the fields a drilling operation
    needs (``rpm``, ``plunge_feed``, ``warnings``), plus the derived figures so the
    numbers can be explained rather than just asserted.

    The warning that matters on a router: the ideal drilling RPM for anything but a very
    small drill is below what a 2.2 kW router spindle will turn. Clamping up to the
    spindle minimum is the only option, and the operator should know the drill is being
    run fast so they can slow the feed or accept a shorter tool life.
    """
    m = _resolve(machine, MACHINES, 'machine')
    mat_key = canonical_material_key(material) if isinstance(material, str) else None
    drill = DRILLING.get(mat_key)
    if drill is None:
        # Falling back to plywood here ran a twist drill in an unknown metal at 300 SFM
        # and wood's feed per revolution. There is no safe guess for a material nobody
        # has quoted; refuse and let the caller say so.
        raise ValueError(
            f"No drilling data for material {material!r}. Known drilling materials: "
            + ', '.join(sorted(DRILLING)))

    diameter = float(tool['diameter'])
    if diameter <= 0:
        raise ValueError("drill diameter must be positive")

    warnings = []

    ideal_rpm = (drill['sfm'] * 12.0) / (math.pi * diameter)
    rpm = _clamp(ideal_rpm, m['rpm_min'], m['rpm_max'])
    if rpm > ideal_rpm * 1.05:
        warnings.append(
            f"Drilling {diameter:.3f} in wants about {ideal_rpm:.0f} RPM "
            f"({drill['sfm']} SFM), but the spindle floor is {m['rpm_min']:.0f} RPM. "
            f"The drill will run hot - peck often, and expect reduced tool life.")
    elif rpm < ideal_rpm * 0.95:
        warnings.append(
            f"Drilling {diameter:.3f} in wants about {ideal_rpm:.0f} RPM, above the "
            f"spindle's {m['rpm_max']:.0f} RPM ceiling; feed reduced to match.")

    ipr = drill['ipr_ref'] * (diameter / DRILL_REFERENCE_DIAMETER) ** DRILL_IPR_EXPONENT
    plunge_raw = rpm * ipr
    plunge = min(plunge_raw, m['z_feed_max'])
    if plunge_raw > m['z_feed_max']:
        warnings.append(
            f"Drill plunge clamped by the machine's Z limit: wanted {plunge_raw:.1f} IPM, "
            f"max is {m['z_feed_max']:.0f} IPM.")

    return {
        'rpm': round(rpm),
        'plunge_feed': round(plunge, 1),
        'ideal_rpm': round(ideal_rpm),
        'ipr': round(ipr, 5),
        'sfm': drill['sfm'],
        'warnings': warnings,
        'formulas': [
            "rpm   = clamp(SFM * 12 / (pi * D), machine rpm range)",
            f"ipr   = ipr_ref * (D / {DRILL_REFERENCE_DIAMETER})^{DRILL_IPR_EXPONENT}",
            "plunge = min(rpm * ipr, machine z_feed_max)",
        ],
    }


TOOL_PRESETS = {
    '3mm_1f': {'name': '3mm 1-flute', 'diameter': 0.118, 'flutes': 1},
    '4mm_1f': {'name': '4mm 1-flute (default)', 'diameter': 0.157, 'flutes': 1},
    '125_1f': {'name': '1/8" 1-flute', 'diameter': 0.125, 'flutes': 1},
    '250_1f': {'name': '1/4" 1-flute', 'diameter': 0.250, 'flutes': 1},
    '250_2f': {'name': '1/4" 2-flute', 'diameter': 0.250, 'flutes': 2},
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def _resolve(spec, presets, kind):
    """Resolve a machine/material argument to a dict.

    ``spec`` may be a preset key (str), or a dict. A dict may carry a ``preset`` key
    naming a base preset whose values are overlaid with the remaining keys (so the
    public 'custom' path can start from a preset and tweak a field or two).
    """
    if isinstance(spec, str):
        if spec not in presets:
            raise ValueError(f"Unknown {kind}: {spec!r}. Options: {sorted(presets)}")
        return dict(presets[spec])
    if isinstance(spec, dict):
        base = {}
        if spec.get('preset'):
            # Named, so it must exist. Resolving an unknown name to an empty base left
            # the caller's handful of overrides standing in for the whole spec, and the
            # first field the model reached for was simply absent - surfacing in the
            # calculator API as `KeyError: 'preferred_rpm'`, a 500 with nothing in it
            # for whoever typed the name.
            if spec['preset'] not in presets:
                raise ValueError(f"Unknown {kind} preset: {spec['preset']!r}. "
                                 f"Options: {sorted(presets)}")
            base = dict(presets[spec['preset']])
        base.update({k: v for k, v in spec.items() if k != 'preset'})
        return base
    raise TypeError(f"{kind} must be a preset key or dict, got {type(spec).__name__}")


def calculate_feeds(machine, material, tool, operation='profile'):
    """Compute derived feeds & speeds.

    Args:
        machine: preset key (e.g. ``'omio_x8'``) or a dict of machine fields.
        material: preset key (e.g. ``'plywood'``) or a dict of material fields.
        tool: dict with ``diameter`` (inches) and ``flutes``.
        operation: one of ``profile``, ``slot``, ``pocket``, ``clearing``,
            ``peck_drill``.

    Returns a dict of results, warnings (list of str), a prose ``explanation`` and
    the ``formulas`` used, suitable for direct JSON serialization.
    """
    m = _resolve(machine, MACHINES, 'machine')
    mat = _resolve(material, MATERIALS, 'material')

    diameter = float(tool['diameter'])
    flutes = int(tool['flutes'])
    if diameter <= 0 or flutes <= 0:
        raise ValueError("tool diameter and flutes must be positive")

    d_ref = REFERENCE_TOOL['diameter']
    rigidity = m.get('rigidity', 'medium')
    rigidity_factor = RIGIDITY_FACTOR.get(rigidity, 1.0)

    warnings = []

    # RPM: material preference clamped to the machine's spindle range.
    rpm = _clamp(mat['preferred_rpm'], m['rpm_min'], m['rpm_max'])
    if rpm != mat['preferred_rpm']:
        warnings.append(
            f"RPM clamped to machine range: preferred {mat['preferred_rpm']:.0f} -> "
            f"{rpm:.0f} (machine allows {m['rpm_min']:.0f}-{m['rpm_max']:.0f})")

    # Chipload target: reference chipload scaled by diameter, derated for slotting.
    is_slot = operation in FULL_SLOT_OPERATIONS
    op_factor = mat['slotting_multiplier'] if is_slot else 1.0
    diameter_scale = (diameter / d_ref) ** DIAMETER_EXPONENT
    chipload_target = mat['chipload_ref'] * diameter_scale * op_factor

    # Feed from chipload, boosted by machine rigidity, then clamped to machine limit.
    # Flutes multiply feed only up to the material's evacuation limit: in a gummy
    # metal, flutes past feed_flutes_max cannot clear their chips from a slot - they
    # recut and weld them, the tool seizes, and it snaps. Extra flutes therefore buy
    # no feed at all; the per-tooth chip just gets thinner (and the rubbing check
    # below will say so).
    feed_flutes = flutes
    flutes_capped = False
    feed_flutes_max = mat.get('feed_flutes_max')
    if feed_flutes_max and flutes > feed_flutes_max:
        feed_flutes = feed_flutes_max
        flutes_capped = True
        warnings.append(
            f"Feed held to the {feed_flutes_max}-flute rate: {flutes} flutes cannot "
            f"clear their chips from a slot in {mat.get('name', 'this material')} - "
            f"they pack, weld, and snap the tool. Use a 1- or 2-flute cutter to run "
            f"this material at full feed.")
    feed_raw = rpm * feed_flutes * chipload_target * rigidity_factor
    feed = min(feed_raw, m['xy_feed_max'])
    if feed_raw > m['xy_feed_max']:
        warnings.append(
            f"Feed clamped by machine limit: wanted {feed_raw:.1f} IPM, "
            f"machine max is {m['xy_feed_max']:.0f} IPM")

    chipload_achieved = feed / (rpm * flutes)

    # Achieved-chipload sanity checks (use the un-derated bounds; op_factor only
    # trims the target, the physical min/max are properties of the material/tool).
    if chipload_achieved < mat['chipload_min']:
        warnings.append(
            f"Achieved chipload {chipload_achieved:.4f} in/tooth is below the "
            f"recommended minimum {mat['chipload_min']:.4f} - risk of rubbing and heat. "
            + ("A tool with fewer flutes fixes this." if flutes_capped
               else "Consider fewer flutes or lower RPM."))
    elif chipload_achieved > mat['chipload_max']:
        warnings.append(
            f"Achieved chipload {chipload_achieved:.4f} in/tooth exceeds the "
            f"recommended maximum {mat['chipload_max']:.4f} - risk of tool deflection "
            f"or breakage. Consider more flutes or higher RPM.")

    # Advisory only where no hard cap fired - the cap's own warning already says it.
    if flutes > mat['max_flutes_soft'] and not flutes_capped:
        warnings.append(
            f"{flutes}-flute tool in {mat.get('name', 'this material')}: soft/gummy "
            f"materials evacuate chips poorly with high flute counts - the tool may "
            f"rub or pack. A 1- or 2-flute cutter is usually better.")

    # The ramp/helix always derives from the FULL-SLOT feed, whatever the operation:
    # an entry move is a full-width slot with axial engagement stacked on top, even
    # when the operation it enters (a pocket) then runs at partial engagement.
    # Multiplying the pocket's un-derated feed instead made a pocket's helical entry
    # run hotter than the same tool's perimeter ramp - backwards for the worst cut.
    slot_feed = feed if is_slot else feed * mat['slotting_multiplier']
    ramp_feed = slot_feed * mat['ramp_multiplier']
    # Clamped to the machine's Z limit like calculate_drill_feeds always has been.
    # This is emitted on Z-only moves (pecks, tab lifts), and a 2-flute in plywood
    # derived 66 ipm against the Omio's 60 ipm Z axis - the firmware clamps it, but
    # the program should never command what the machine cannot do.
    peck_raw = feed * mat['plunge_multiplier']
    peck_feed = min(peck_raw, m['z_feed_max'])
    if peck_raw > m['z_feed_max']:
        warnings.append(
            f"Plunge clamped by the machine's Z limit: wanted {peck_raw:.1f} IPM, "
            f"max is {m['z_feed_max']:.0f} IPM.")

    stepover = mat['stepover_ratio'] * diameter
    slot_stepdown = mat['slot_stepdown_ratio'] * diameter
    if mat['slot_stepdown_ratio'] > 3.0:
        warnings.append(
            f"Slotting stepdown of {slot_stepdown:.3f} in ({mat['slot_stepdown_ratio']:.1f}x "
            f"diameter) is aggressive - verify your tool's flute length and rigidity.")

    explanation = _build_explanation(
        m, mat, diameter, flutes, rpm, chipload_target, chipload_achieved,
        feed, ramp_feed, is_slot, op_factor, rigidity, rigidity_factor)

    formulas = [
        f"chipload_target = chipload_ref * (D / {d_ref:.3f})^{DIAMETER_EXPONENT}"
        + (" * slotting_multiplier" if is_slot else ""),
        "feed = RPM * min(flutes, feed_flutes_max) * chipload_target * rigidity_factor",
        "ramp_feed = slot_feed * ramp_multiplier",
        "peck_feed = min(feed * plunge_multiplier, machine z_feed_max)",
        "stepover = stepover_ratio * D",
        "slot_stepdown = slot_stepdown_ratio * D",
    ]

    return {
        'rpm': round(rpm),
        'feed_xy': round(feed, 1),
        'ramp_feed': round(ramp_feed, 1),
        'peck_feed': round(peck_feed, 1),
        'stepover': round(stepover, 4),
        'stepover_percentage': mat['stepover_ratio'],
        'slot_stepdown': round(slot_stepdown, 4),
        'chipload_target': round(chipload_target, 5),
        'chipload_achieved': round(chipload_achieved, 5),
        'feed_clamped': feed_raw > m['xy_feed_max'],
        'operation': operation,
        'warnings': warnings,
        'explanation': explanation,
        'formulas': formulas,
    }


def _build_explanation(m, mat, diameter, flutes, rpm, chipload_target,
                       chipload_achieved, feed, ramp_feed, is_slot, op_factor,
                       rigidity, rigidity_factor):
    dia_mm = diameter * 25.4
    parts = [
        f"For a {dia_mm:.1f}mm ({diameter:.3f}\") {flutes}-flute tool in "
        f"{mat.get('name', 'this material')} at {rpm:.0f} RPM, the target chipload is "
        f"{chipload_target:.4f} in/tooth."
    ]
    if is_slot and op_factor != 1.0:
        parts.append(
            f"Because this is a slotting/profile cut, the {op_factor:.2f} slotting "
            f"multiplier is applied to the reference chipload.")
    parts.append(
        f"Feed = RPM x flutes x chipload = {rpm:.0f} x {flutes} x "
        f"{chipload_target:.4f} = {rpm * flutes * chipload_target:.1f} IPM"
        + (f", scaled by the {rigidity} machine's {rigidity_factor:.2f} rigidity factor"
           if rigidity_factor != 1.0 else "")
        + f", giving {feed:.1f} IPM.")
    if abs(chipload_achieved - chipload_target) > 1e-6:
        parts.append(
            f"After machine limits the achieved chipload is "
            f"{chipload_achieved:.4f} in/tooth.")
    parts.append(
        f"Ramp feed is {mat['ramp_multiplier']:.2f} x the full-slot feed, "
        f"or {ramp_feed:.1f} IPM.")
    return " ".join(parts)


if __name__ == '__main__':
    import json
    demo = calculate_feeds('omio_x8', 'plywood', TOOL_PRESETS['4mm_1f'], 'profile')
    print(json.dumps(demo, indent=2))
