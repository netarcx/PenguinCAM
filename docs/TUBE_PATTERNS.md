# Pre-designed Tube Patterns

Read this before changing `tube_patterns.py`, `FRCPostProcessor.load_tube_pattern`, the
tube branch of `/process`, or the tube model in `static/gcode_viewer.js`.

A tube job normally takes its pattern from a DXF. That is right when the pattern is
specific to the part, and needless work when it is the pattern every team puts in every
piece of 1x1 and 2x1. `tube_patterns.py` generates it directly, so a tube can be machined
**with no CAD step at all**.

---

## Two modes, never both

| `mode='holes'` | `mode='lightening'` |
|---|---|
| **Drilled** #10 clearance (0.201") on 0.500" centres | **Milled** right-triangle truss pockets |
| 2" face: **3 holes per column** (rows at 0.5", 1.0", 1.5") | 2" face: 1.5" x 1.875" triangles |
| 1" face: **1 hole per column**, centred | 1" face: 0.5" x 1.875" triangles |
| No pockets | No holes |
| Tool: 0.201" twist drill | Tool: your end mill |

**They are mutually exclusive on purpose.** A face drilled on half-inch centres has no room
left to lighten — three rows across 2" leaves 0.049" between them once the web is kept
clear — and a face cut away by a truss has nothing solid left to bolt through. Mixing them
produces a pattern that does neither job well, and would need a tool change mid-face
because holes want a drill and pockets want an end mill.

Both are centred on the tube length, so the two ends keep equal material and a tube stays
symmetric if it is later cut down.

### Why triangles

The right angle alternates between the two sides of the face from cell to cell, so the
material left between pockets runs **diagonally** — a zigzag web carrying load in tension
and compression, rather than a straight rail carrying it in bending. It is the same reason
a truss is not a plank.

Because a lightening pattern carries no holes, the triangles get the whole face (inside
`LIGHTENING_EDGE_MARGIN` of each long edge) rather than a narrow band between hole rows.
That is what makes them worth cutting: 1.406 sq in each on a 2" face, and it is why a 1"
face can now be lightened at all.

---

## Holes are DRILLED, not milled

This is not a separate toolpath. It falls out of **sizing the cutter to the hole**:
`classify_holes` marks a hole at tool size as `needs_peck_drill`, and
`_generate_peck_drill_and_spiral_gcode` then emits straight pecks with *no lateral
clearing* — the only motion a twist drill can make.

So a holes pattern **requires** the tool to be a 0.201" twist drill, and
`load_tube_pattern` raises rather than quietly milling with whatever is loaded. An end
mill narrower than the hole would cut each of 141 holes out sideways, which is exactly the
class of bug this project has shipped before (see `MULTI_TOOL_STATUS.md`). The CLI and the
route both substitute the drill automatically and say so; the program header reads
`( Tool: 0.201" twist drill )`.

Verified on generated output: 0 helical entries, 0 spiral clearing, and **0 lateral moves
while the drill is below the tube surface**.

---

## What it refuses

An audit of the generated programs found several that were well-formed, passed every
test, and were physically wrong. These are now refused outright rather than emitted:

| Refused | Because |
|---|---|
| A drilled pattern with `--square-end` or `--cut-to-length` | Those are milling operations. The program has a twist drill loaded and no tool change, so it fed the drill sideways through the wall 316 times. Run the facing as a separate tube-facing job. |
| A tube longer than the machine's travel | A 24" tube exceeds the 19.7" Y travel of the machine this was written for. It used to return a clean success, a 3D preview and a downloadable program the machine cannot run. |
| Any job in millimetres | Every constant here is inches and the tube program hard-codes `G20`, so a metric run emitted inch-mode G-code holding millimetre numbers. |
| A tool that is not the drill, in `holes` mode | Checked against `min_millable_hole`, not just `tool_diameter` - a config with `min_millable_multiplier: 1.0` passed the naive check and milled every hole. |
| A tube size that is not recognised | `_parse_tube_size` answers "1x1" for anything unknown, so a typo silently became a 1x1 job on a 2" face. |
| A non-positive or non-finite tool, wall, height or length | A negative tool made the pocket-clearing loop step outward forever and hung the request; a negative height put the whole program, including its "safe" retract, below the work zero. |

Drilled holes also now go **deeper than the wall by the length of the drill point**. A
twist drill cuts a cone: stopping the tip at the wall bottom left a 0.201" hole exiting
at 0.027", which no #10 screw passes - the one thing the pattern exists to allow.

## The rules that keep it safe

Each drops geometry rather than emitting something the machine will mishandle:

- **A pocket the tool cannot clear is dropped.** The real constraint is the triangle's
  *inscribed circle*, not its area or bounding box: a cutter wider than that cannot reach
  the corners, and the path degenerates when offset inward.
- **`MIN_WEB` (0.125") is preserved** between adjacent pockets, and
  `LIGHTENING_EDGE_MARGIN` (0.25") along each long edge — the corner radius of the
  extrusion lives there and is the stiffest part of the section.
- **A tube too short to hole safely gets no holes**, instead of one placed where it tears out.
- **Pockets are checked for overlap before being emitted.** The arithmetic already
  separates them — each triangle's base is horizontal, so consecutive cells are a full
  `MIN_WEB` apart at every X — but the check runs anyway, because two overlapping pockets
  would be cut twice and the second pass would climb into air. Verified across 784 tube
  configurations: zero overlaps, minimum gap exactly 0.125".
- **The program retracts to safe Z before its first lateral move, and after every
  pause.** The operator has just had their hands in the envelope to flip the tube, and
  jogging Z is the normal thing to do while there.

All of these are warnings, not errors: the program is still machinable without whatever was
dropped, and the operator is told what is missing.

---

## The CAD preview

The viewer draws the **tube itself**, not a translucent box around the toolpath. The
machined wall is a `THREE.Shape` with every hole and pocket added as a `Path` hole, then
extruded to the wall thickness — a genuine cut-out you can see through, not a decal. The
other three walls are plain boxes, enough to read as a section.

`/process` returns `tube_preview` (face frame, inches) for this. The viewer cannot infer
the shapes back out of G-code: nothing in a toolpath distinguishes a hole from a circular
pocket.

Shape coordinates are the pattern's own frame (u across the face, v along the tube).
`rotateX(-90°)` maps local `(u, v, w)` to `(u, w, -v)`, which is exactly the gcode-to-scene
mapping the rest of the viewer uses, so the model lands on the toolpath without a second
convention to keep straight. Tubes also get their own camera fit and a centred grid; the
plate framing left a 2x24 tube as a sliver in the corner.

---

## Using it

```bash
# 2x1, 24" long, drilled mounting holes, both faces
uv run python frc_cam_postprocessor.py --mode tube-pattern out.nc \
  --tube-pattern holes --tube-size 2x1-flat --tube-length 24 \
  --material aluminum_tube --thickness 0.0625 --tool-diameter 0.157

# same tube, truss lightening instead
  ... --tube-pattern lightening
```

`--tube-length` is **required**: it decides how many holes or triangles fit, and guessing
it would hand the operator a program for the wrong tube. `--tube-size` sets both face width
and tube height; `--tube-width` / `--tube-height` override for a non-standard extrusion.

In the wizard: Setup → mode **Tubing** → Pattern. The note under the length field gives the
count before you generate anything.

---

## Verification, and what is NOT verified

`tests/test_tube_patterns.py` covers the geometry (spacing, centring, end margins, mode
exclusivity, overlap — including that the overlap guard itself fires), the drilled-hole
path (every hole marked for a peck; a milling cutter refused), the `/process` route
including the no-file case, the preview payload, and the G-code formatting rules.

`gcode_audit.py` audits tube programs via `audit_tube()`. **Only the frame-independent
checks run there** — comment rules, ATC/canned cycles, program end, spindle restart after
each flip, and the first-move retract. The ZMIN and rapid-below-top checks assume the plate
Z-frame; a tube works at `Z = tube_height - wall` and pauses to flip rather than to change
tools. **Physical-depth auditing of tube programs is not covered.**

> **Nothing generated by this module has been cut on real stock.** Well-formed,
> fully-passing, rule-compliant programs have been wrong here before. Dry-run above the
> work first — especially anything drilled.
