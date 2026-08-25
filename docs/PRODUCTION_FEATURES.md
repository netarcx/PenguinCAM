# Production Features

The six things that turn "generate a program" into "cut six of these before lunch".
Read this before changing `stroke_font.py`, `job_library.py`, the nesting or stock code
in `static/wizard.js`, or the `/stock/*` and `/jobs/*` routes.

---

## Dry run

**What:** the same program, raised clear of the work, with the spindle never started.

**Why:** `MULTI_TOOL_STATUS.md` says plainly that no multi-tool or drilled program has
ever been cut on real stock, and that two bugs there produced well-formed, fully-passing,
*wrong* programs. The habit that catches those is cutting air first.

**How it works:** the lift rides the Z frame (`set_dry_run` → `_apply_z_frame`), so it
moves the three Z anchors and every toolpath follows. The dry run and the real program
therefore differ by exactly the lift and nothing else — `tests/test_dry_run.py` asserts
that move for move. `M3` is never emitted, coolant stays off, the header carries a
banner, and the file is named `_DRYRUN`.

**The lift is a minimum, not a constant.** `_apply_z_frame` raises it to
`thickness + clearance + 0.25` whenever that is more, because a fixed 2" lift over a
2.5" block still fed a stationary cutter half an inch into it, under a banner promising
the program does not cut anything.

**Tubing dry-runs too.** The tube branch has its own Z anchors (`z_top`, `z_safe`,
`tube_safe_z`), and they take the lift as well — otherwise the chip, the setup sheet and
the UI all said "cuts air" while the program cut the tube.

**`gcode_audit.py` audits three dry-run programs** and asserts the physical claim
independently: parse the lift out of the banner, and nothing may reach the work below
it. Two things in the audit had to be fixed first — `simulate()` seeded `min_z` at a Z
the program never visits (harmless while every depth was negative, wrong for an entirely
positive frame), and the "every pause restarts the spindle" rule is inverted for a dry
run rather than skipped.

**Do not** implement this by rewriting finished G-code as text. The previous attempt
(`safe_test_mode.py`, deleted) did, and it put emoji into comments this project's rules
forbid.

---

## Stock library and offcuts

**Config:** a top-level `stock:` list, beside `tools:` — a sheet in the rack belongs to
the shop, not to one machine.

```yaml
stock:
  - name: "Half sheet ply"
    width: "48in"
    height: "48in"
    thickness: "0.25in"
    material: plywood
  - name: "Al offcut 11.5 x 6"
    width: "11.5in"
    height: "6in"
    remnant: true
```

**What changes when a sheet is chosen:** the sheet becomes the stock and the G54 origin
becomes the *sheet's* lower-left corner, not the parts' bounding box — so a part keeps
its place on the material between jobs. A part hanging off the sheet becomes an error;
before, it passed the machine check happily and ran off the material.

This holds on **both** generate paths. `/process-job` and `/process-multitool` each read
`job.stock` when it carries `from_library`, validate every part against it
(`validate_job_layout(..., stock=)`), and report it back as the stock so the 3D preview
and the summary chip agree with the setup sheet. The multi-tool path used to make its
placements relative to the parts' bounding box no matter what, so the whole nest cut
translated by however far it sat from the sheet's corner while the setup sheet told the
operator to zero on that corner.

The bounds check counts **half a kerf** on every side, because the profile pass rides
outside the outline: a part flush with the sheet edge still cuts past it.

**Offcuts** are the half that pays: "save offcut" measures what is left and writes it
back as a remnant. An offcut nobody wrote down is an offcut nobody uses. It refuses to
measure a layout the app has already flagged as invalid, and it clamps to the sheet —
unclamped, a part dragged off the left edge sent `maxX` negative and the prompt offered
an offcut *wider than the whole sheet*.

---

## Auto-arrange and fill sheet

Shelf packing, tallest first, one kerf between neighbours. Deliberately the simple
algorithm: not optimal, but predictable, never overlapping, and a person can see why it
did what it did — which matters more than the last few percent when someone is standing
at a machine deciding whether to trust it. Everything it places stays draggable.

**Fill sheet** answers "how many of these fit?" by placing them, and stops at the same
part limit the server enforces (`MAX_PARTS_PER_JOB`) so it cannot build a job the next
step refuses.

A part too wide for any shelf is counted **unplaced**, tested before the shelf-wrap.
Wrapping first sent a part that can never fit to a fresh shelf and then placed it there
anyway — hanging off the right edge and counted as placed, so "fill sheet" answered the
question with parts that do not fit, and the nest it built could not be generated at all.

Neither button assigns to `#layout-errors`; both **prepend**. `drawLayout()` has just
written the real validation messages there, and overwriting them showed a clean layout
that was not one until the operator tried to leave the step.

---

## Engraved part names

`stroke_font.py` is a **single-stroke** (centreline) font, not an outline one: an outline
font describes the outside of a letter, so engraving one means pocketing the space
between two curves — slow, and illegible at part-label sizes. Here the toolpath *is* the
letter.

Engraving runs **before the profile**, while the sheet is still whole: afterwards the
part hangs on tabs, and a light chattery label cut is exactly what breaks one. It lives
in `generate_part_phases` as well as `generate_gcode`, because multi-part jobs build
their phases directly and never call the latter.

**Placement is proved, not assumed.** `_engrave_available_area()` is the outline eroded
by a tool radius plus a hair, *minus every hole and pocket*; `_engrave_placement()` then
searches that area for somewhere the text's own measured extent is contained. The first
version took the bounding-box centre of the eroded outline with no containment test — on
any L, U or C part that is the notch, so the name was cut into whatever was nested
beside it, and on a part with a central bore it was cut away with the slug. The extent
is measured from the strokes, not assumed to be a 0..h em box, because a comma hangs
below the baseline and a `$` above the cap.

**The size floor is `2.1 x tool diameter`** (`ENGRAVE_MIN_HEIGHT_PER_TOOL`), from the
font's tightest feature — the E/F/H crossbar gap at 0.48 of cap height. The original
1.2x gate passed every common nesting cutter straight through to cut a solid blob. The
floor also means the **default 1/4" end mill could never engrave at 0.18"**, so a part
with room gets *taller* letters rather than a refusal, and sizing is arithmetic (the
height that spans the available width) rather than a fixed ladder that refuses names
that would have fitted.

It refuses out loud — a warning reaches the operator, who ticked the box and is expecting
a label — and it names the real obstacle: blaming the geometry when the cutter is what
will not fit sends someone hunting for room they already have.

Inputs are validated (a depth deeper than the stock used to plunge through the table),
lateral moves use the configured `retract_height` rather than a hard-coded one, and a
character with no glyph becomes a **visible dash plus a warning** rather than a mark that
reads as some other part number. The glyph-coverage test derives its expectation from
`sanitize_comment` instead of asserting a hardcoded list.

---

## Setup sheet

A printable page for the machine: stock, tools in the order the program asks for them,
the surface to zero on (its own highlighted row), the nest, and a pre-flight checklist.
Built client-side from state plus the Layout canvas, and opened in its own window.

The nest is redrawn in ink-on-white before capture (`printPalette`) and the screen
palette restored afterwards — a full-bleed dark rectangle wastes toner and is harder to
read under shop lighting.

---

## Saved jobs

`job_library.py`. A job is the whole setup plus every part's placement **and its DXF**,
stored in `penguincam-jobs/` beside the team config — same place, same lifetime, same
backups as the config.

**Not in the config file:** a job carries geometry, and a YAML config a person edits by
hand should not grow a base64 blob in the middle of it.

Opening a job pushes each DXF back through the ordinary upload path, so a restored part
goes through exactly the same code as a freshly dropped one — one path means a saved job
cannot drift into behaving differently from the job it was saved from.

**A part records both placement anchors.** `place_x`/`place_y` is the footprint's
lower-left corner, which is what every other wire format in this app means by "place";
`center_x`/`center_y` is what a part actually holds in the browser. Format 1 saved only
the corner and read it back as the centre, so every part moved by half its own footprint
— compounding on each save/open cycle, and silently: a nest with no sheet reopened,
generated and downloaded as a *different* nest with nothing on screen to say so. Format 2
saves both; a format 1 file is converted on open using the loaded part's footprint, with
rotation and mirror applied first so a turned part is measured as it sits.

**Watch out for:** the job name comes off the wire and becomes a directory name.
`_slug` strips anything that could climb out, and `_job_path` verifies the result is
inside the jobs directory rather than trusting it. There is a test for both. Placements
go through `_finite()` — NaN survives `float()` and `json.dump` writes it as bare `NaN`,
which `json.load` accepts, so a job saved with one was openable but unusable.

The routes have their own tests now (`JobRouteTest`), over HTTP, the way the browser
uses them: save → list → open → delete, plus the bad-base64 and missing-job cases. None
of that had a test before.

---

## Where each one lives

| Feature | Code |
|---|---|
| Dry run | `frc_cam_postprocessor.py` (`set_dry_run`, `_spindle_start_gcode`), `tests/test_dry_run.py` |
| Stock library | `team_config.py` (`saved_stock`), `local_mode.py` (managed blocks), `/stock/*`, `tests/test_stock.py` |
| Nesting | `static/wizard.js` (`autoArrange`, `fillSheet`, `updateUsage`) |
| Engraving | `stroke_font.py`, `frc_cam_postprocessor.py` (`_engrave_body`), `tests/test_engrave.py` |
| Setup sheet | `static/wizard.js` (`openSetupSheet`, `setupSheetHTML`) |
| Saved jobs | `job_library.py`, `/jobs/*`, `tests/test_job_library.py` |

Saving anything (bits, stock, jobs) needs a local install with a writable config
directory. The hosted app reads its config from Onshape, so those controls are disabled
there with the reason in the tooltip.
