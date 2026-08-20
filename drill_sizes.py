"""Standard twist-drill sizes, and picking one for a hole from a drawing.

A twist drill makes exactly one size of hole - its own - so "which drill for this hole"
is a lookup against the sizes that actually exist in a tool crib, not arithmetic. CAD
almost always draws a hole at a real drill size already (0.1935 is a #10, 0.196 is a #9),
so the answer is usually exact; when it isn't, the choice of which way to round matters.

**Round up, not to nearest.** A hole drawn 0.1935 that gets a 5/32 (0.1562) drill is
0.037 undersized, and the #10 bolt it was drawn for will not pass through it. Going
slightly over costs a few thou of slop; going under costs the part. So a drill is only
offered under-size inside a tight tolerance, and the preference order is: exact, then the
smallest drill at or above the hole, then - only if nothing else fits - the nearest under.

This module is deliberately free of any PenguinCAM dependency: it is a table and three
functions, so it can be tested and reused on its own.
"""

from fractions import Fraction
from typing import Dict, List, Optional, Sequence

#: Number drills, #1 (largest) to #60. The FRC-relevant ones are #7/#9/#10 (the clearance
#: sizes for 10-32 and 10-24 hardware) and #19/#21/#25 around 8-32.
NUMBER_DRILLS = {
    1: 0.2280, 2: 0.2210, 3: 0.2130, 4: 0.2090, 5: 0.2055, 6: 0.2040, 7: 0.2010,
    8: 0.1990, 9: 0.1960, 10: 0.1935, 11: 0.1910, 12: 0.1890, 13: 0.1850, 14: 0.1820,
    15: 0.1800, 16: 0.1770, 17: 0.1730, 18: 0.1695, 19: 0.1660, 20: 0.1610, 21: 0.1590,
    22: 0.1570, 23: 0.1540, 24: 0.1520, 25: 0.1495, 26: 0.1470, 27: 0.1440, 28: 0.1405,
    29: 0.1360, 30: 0.1285, 31: 0.1200, 32: 0.1160, 33: 0.1130, 34: 0.1110, 35: 0.1100,
    36: 0.1065, 37: 0.1040, 38: 0.1015, 39: 0.0995, 40: 0.0980, 41: 0.0960, 42: 0.0935,
    43: 0.0890, 44: 0.0860, 45: 0.0820, 46: 0.0810, 47: 0.0785, 48: 0.0760, 49: 0.0730,
    50: 0.0700, 51: 0.0670, 52: 0.0635, 53: 0.0595, 54: 0.0550, 55: 0.0520, 56: 0.0465,
    57: 0.0430, 58: 0.0420, 59: 0.0410, 60: 0.0400,
}

#: Letter drills, A (smallest) to Z.
LETTER_DRILLS = {
    'A': 0.2340, 'B': 0.2380, 'C': 0.2420, 'D': 0.2460, 'E': 0.2500, 'F': 0.2570,
    'G': 0.2610, 'H': 0.2660, 'I': 0.2720, 'J': 0.2770, 'K': 0.2810, 'L': 0.2900,
    'M': 0.2950, 'N': 0.3020, 'O': 0.3160, 'P': 0.3230, 'Q': 0.3320, 'R': 0.3390,
    'S': 0.3480, 'T': 0.3580, 'U': 0.3680, 'V': 0.3770, 'W': 0.3860, 'X': 0.3970,
    'Y': 0.4040, 'Z': 0.4130,
}

#: Tap drills for the threads an FRC team actually cuts. `nominal` is what CAD usually
#: draws (the thread's major diameter); `tap` is the drill that leaves the right amount of
#: material for the tap - always UNDERSIZE, by roughly the thread depth; `clearance` is
#: the drill for a hole a fastener of that size passes through.
#:
#: This is why a drill cannot be chosen from the drawn diameter alone: 0.1900 drawn as a
#: 10-32 clearance hole wants a #10 (0.1935, over), and the same 0.1900 drawn as a hole to
#: be tapped 10-32 wants a #21 (0.1590, well under). Same number, opposite directions.
TAP_DRILLS = {
    '4-40':    {'nominal': 0.1120, 'tap': 0.0890, 'clearance': 0.1160},   # #43 / #32
    '6-32':    {'nominal': 0.1380, 'tap': 0.1065, 'clearance': 0.1440},   # #36 / #27
    '8-32':    {'nominal': 0.1640, 'tap': 0.1360, 'clearance': 0.1695},   # #29 / #18
    '10-24':   {'nominal': 0.1900, 'tap': 0.1495, 'clearance': 0.1935},   # #25 / #10
    '10-32':   {'nominal': 0.1900, 'tap': 0.1590, 'clearance': 0.1935},   # #21 / #10
    '1/4-20':  {'nominal': 0.2500, 'tap': 0.2010, 'clearance': 0.2656},   # #7  / 17/64
    '1/4-28':  {'nominal': 0.2500, 'tap': 0.2130, 'clearance': 0.2656},   # #3  / 17/64
    '5/16-18': {'nominal': 0.3125, 'tap': 0.2570, 'clearance': 0.3281},   # F   / 21/64
    '3/8-16':  {'nominal': 0.3750, 'tap': 0.3125, 'clearance': 0.4062},   # 5/16/ 13/32
    'M3':      {'nominal': 0.1181, 'tap': 0.0984, 'clearance': 0.1280},   # 2.5 / 3.25 mm
    'M4':      {'nominal': 0.1575, 'tap': 0.1299, 'clearance': 0.1719},   # 3.3 / 4.36 mm
    'M5':      {'nominal': 0.1969, 'tap': 0.1654, 'clearance': 0.2165},   # 4.2 / 5.5 mm
    'M6':      {'nominal': 0.2362, 'tap': 0.1969, 'clearance': 0.2610},   # 5.0 / 6.63 mm
}

#: What a drilling operation is for. The purpose decides which drill suits a hole, and
#: they disagree by more than any tolerance - see TAP_DRILLS.
PURPOSE_CLEARANCE = 'clearance'   # the fastener passes through: match, never undersize
PURPOSE_TAP = 'tap'               # the hole gets threaded: deliberately undersize
PURPOSE_SPOT = 'spot'             # centre/spot drill: a locating dimple, size irrelevant
PURPOSES = (PURPOSE_CLEARANCE, PURPOSE_TAP, PURPOSE_SPOT)

#: How close a drill has to be before it counts as the size a hole was drawn at. Under a
#: 64th, which is the granularity a fractional index actually offers.
DEFAULT_TOLERANCE = 0.010

#: How close a drawn diameter must be to a thread's major diameter to be read as that
#: thread. Tight on purpose: at 0.004 a 0.1935 hole (the #10 CLEARANCE size) came within
#: range of the 0.190 nominal, so a clearance hole could be mistaken for a tapped one.
NOMINAL_TOLERANCE = 0.002

#: A drill smaller than the drawn hole is only ever acceptable by a whisker - the hole is
#: usually a clearance hole and undersizing it means the fastener does not fit.
UNDERSIZE_TOLERANCE = 0.002


def _fractional_drills() -> Dict[str, float]:
    """1/64 up to 1/2 in 64ths, then 1/2 to 1 in 32nds - a standard index."""
    sizes = {}
    for numerator in range(1, 33):                 # 1/64 .. 32/64 (= 1/2)
        frac = Fraction(numerator, 64)
        sizes[f'{frac.numerator}/{frac.denominator}'] = float(frac)
    for numerator in range(17, 33):                # 17/32 .. 32/32 (= 1)
        frac = Fraction(numerator, 32)
        sizes[f'{frac.numerator}/{frac.denominator}'] = float(frac)
    return sizes


FRACTIONAL_DRILLS = _fractional_drills()


class DrillSize:
    """One drill from the index."""

    __slots__ = ('diameter', 'designation', 'series')

    def __init__(self, diameter: float, designation: str, series: str):
        self.diameter = diameter
        self.designation = designation
        self.series = series          # 'fractional' | 'number' | 'letter'

    @property
    def label(self) -> str:
        if self.series == 'fractional':
            return f'{self.designation} in'
        if self.series == 'number':
            return f'#{self.designation}'
        return f'{self.designation} letter'

    def describe(self) -> str:
        return f'{self.label} ({self.diameter:.4f} in)'

    def to_dict(self) -> Dict[str, object]:
        return {'diameter': round(self.diameter, 4), 'designation': self.designation,
                'series': self.series, 'label': self.label}

    def __repr__(self):                                     # pragma: no cover - debugging
        return f'<DrillSize {self.describe()}>'


def _build_index() -> List[DrillSize]:
    sizes = [DrillSize(d, name, 'fractional') for name, d in FRACTIONAL_DRILLS.items()]
    sizes += [DrillSize(d, str(n), 'number') for n, d in NUMBER_DRILLS.items()]
    sizes += [DrillSize(d, letter, 'letter') for letter, d in LETTER_DRILLS.items()]
    return sorted(sizes, key=lambda s: s.diameter)


DRILL_INDEX = _build_index()


def nearest_drill(diameter: float, tolerance: float = DEFAULT_TOLERANCE,
                  series: Sequence[str] = None) -> Optional[DrillSize]:
    """The drill to use for a hole drawn at `diameter`, or None if nothing is close.

    Prefers, in order: an exact match, the smallest drill at or above the hole, and only
    then a drill just under it (within UNDERSIZE_TOLERANCE). See the module docstring for
    why under-sizing is the last resort rather than the nearest-value answer.
    """
    if diameter <= 0:
        return None
    candidates = [s for s in DRILL_INDEX if not series or s.series in series]

    exact = [s for s in candidates if abs(s.diameter - diameter) < 1e-6]
    if exact:
        return _preferred(exact)

    over = [s for s in candidates if 0 < s.diameter - diameter <= tolerance]
    if over:
        best = min(s.diameter for s in over)
        return _preferred([s for s in over if abs(s.diameter - best) < 1e-9])

    under = [s for s in candidates if 0 < diameter - s.diameter <= UNDERSIZE_TOLERANCE]
    if under:
        best = max(s.diameter for s in under)
        return _preferred([s for s in under if abs(s.diameter - best) < 1e-9])

    return None


def _preferred(matches: List[DrillSize]) -> DrillSize:
    """Several designations share a diameter (1/4 in, E letter and 0.250 all coincide).
    Prefer the one a team is most likely to own and recognise."""
    order = {'fractional': 0, 'number': 1, 'letter': 2}
    return sorted(matches, key=lambda s: order.get(s.series, 3))[0]


def thread_for_nominal(diameter: float, tolerance: float = NOMINAL_TOLERANCE) -> Optional[str]:
    """The thread whose major diameter matches a drawn hole, or None.

    Several threads share a nominal (10-24 and 10-32 are both 0.190), so this returns the
    coarse one by convention; `tap_drill_for` reports the ambiguity rather than hiding it.
    """
    matches = [name for name, spec in TAP_DRILLS.items()
               if abs(spec['nominal'] - diameter) <= tolerance]
    return sorted(matches)[0] if matches else None


def tap_drill_for(diameter: float, tolerance: float = NOMINAL_TOLERANCE
                  ) -> Optional[Dict[str, object]]:
    """Tap-drill advice for a hole that is going to be threaded.

    Matches the drawn diameter against a thread's NOMINAL (major) diameter, and also
    against its CLEARANCE diameter - because plenty of CAD libraries draw every #10 hole
    at 0.1935 (the clearance size) whether the hole is meant to be tapped or not. Reading
    only the nominal meant a perfectly ordinary drawing could not be tapped at all.

    Returns {'threads': [...], 'tap_drills': [DrillSize], 'nominal': float} or None. All
    threads matching are listed because they can need DIFFERENT tap drills - 10-24 wants
    a #25 and 10-32 a #21 - and picking one silently would be a guess about hardware only
    the designer knows.
    """
    threads = sorted(
        name for name, spec in TAP_DRILLS.items()
        if abs(spec['nominal'] - diameter) <= tolerance
        or abs(spec['clearance'] - diameter) <= tolerance)
    if not threads:
        return None
    tap_sizes = sorted({TAP_DRILLS[t]['tap'] for t in threads})
    return {
        'threads': threads,
        'nominal': diameter,
        'tap_drills': [nearest_drill(size, tolerance=0.002) for size in tap_sizes],
        'ambiguous': len(tap_sizes) > 1,
    }


def drill_for_purpose(diameter: float, purpose: str = PURPOSE_CLEARANCE,
                      tolerance: float = DEFAULT_TOLERANCE) -> Optional[DrillSize]:
    """The drill for a hole, given what the hole is FOR.

    `spot` returns None on purpose: a centre drill only marks a location, so no size
    matching applies and any spotting tool will do.
    """
    if purpose == PURPOSE_SPOT:
        return None
    if purpose == PURPOSE_TAP:
        advice = tap_drill_for(diameter)
        if not advice or not advice['tap_drills']:
            return None
        return advice['tap_drills'][0]
    return nearest_drill(diameter, tolerance)


def suggest_drills(diameters: Sequence[float], tolerance: float = DEFAULT_TOLERANCE
                   ) -> Dict[str, object]:
    """Recommend a drill for every distinct hole size in a part.

    Returns {'matched': [{hole, drill, difference, count}], 'unmatched': [hole, ...]}.
    `unmatched` holes have no standard drill within tolerance and want an end mill - a
    1.125 in bearing bore, for instance, is bored, not drilled.
    """
    counts: Dict[float, int] = {}
    for raw in diameters:
        key = round(float(raw), 4)
        counts[key] = counts.get(key, 0) + 1

    matched, unmatched = [], []
    for hole in sorted(counts):
        drill = nearest_drill(hole, tolerance)
        if drill is None:
            unmatched.append(hole)
            continue
        entry = {
            'hole': hole,
            'count': counts[hole],
            'drill': drill.to_dict(),
            'difference': round(drill.diameter - hole, 4),
            'exact': abs(drill.diameter - hole) < 1e-6,
        }
        # If the hole is drawn at a thread's nominal diameter it may be meant for tapping,
        # in which case the drill above (a clearance size) is the wrong answer by ~0.03.
        # Only the designer knows which, so both are offered rather than one guessed at.
        tap = tap_drill_for(hole)
        if tap:
            entry['tap_option'] = {
                'threads': tap['threads'],
                'ambiguous': tap['ambiguous'],
                'drills': [d.to_dict() for d in tap['tap_drills'] if d],
            }
        matched.append(entry)
    return {'matched': matched, 'unmatched': unmatched}


def describe_suggestion(hole: float, tolerance: float = DEFAULT_TOLERANCE) -> str:
    """One-line advice for a hole size, for an error message."""
    drill = nearest_drill(hole, tolerance)
    if drill is None:
        return (f'no standard drill matches {hole:.4f} in - cut this one with an end '
                f'mill instead')
    if abs(drill.diameter - hole) < 1e-6:
        return f'{hole:.4f} in is a {drill.label} drill exactly'
    return (f'the closest standard drill to {hole:.4f} in is {drill.describe()}, '
            f'{abs(drill.diameter - hole):.4f} in '
            f'{"over" if drill.diameter > hole else "under"}')
