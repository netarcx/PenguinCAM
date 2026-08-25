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

**Do not** implement this by rewriting finished G-code as text. The previous attempt
(`safe_test_mode.py`) did, and it put emoji into comments this project's rules forbid.

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

**Offcuts** are the half that pays: "save offcut" measures what is left and writes it
back as a remnant. An offcut nobody wrote down is an offcut nobody uses.

---

## Auto-arrange and fill sheet

Shelf packing, tallest first, one kerf between neighbours. Deliberately the simple
algorithm: not optimal, but predictable, never overlapping, and a person can see why it
did what it did — which matters more than the last few percent when someone is standing
at a machine deciding whether to trust it. Everything it places stays draggable.

**Fill sheet** answers "how many of these fit?" by placing them, and stops at the same
part limit the server enforces (`MAX_PARTS_PER_JOB`) so it cannot build a job the next
step refuses.

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

It refuses out loud — a tool too fat to write with, or a part too small for a legible
name, is skipped with a warning that reaches the operator, who ticked the box and is
expecting a label. Long names shrink to fit rather than running off into the neighbour.

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

**Watch out for:** the job name comes off the wire and becomes a directory name.
`_slug` strips anything that could climb out, and `_job_path` verifies the result is
inside the jobs directory rather than trusting it. There is a test for both.

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
