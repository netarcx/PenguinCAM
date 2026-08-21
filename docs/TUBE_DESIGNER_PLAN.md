# Plan: Tube Creator mode — free placement of holes, patterns, pockets and bearing bores

Status: **PLAN, not built.** Written for the implementing session. Read
`TUBE_PATTERNS.md` first — the designer builds on that machinery and every rule in it
still applies. Read `MULTI_TOOL_STATUS.md` for why nothing here may be trusted until it
is verified against generated output.

## What is being asked for

Today a tube face gets one of two fixed patterns (drilled hole grid, or truss
lightening) or a DXF drawn in CAD. The designer is the third path: **author the face in
the browser** — click to place individual features at chosen positions — and machine it
through the pipeline that already exists. Features requested:

- Holes at named sizes: **1/4" clearance (0.2656")**, **10-32 clearance (0.1935")**,
  **8-32 clearance (0.1695")** — plus custom diameter.
- **Hole patterns**: a linear run (start, pitch, count) and a rows-x-columns array.
- **Pockets** (rectangular lightening with corner radii; the truss triangle as a
  placeable unit is a nice-to-have, not required).
- **Bearing holes: 1.125"** (the standard FRC 0.5"-hex flanged bearing OD).

## The one architectural decision that makes this small

**A custom design is machined exactly like a DXF tube face.** Do not extend the drilled
(`mode='holes'`) path. A design mixing 0.1695", 0.2656" and 1.125" holes cannot be
drilled with one bit, and the tube program has no tool change — but the DXF tube path
already machines arbitrary mixed circles and pocket rings with a single end mill:
`classify_holes()` picks peck-plunge+spiral for small holes and helical entry for large
ones, `_generate_pocket_gcode` clears the pockets, and `generate_tube_pattern_gcode`
mirrors face 2. A 1.125" bearing bore is just a large hole to that code.

So the server-side job is only: **turn a feature list into `circles` + `pockets`**, then
hand off to `load_tube_pattern`-style loading. No new toolpath generation. Everything
else is validation and UI.

A "drill this instead" optimisation (when every hole in the design happens to match one
drill) is explicitly **out of scope** for v1 — note it in the doc, do not build it.

## Named sizes: one registry, already in the repo

`drill_sizes.TAP_DRILLS` already holds every requested clearance value
(`'1/4-20': clearance 0.2656`, `'10-32': 0.1935`, `'8-32': 0.1695`, plus 4-40 through
3/8-16 and M3–M6). **Do not create a second table.** Build the designer's size menu from
it, adding only:

```python
# tube_designer.py
BEARING_BORES = {'flanged-hex-bearing': 1.125}   # FRC 0.5in hex bearing OD
```

The UI menu = clearance sizes from TAP_DRILLS + bearing bores + "custom diameter".
Counts and labels must come from the server (see "single source of truth" below).

## Design document (the JSON the browser sends)

```json
{ "version": 1,
  "features": [
    {"type": "hole",    "x": 0.5, "y": 1.0, "size": "10-32"},
    {"type": "hole",    "x": 1.0, "y": 3.0, "diameter": 0.196},
    {"type": "bearing", "x": 1.0, "y": 6.0},
    {"type": "hole-run","x": 0.5, "y": 1.0, "pitch": 0.5, "count": 8, "axis": "y"},
    {"type": "hole-array","x":0.5,"y":1.0,"pitch_x":1.0,"pitch_y":0.5,"cols":2,"rows":4,"size":"10-32"},
    {"type": "pocket",  "x": 1.0, "y": 4.0, "w": 1.0, "h": 2.0, "corner_radius": 0.25}
  ] }
```

- `x`/`y` are the FEATURE CENTRE in the tube frame (X across the face, Y along the tube,
  inches — the same frame `tube_patterns` and the Layout canvas already use).
- `size` (named) and `diameter` are mutually exclusive; named wins and is resolved
  server-side so a stale client cannot ship wrong numbers.
- Caps: ≤ 200 features, ≤ 500 resolved holes (route-level, like the length cap).

## Server work

**New module `tube_designer.py`** (keep `tube_patterns.py` untouched — the fixed
patterns must not change behaviour):

```python
def resolve(design: dict, face_width: float, tube_length: float,
            tool_diameter: float, helix_radius_multiplier: float) -> dict
    # -> {'circles': [...], 'pockets': [ring, ...], 'warnings': [...], 'errors': [...]}
```

Expansion: `hole-run`/`hole-array` expand to individual circles. Pockets become closed
rings (rounded-rect via shapely `Polygon(...).buffer(r).exterior` or explicit arcs —
match `CHORD_TOLERANCE` fidelity).

Validation — errors (refuse) vs warnings (advise), following the existing philosophy of
refusing rather than silently mis-machining:

| check | why (all of these bit us already) |
|---|---|
| every value finite, via `not (x > 0)` style | NaN slid through `<= 0` guards and emitted `Xnan` |
| feature inside the face with `LIGHTENING_EDGE_MARGIN` to long edges, `MIN_END_MARGIN` to ends | a 0.201" hole was placed on a 0.15" face |
| ≥ `MIN_WEB` between every pair of feature edges (shapely `distance` on the resolved shapes, all pairs — designs are small) | overlapping pockets cut the same metal twice |
| hole ≥ `tool_diameter - hole_size_tolerance` | classify_holes refuses these anyway; refuse EARLY with the feature's index in the message |
| pocket inradius ≥ `tool_radius * (1 + helix_radius_multiplier) + POCKET_TOOL_CLEARANCE`, AND `buffer(-tool_radius).area > 0.001` | the helix entry spilled through the edge margin; the inradius test alone passed pockets the offset then dropped, yielding "success" that cut nothing |
| corner_radius ≥ tool_radius for pockets | an end mill cannot cut a sharper inside corner |
| face_width x tube_length inside the machine envelope | the 24" tube that "worked" |
| unknown `type`/`size` → error naming it | `_parse_tube_size` answering "1x1" for garbage is the cautionary tale |

**Post-processor**: add `FRCPostProcessor.load_tube_design(design, face_width,
tube_length)` mirroring `load_tube_pattern` — same attribute resets (`circles`, `lines`,
`arcs`, `polylines`, `splines`, `layer_data`, `perimeter`, `pockets`, **`errors = []`**),
then `classify_holes()` + `_sort_pockets()`, `tube_pattern_mode = 'custom'`. Inch-only
guard identical to the pattern one. `'custom'` mode uses an end mill, so `square_end` /
`cut_to_length` remain **allowed** (the drill refusal keys on `mode == 'holes'` — verify
that stays true, and add a test).

**Routes**:
- `/process`: accept `tube_pattern=custom` + a `tube_design` form field (JSON string;
  parse with try/except → 400, never 500). Reuse the existing envelope check, warnings
  plumbing, `tube_preview` payload (it already carries arbitrary holes+pockets — the 3D
  viewer needs zero changes), and the honest-parameters block.
- `/api/tube-pattern`: add a POST form accepting the design and returning resolved
  geometry + warnings + errors, WITHOUT generating G-code. This is the single source of
  truth for the editor: **the browser never computes counts or legality itself** — the
  fixed-pattern note drifted the one time it duplicated constants, and the audit called
  it out. Debounce ~300 ms client-side.

## Browser work (`static/wizard.js` + a new `static/tube_designer.js`)

Add `custom` to the `#f-tube-pattern` select ("Custom — place holes and pockets
yourself"). When selected, show an editor panel in the Parts quadrant (which is idle for
generated patterns — its note already says "nothing to add").

Editor = the existing Layout-canvas machinery, extended:
- Draw the tube outline + features with `drawTubePattern` (it already draws arbitrary
  holes/pockets from the API payload).
- Palette: hole (size dropdown from the API's size list), hole run, array, pocket,
  bearing. Click places at the cursor, **snapped to a 0.125" grid** (half-web); arrow
  keys nudge by one snap; Delete removes; drag moves. Selection = nearest feature within
  hit radius, reusing the canvas hit-test patterns from the 2D layout.
- A properties strip for the selected feature (position, size/pitch/count/w/h).
- Server-rejected features drawn in the danger colour with the server's message shown in
  the existing errors region — do not invent a client-side validity model.
- State lives in `state.tubeDesign` (the JSON above). Every mutation →
  `invalidatePreview()` + debounced POST to `/api/tube-pattern` → redraw + update note.

Hard-won UI rules that MUST be followed for every new control:
- `autocomplete="off"` on every new input/select (Firefox soft-reload restore made the
  wizard incoherent; `adoptControlsIntoState` only covers the controls it knows).
- New inputs that affect the program must call `invalidatePreview()`.
- First paint must come from `syncUIFromState()` — no control may depend on an event
  having fired (the blank-Layout-panel bug).
- Face 2 note: v1 mirrors face 1 (the pipeline mirrors automatically). Say so in the
  editor ("mirrored onto the opposite wall"). Distinct per-face designs are v2.

## Verification (gate for calling it done)

1. `tests/test_tube_designer.py`: expansion counts; named-size resolution against
   `drill_sizes.TAP_DRILLS`; every validation row above has a refusing test AND a
   passing near-miss; route tests incl. no-file, bad JSON → 400, caps, envelope;
   `tube_preview` carries the resolved geometry; mixed-size design generates with
   helical entries for the bearing bore and NO drill claims in the header.
2. `gcode_audit.py`: add 2–3 `audit_tube`-style custom designs (mixed holes + pocket +
   bearing, both faces). The audit's text/structure + first-move-retract checks run;
   note again that depth truth is NOT covered.
3. Browser (Playwright, Chromium AND Firefox): place features, generate, counts match
   the program; soft-F5 consistency; `pageerror` + console + **unhandledrejection**
   captured (the maxX ReferenceError hid in a promise chain).
4. `make test` green; deploy is the standard rsync + compose rebuild (root-anchored
   excludes — rsync patterns are not .dockerignore patterns).

## Staging (each stage ends green and committable)

1. `tube_designer.py` + resolver tests — pure geometry, no routes.
2. `load_tube_design` + `/process` + `/api/tube-pattern` POST + route tests + audit cases.
3. Editor UI + browser tests; docs (`TUBE_PATTERNS.md` cross-link, `CLAUDE.md` table row,
   ROADMAP note).
4. Deploy to UP2, verify live in Firefox, then reconcile `MULTI_TOOL_STATUS.md`
   ("nothing cut on real stock" applies in full).

## Explicitly out of scope for v1

Distinct face-2 designs; drill-mode optimisation; slots/ovals; tapped-hole purposes
(spot/tap); importing a fixed pattern into the editor as a starting point (v2 candidate:
"start from Mounting holes / Lightening and edit").
