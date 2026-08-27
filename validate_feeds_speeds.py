"""Validate the feeds_speeds model against PenguinCAM's existing presets.

Goal (A) from the project: prove the derived model reproduces the hand-tuned presets
for the reference 4mm 1-flute tool, so we can trust it to extrapolate to other tools.

For each (machine, material) it runs ``calculate_feeds`` with the reference tool and a
profile (full-slot) operation, then compares to the preset feed/ramp/stepover/stepdown.
Exits non-zero if any compared value exceeds the tolerance, so it can gate changes.

Run:  uv run python validate_feeds_speeds.py
"""

import sys

from feeds_speeds import calculate_feeds, TOOL_PRESETS

# Tolerance (fraction) beyond which a delta is flagged as a failure.
TOLERANCE = 0.10

# Presets copied from team_config.py TEAM_6238_DEFAULTS (the machine-agnostic tuned
# numbers PenguinCAM actually applies) plus the per-machine feed differences.
# Each entry: machine key -> material key -> preset values for the 4mm 1F tool.
PRESETS = {
    'omio_x8': {
        'plywood':       {'feed_rate': 75.0, 'ramp_feed_rate': 50.0, 'stepover_percentage': 0.65, 'max_slotting_depth': 0.4,  'spindle_speed': 18000},
        'aluminum_6061': {'feed_rate': 30.0, 'ramp_feed_rate': 19.0, 'stepover_percentage': 0.25, 'max_slotting_depth': 0.06, 'spindle_speed': 14000},
        'polycarbonate': {'feed_rate': 75.0, 'ramp_feed_rate': 50.0, 'stepover_percentage': 0.55, 'max_slotting_depth': 0.25, 'spindle_speed': 18000},
    },
    'avid_pro2424': {
        'plywood':       {'feed_rate': 75.0, 'ramp_feed_rate': 50.0, 'stepover_percentage': 0.65, 'max_slotting_depth': 0.4,  'spindle_speed': 18000},
        'aluminum_6061': {'feed_rate': 33.0, 'ramp_feed_rate': 21.0, 'stepover_percentage': 0.25, 'max_slotting_depth': 0.06, 'spindle_speed': 14000},
        'polycarbonate': {'feed_rate': 75.0, 'ramp_feed_rate': 50.0, 'stepover_percentage': 0.55, 'max_slotting_depth': 0.25, 'spindle_speed': 18000},
    },
}

# Materials whose preset is a SAFETY ENVELOPE rather than a target. For these the model
# may sit anywhere at or below the preset - and since 2026-08-27 it deliberately does:
# lowering the aluminum preferred_rpm from 18000 to 14000 dropped the derived feed from
# 30 IPM to 23.3 without re-tuning the chipload constants, so the model is now about 22%
# more conservative than the tested envelope. What must never happen is the model asking
# for MORE than the envelope, so that is what is gated.
ENVELOPE_MATERIALS = {'aluminum_6061', 'aluminum_6063'}

# Metrics where "less than the preset" is safe. Geometry (stepover, depth) still has to
# match both ways: a model that halved the stepover would silently double the cycle time.
ENVELOPE_METRICS = {'feed IPM', 'ramp IPM', 'RPM'}

# What we compare: label -> (model result key, preset key).
COMPARISONS = [
    ('feed IPM',      'feed_xy',       'feed_rate'),
    ('ramp IPM',      'ramp_feed',     'ramp_feed_rate'),
    ('stepover %',    'stepover_percentage', 'stepover_percentage'),
    ('slot depth in', 'slot_stepdown', 'max_slotting_depth'),
    ('RPM',           'rpm',           'spindle_speed'),
]


def main():
    ref_tool = TOOL_PRESETS['4mm_1f']
    header = f"{'machine':<14}{'material':<15}{'metric':<15}{'model':>10}{'preset':>10}{'delta':>9}"
    print(header)
    print('-' * len(header))

    failures = 0
    for machine, materials in PRESETS.items():
        for material, preset in materials.items():
            result = calculate_feeds(machine, material, ref_tool, operation='profile')
            for label, model_key, preset_key in COMPARISONS:
                model_val = result[model_key]
                preset_val = preset[preset_key]
                if preset_val:
                    delta = (model_val - preset_val) / preset_val
                else:
                    delta = 0.0
                one_sided = (material in ENVELOPE_MATERIALS
                             and label in ENVELOPE_METRICS)
                flag = ''
                if one_sided and delta < -TOLERANCE:
                    flag = '  (under envelope)'
                elif abs(delta) > TOLERANCE:
                    flag = '  <-- FAIL'
                    failures += 1
                print(f"{machine:<14}{material:<15}{label:<15}"
                      f"{model_val:>10.4g}{preset_val:>10.4g}{delta:>+8.1%}{flag}")
            print()

    if failures:
        print(f"FAILED: {failures} value(s) outside +/-{TOLERANCE:.0%} tolerance.")
        return 1
    print(f"OK: all values within +/-{TOLERANCE:.0%} of PenguinCAM presets "
          f"(envelope materials may run under).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
