# Multi-Tool Operations

Read this before changing `tooling.py`, the `/process-multitool` or `/part-features`
routes, `static/multitool.js`, or the chamfer code in `frc_cam_postprocessor.py`.

A normal UV-CAM job uses one cutter for the whole part. That is the right default —
it's fast, it needs no operator intervention, and it's what most FRC plates want. But
some parts genuinely need more than one:

- bolt holes too small for the cutter that profiles the part
- a big lightening pocket that a 1/4" cutter clears in a fraction of the time
- a chamfer or edge break, which needs a pointed tool
- a partial-depth pocket roughed with one cutter and finished with another

**Multi-tool mode** covers that case. You list the tools you'll load, then give each part
an ordered list of operations saying which tool cuts what. The output is one program with
a manual tool-change pause at every switch.

---

## Saved bits

The star beside each row in the Tools table writes that cutter into the team config file,
under a top-level `tools:` block:

```yaml
tools:
  - name: "1/4 in 2-flute endmill"
    diameter: "1/4\""
    flutes: 2
    type: endmill
  - name: "1/2 in 90 deg V-bit"
    diameter: "1/2\""
    flutes: 2
    type: vbit
    included_angle: 90
```

Saved bits then appear ahead of the built-ins everywhere the app offers a cutter: the
Add-tool menu here, and the *Saved bits* picker beside the tool diameter in Setup. A bit
is identified by its name, so saving one whose name is already on the list corrects that
entry instead of adding a second one; the star turns amber once a bit is written down,
and clicking it again removes it.

`diameter` accepts anything the UI accepts (`0.25`, `1/4"`, `6mm`) and is shown back the
way you wrote it. `type` is `endmill`, `vbit` or `drill`; `included_angle` only means
anything on a V-bit.

Two limits worth knowing:

* **Saving needs a config file the app can write**, which means local mode. On the hosted
  app the config is read from Onshape, so the star is disabled there and the block above
  is what you paste in by hand.
* **Everything except the `tools:` block is preserved character for character** when the
  app writes the file, and the result is re-parsed and compared against the original
  before it replaces anything - if a save would have changed any other setting, it is
  refused instead. A team config is a hand-written document and the app treats it as one.

## The model

Three objects, all in `tooling.py`:

| Object | What it is |
|---|---|
| `Tool` | One physical cutter: slot number, name, diameter, flute count, type (`endmill` / `vbit` / `drill`), and for a V-tool its included angle. Feeds are derived unless you pin them. |
| `Operation` | One thing to cut with one tool: an op type, the tool slot, an optional depth, and a `scope` narrowing which features it covers. |
| `MultiToolJob` | The tools, the parts, and everything shared: material, thickness, machine, tab spacing. |

### Operation types

| `op_type` | Cuts | Scope keys |
|---|---|---|
| `holes` | Circular features: helical bore or peck drill, then spiral clearing | `min_diameter`, `max_diameter`, `indices` |
| `pockets` | Closed non-circular regions, fully cleared or contoured | `min_area`, `max_area`, `indices` |
| `interior` | Both of the above in one operation | union of the two |
| `perimeter` | The outer profile, with tabs | *(none)* |
| `chamfer` | Breaks top edges with a V-tool | `targets` (`perimeter` / `holes` / `pockets`), `width`, plus the matching feature keys |

`depth` (inches below the material top) makes an operation partial-depth. Omit it and the
operation cuts through the stock into the sacrifice board, which is what a flat plate
normally wants. `perimeter` and `chamfer` **reject** a depth outright — a profile that
doesn't go through wouldn't free the part, and a chamfer's depth follows from its width
and the tool angle. (They used to accept and silently apply it, which produced a program
announcing "perimeter cut, tabs removed" for a part still fully attached to the stock.)

The tool must suit the operation, and this is enforced: a V-tool cannot cut holes,
pockets or a profile (no flat bottom), a drill cannot be fed sideways, and a chamfer
needs a V-tool. Nothing downstream would catch these — the post-processor only ever sees
a diameter, so it would happily emit a well-formed program that ruins the part.

---

## Why one post-processor per operation

`FRCPostProcessor` takes its tool diameter in the constructor, and nearly everything it
derives falls out of that single number: cutter compensation, the minimum millable hole,
helix entry radius, stepover, the contour-versus-clear area threshold, the corner-slowdown
zone. Threading a per-feature tool parameter through all of that would mean touching
thousands of lines of tested toolpath code.

So multi-tool mode doesn't do that. It builds **one post-processor per operation**, each
already correct for its own tool, narrows that instance's feature lists to the operation's
scope, and calls the ordinary generator. Every toolpath in a multi-tool program comes out
of exactly the same code as the single-tool program. `tooling.py` only adds the stitching:
ordering, tool-change blocks, and one shared header and footer.

The pipeline:

```
survey_part()         one throwaway post-processor per part, smallest tool loaded,
                      reporting the holes/pockets/perimeter available to scope against
generate_operation()  one post-processor per (part, operation): load, place, filter the
                      features to scope, emit the body
order_operations()    flatten (part, operation) pairs into one sequence grouped by tool
assemble_job()        header + bodies + a tool-change pause at each switch + footer
```

---

## Ordering

**Each part's operations run in the order you wrote them, always.** That order carries
intent no heuristic can recover — rough before finish, profile before chamfer — so the
job assembler never rearranges it.

What is free is the *interleaving between parts*. `order_operations` repeatedly picks the
tool the next ready operation needs and drains every part's leading run of operations
using it. Four parts sharing a three-tool plan therefore cost **two** tool changes, not
eleven.

Write the operations in the order you'd run them yourself and you'll always get a correct
program; group same-tool operations together and you'll also get a fast one. The UI warns
when a part's own list would force more than one change on its own.

**One exception.** The default fixturing pause (`pause_after_holes: true`) groups the job
into a hole phase and a remaining-work phase. Every part's fastening holes are completed,
then the spindle stops and the tool moves to the roomy manual-access height before a
single shared pause. The operator installs fasteners through those holes into the
sacrifice board, then pockets and profiles begin. If that boundary is also a tool change,
the fastening directions are folded into the tool-change stop instead of pausing twice.

The pause uses `machining.z_reference.tool_change_height` for vertical clearance and also
uses `machine.park_position` when that verified machine-coordinate park is configured.
Set `pause_after_holes: false` for a shop whose stock is already secured another way. The
older `pause_before_perimeter` option remains available as a separate, later boundary,
but is off by default.

### Tabs are held back

Tab removal frees the part. If anything at all is still to be cut afterwards — a chamfer
on the same part, or another part's profile on the same sheet — cutting the tabs inline
would leave a loose part on the table under a spinning cutter.

So a perimeter operation defers its tab-removal pass whenever the part has later
operations or the job has more than one part. All deferred removals run at the very end,
ordered so the tool already in the spindle goes first.

---

## Tool changes

The routers this targets have no automatic tool changer and no tool-length offset table,
so the output contains **no `T`/`M6` and no `G43`**. A change is a pause:

```
( === TOOL CHANGE - T2 1/4 in 1-flute endmill === )
G0 Z2.0000  ; Safe Z clearance
M5  ; Spindle off
G4 P5.0  ; 5 second dwell

( *** OPERATOR ACTION REQUIRED *** )
( Remove T1 1/8 in 1-flute endmill )
( Install T2 1/4 in 1-flute endmill, 0.2500 in diameter )
( Re-zero G54 Z to the sacrifice board surface with the new tool, not with G92 )
( Do NOT change the X or Y zero )
( Press CYCLE START to continue )
M0  ; Program pause

( === RESUME CHECKPOINT TC01 - T2 1/4 in 1-flute endmill === )
G90 G94 G91.1 G40 G49 G17  ; Reset positioning and cutting modes
G20  ; Inches
G92.1  ; Cancel any temporary coordinate offset
G54  ; Restore job work coordinate system
G0 Z0.7500  ; Safe Z before resumed XY motion
S18000 M3  ; Spindle on
G4 P3.0  ; 3 second spindle spin-up
```

This is the same `_generate_pause_and_park_gcode` sequence every other operator pause
uses, so a tool change parks and restarts exactly the way a fixturing pause does. The
spindle restarts at the **incoming** tool's speed, because the block is emitted from the
post-processor built for the operation that follows it.

When `machining.z_reference.tool_change_height` is configured, a tool change without a
verified G53 park retracts to that roomier height instead of stopping just above the
stock. UV-CAM also creates a standalone resume file at every `TCxx` checkpoint. The
default download is a ZIP containing the main program plus all of those recovery files;
the preview offers a separate main-program-only download when needed. A
resume file begins stopped, asks the operator to reference or home the machine if needed,
verify the unchanged G54 X/Y zero, load the named tool, and re-zero G54 Z. Only then does it
reset all modal state, retract, start the incoming tool, and run the remaining operations.
This avoids depending on Mach3's state reconstruction and preparatory move in **Run From
Here**.

Set the new tool's length with the controller's **G54 Z zero**, not a temporary G92
offset. Every normal and resume program deliberately issues `G92.1` before motion so a
stale local offset from an earlier run cannot shift the whole job.

**Re-zeroing Z is mandatory** and X/Y must not be touched: each tool sticks out of the
collet by a different amount, but the part hasn't moved.

The estimated cycle time excludes tool-change time — an `M0` waits for a person, and there
is no honest way to estimate that. The stats carry `excludes_tool_change_time` so the UI
can say so.

---

## Chamfering

A symmetric V-tool centred exactly **on the true, uncompensated edge** and dropped
`depth` below the material top breaks that edge by `width` horizontally. At height `h`
above the tip the cone radius is `h · tan(θ)` for half-angle `θ`, so as the tool follows
the edge its flank sweeps the plane running from `(edge, top)` down to
`(edge, top − depth)`:

```
depth = width / tan(included_angle / 2)
```

A 90° V-bit gives depth = width. A narrower tool must go deeper for the same break.

That makes a chamfer pass a plain **zero-compensation contour trace at a shallow depth** —
no offset math at all — and the same routine works on the perimeter, a hole, or a pocket.
Outside edges are cut clockwise and inside edges counter-clockwise, so the V-tool climb
mills either way.

These are refused rather than cut:

- a chamfer wider than the tool's radius (the cone can't reach that far sideways)
- a chamfer whose depth would go through the stock
- a chamfer wider than the hole it has to fit into
- a chamfer that doesn't fit the part or a pocket — see below
- a width that isn't a positive real number

**Does the break fit?** The V-tool reaches `width` sideways from the edge, so two edges
less than `2 × width` apart have their chamfers meet and the material between them
vanishes. `_chamfer_fits()` tests this by eroding the region by `width`: an *empty*
result means the shape is thin everywhere, and a result in *more pieces than it started
with* means the erosion ate through a neck — which is the narrow spot in question.

Testing only for empty is not enough, and this is worth remembering: a part with two
generous lobes joined by a thin waist erodes to two healthy islands, so a whole-part
"did it vanish" test passes happily while the waist is machined away.

### Compatibility deburr paths

The browser now routes every flat 2D job through an operation plan. The older `/process`,
`/process-job`, and CLI `--chamfer-width` / `--chamfer-bit-diameter` /
`--chamfer-bit-angle` / `--chamfer-targets` interfaces remain compatible for scripts and
saved integrations. They append one V-bit pass behind a manual tool change, using the
same fit and loose-part safety checks as the operations engine.

---

## Drilling

Everything else in the post-processor assumes an end mill: it enters helically, feeds
sideways, and steps over to open a bore. **A twist drill can do none of that.** It has no
side cutting edges worth the name, so the only motion it may make under load is straight
down its own axis.

Set a tool's `type` to `drill` and a `holes` operation on it takes a completely separate
path — `generate_drill_operation_gcode`, which emits a pecked `G83` on each hole centre
and nothing else. No helix, no stepover, no lateral feed move anywhere in the operation.
A drill is rejected outright for `pockets`, `interior` and `perimeter`.

### Depth

A through hole is driven **past the stock by the drill's point length**, because the tip
reaches depth before the full diameter does:

```
point_length = (D / 2) / tan(point_angle / 2)
```

For a standard 118° grind that's about 0.3 × D — on a 1/4" drill, 0.075", which is ten
times the 0.008" spoilboard overcut a *milled* hole gets away with. Without it the exit
side of the hole is a cone rather than an opening. Set `scope: {point_angle: 135}` for a
split-point drill, which reaches depth sooner and so drills measurably shallower.

### What is the hole FOR?

`scope: {purpose: ...}` on a drilling operation, because the same drawn diameter wants
opposite drills depending on the answer:

| purpose | meaning | 0.190" hole gets |
|---|---|---|
| `clearance` (default) | the fastener passes through | **#10** (0.1935", *over*) |
| `tap` | the hole is threaded afterwards | **#21** (0.1590", *well under*) |
| `spot` | a locating dimple for a later drill | whatever is in the spindle |

A **tap drill is deliberately undersize** — it leaves the material the tap cuts threads
into — so the round-up rule below is exactly wrong for it. `drill_sizes.TAP_DRILLS` holds
the threads an FRC team actually cuts. Where two threads share a nominal (10-24 and 10-32
are both 0.190" but want a #25 and a #21) that is **reported, never guessed**: only the
designer knows which hardware is going in.

**Spot drilling** ignores hole size entirely — a centre drill marks a location, it does
not make the hole — so any tool is accepted for any hole. It cuts a shallow dimple
(D/4 by default, `spot_depth` to override), never goes through the stock, and takes no
drill-point allowance. Every spot operation warns that *the holes still have to be
drilled*, because a program that spots and stops looks finished and is not.

Because a spot accepts any hole, the survey **lists** holes smaller than every tool in
the job (flagged `too_small`) instead of rejecting them: a 0.089" hole in a job whose
smallest cutter is a 1/8" end mill is still legitimately centre-marked for the drill
press. Whether such a hole is an error is decided by coverage validation, which knows the
plan — covered by a spot only, it is the usual "spotted but never drilled" warning;
covered by nothing, it is an error naming the two real fixes (a drill that size, or a
spot operation); claimed by a cutting operation, that operation fails with the ordinary
too-small message. `suggest_tooling` follows the same rule and proposes a spot operation
for sizes nothing in the plan can make, rather than sweeping them into the bore range.

### Pecking is written out, not a canned cycle

Drilling emits explicit `G0`/`G1` moves — cut a peck, retract to the R plane to clear
chips, rapid back down, repeat — rather than a `G83`. That costs about a dozen lines per
hole and buys four things at once:

- **GRBL doesn't implement canned cycles.** `G81`–`G89` are absent from GRBL 1.1, so a
  `G83` is `error:20 Unsupported command` and the program stops dead. `ASSUMPTIONS.md`
  lists GRBL as a target controller.
- **Cycle time counted it as zero.** `_estimate_cycle_time` only matches `G0`/`G1`/`G2`/
  `G3`/`G4`, so every `G83` scored nothing. A 12-hole aluminium plate reported 46 s
  against a real 1 m 10 s — the entire drilling operation was missing.
- **The 3D preview didn't draw it.** `gcode_viewer.js` matches `/^(G[0-3])/`, so drilled
  holes simply never appeared.
- **The heightmap simulator didn't see the material leave**, so harness comparisons were
  wrong for any drilled part.

Fixing the three consumers separately would still have left the GRBL problem, and would
need repeating for every future consumer. One expansion fixes all of them, and the audit
script now fails any program containing a `G8x` word.

This also applies to the **pre-existing** tool-sized-hole path
(`_generate_peck_drill_and_spiral_gcode`), which emitted `G83` on the hosted app.

### Hole size: a drill makes exactly one

A drill doesn't *cut* a hole, it **is** the hole — so a drawing asking for 0.196" and a
tool crib holding a #10 (0.1935") or a 13/64" (0.2031") have to meet somewhere.

This is the `clearance` case. A hole within `size_tolerance` of the drill (default
**0.010"**, under a 64th — the granularity a fractional drill index actually offers) is
drilled **at the drill's diameter**, and the substitution is reported:

```
Drill bolt holes: 4 hole(s) drawn at 0.1960 in will be drilled at 0.1935 in
(T1 #10 twist drill), a difference of 0.0025 in.
```

That is a warning, not an error — but it is always stated, because the finished part
measures the drill and not the drawing, and whoever reads the program should know that
before the bolt does. Outside the tolerance the difference is real design intent rather
than a stocking gap, so it fails with both numbers and a suggestion (a different drill,
or bore it with an end mill).

Note this cuts both ways: a hole slightly **smaller** than the drill still snaps. The
survey's usual gate is an end-mill rule — the cutter must fit inside the hole — and
applying it to a drill rejected a 13/64" against a 0.196" drawing before the tolerance
ever got a say. `MultiToolJob.smallest_tool_diameter` discounts a drill by the tolerance
for exactly this reason.

### Suggesting drills from the DXF

`drill_sizes` holds the standard index — 134 sizes across the fractional, number and
letter series — and `/part-features` returns a recommended drill for every distinct hole
size in the part, which the Tools & Ops step shows per part with a one-click **+ Add
drills** button. `/api/drill-sizes?diameter=0.1935` answers the same question directly.

CAD nearly always draws holes at real drill sizes, so the answer is usually exact
(0.1935 → **#10**, 0.196 → **#9**, 0.2656 → **17/64"**). A size with no standard drill —
a 1.125" bearing bore — is reported as *bore this with an end mill* rather than forced to
a near miss.

**The suggester is drill-aware, and that matters.** It used to sort drills in with the
end mills and assign holes by diameter *range*, which a drill cannot honour because it
makes exactly one size. That is how a 5/32" drill got proposed for 0.1935" holes and
produced a plan that failed the instant it ran. A drill now only ever receives holes
drawn at its own size.

**There is exactly one suggestion path**, `suggest_tooling`. It reuses tools already in
the job — by their real slot numbers, so nothing needs remapping — and proposes new ones
only for what those cannot do. A second, tool-constrained suggester used to exist
alongside it and auto-filled the operation list, so a part surveyed with just a default
end mill silently got a plan that milled every hole while the better answer sat behind a
button. Two suggestion paths that can disagree is precisely how the wrong drill gets
proposed; there is now nothing to disagree with.

### Feeds

Drilling is quoted on **surface speed and feed per revolution**, not chipload per tooth at
some radial engagement, so `feeds_speeds.calculate_drill_feeds` replaces the milling model
entirely for a drill. Running the milling model here produced a *lateral* feed (150 IPM on
a clamped 1/4" cutter) being used as a plunge rate.

```
rpm    = clamp(SFM * 12 / (pi * D), machine rpm range)
ipr    = ipr_ref * (D / 0.25)^0.5
plunge = min(rpm * ipr, machine z_feed_max)
```

Expect a warning on a router: the ideal RPM for anything but a small drill is below what a
2.2 kW spindle will turn. A 1/4" drill in plywood wants ~4,600 RPM and the Omio's floor is
6,000, so it gets clamped and the operator is told the drill will run hot.

---

## Feeds and speeds

The material presets in `team_config.py` are all quoted for one 4 mm single-flute cutter.
They stop meaning anything the moment the diameter or flute count changes, which is
exactly what multi-tool mode does — so feeds are re-derived per tool from `feeds_speeds`,
the module written for that job.

Per operation, from the tool's diameter and flute count and the operation's engagement:

| Set from `feeds_speeds` | Kept from the material preset |
|---|---|
| spindle RPM, cutting feed, ramp feed, plunge feed | ramp angle, ramp start clearance |
| stepover fraction | tab width and height, helix radius multiplier |
| peck depth (scaled by diameter) | corner-slowdown floor |
| depth of cut — **but clamped, see below** | |

Engagement per op type: `holes` → `slot` (a helical bore engages all round), `pockets` →
`pocket` (partial engagement, full reference chipload), `perimeter` and `chamfer` →
`profile`.

### Flutes multiply feed only while chips can escape

The chipload model feeds `RPM x flutes x chipload` — each flute takes a healthy chip, so
more flutes mean more feed. That assumption breaks in gummy metal: in a 6061 slot, flutes
past the material's `feed_flutes_max` (2 for aluminum) cannot clear their chips. They
recut and weld them, the tool seizes, and it snaps. Feeding a 1/8" 4-flute at the 4-flute
rate commanded 150+ IPM and broke real cutters, which is why the multiplier is now capped
at the evacuation limit: a 4-flute in aluminum is fed at the 2-flute rate, and the
warnings say both that the feed was held down and that each tooth is now taking half a
chip (rubbing risk) — the actual fix being a 1- or 2-flute cutter. Wood and plastics keep
the uncapped scaling; there `max_flutes_soft` stays advisory, because their chips clear
(or the failure is melting, which slowing down makes worse, not better).

### Depth of cut is clamped, never raised

`max_slotting_depth` is the one preset value the model would otherwise make *more*
aggressive, and it drives the pass count directly. `feeds_speeds` derives `slot_stepdown`
as a fixed multiple of diameter (0.38 × D in aluminium since the 2026-08-24 derate,
2.55 × D in plywood). That matches the preset exactly at the 4 mm reference tool and
then diverges for other diameters:

| 0.25" 6061, 3/8" 2-flute | preset | model |
|---|---|---|
| max depth of cut | 0.060" | 0.143" |
| passes for a 0.258" profile | 5 | 2 |

A deeper full-width pass at the (legitimately higher) feed for a 3/8" cutter piles MRR
onto a hobby router fast — a stalled spindle or a snapped end mill. So the applied value
is `min(model, preset)`: a smaller cutter is still scaled *down* below the preset, but
nothing is ever scaled up on the strength of a chipload model. Taking an extra pass
costs time; over-committing costs a cutter. (Before the derate the aluminium numbers
were 55 IPM and 1.27 × D — refuted in the field by broken 1/8" cutters; see
MULTI_TOOL_STATUS item 10.)

On top of the automatic clamps, the operator can set their own ceiling: `max_pass_depth`
on the job (the wizard's "Max depth per pass" field, or `--max-pass-depth` on the CLI).
It is applied after everything above and is likewise clamp-only — the way to baby a
fragile or multi-flute cutter is more, shallower passes, never a deeper one.

**Tab removal obeys the same limit.** It used to be the one cut that didn't: it slotted
whatever was standing in a tab in a single move, at whatever height that was. In the
usual flows the perimeter's intermediate passes thin the tab spans first (only the
final pass lifts over them), which is the only reason this never bit — a single-pass
profile on thin stock leaves tabs the full plate thickness, and the removal pass would
slot all of it in one move. The contour generator now tells the removal pass how much
material is actually standing in a tab, and removal steps down through it within
`max_slotting_depth`, re-entering through the open kerf for each pass. `gcode_audit`
carries an independent engagement checker that fails any program where a straight feed
move bites materially more than the per-pass limit.

**In metal, feed is anchored to the tested preset.** The chipload model's feed for a
multi-flute cutter can exceed anything the machine has demonstrated — it quoted a 1/8"
cutter 85.9 IPM against the 55 IPM the aluminum preset was tuned at, and that too broke
a real bit. For materials with `feed_flutes_max` (the ones that seize), the derived feed
is capped at `preset_feed × (D/D_ref)^0.7` — the same never-raise-above-tested rule the
depth already follows. Wood and plastics keep the model's numbers; an explicit per-tool
feed override still wins.

### …and clamped again by spindle power

The preset clamp is not enough on its own. **A chipload model looks at one tooth at a
time and never at total load.** It will hand a 3/8" cutter a perfectly legitimate
0.0042 in/tooth and a 150 IPM feed — and in a full-width profile cut at 0.129" deep that
is 2.2 hp out of a 2.2 kW spindle that usefully delivers about 2.1.

Cutting power is roughly `MRR × unit_power`, and `MRR = axial × radial × feed`, so for a
given cutter and feed there is a depth past which the spindle simply bogs. `max_depth_for_power`
solves for it, using **full-diameter radial engagement** — a profile cut is a slot, with
the part on one side and the offcut on the other, and that is the worst case the same
depth setting has to survive.

Measured on the Omio X8 in 0.25" 6061 (the tested 4 mm reference draws 0.33 hp):

| tool | before | after |
|---|---|---|
| 1/4" 2F | 0.129" × 2 passes → 1.35 hp | unchanged |
| 3/8" 2F | 0.129" × 2 → **2.18 hp, over** | 0.086" × 3 → 1.45 hp |
| 1/2" 3F | 0.129" × 2 → **2.90 hp, over** | 0.086" × 3 → 1.94 hp |

Three properties worth keeping true:

- **It never binds on wood or plastic.** Plywood's unit power is a sixth of aluminium's;
  there the limit is chip evacuation and feed rate, not the motor. A guard firing there
  would just make every wood job slower for nothing.
- **It does not touch the tested 4 mm reference.** If the guard moved the config the
  team's aluminium numbers were tuned against, the guard would be wrong.
- **It tracks the machine.** A `generic_light_router` (1.25 kW) gets 0.070" where the Omio
  gets 0.122". Machines with no `spindle_kw` are simply unlimited by it.

The operator is told when it binds, because the job takes longer than they might expect:
*"depth of cut reduced … so a full-width pass stays inside what the spindle can drive. A
bigger cutter is not always a faster one on a router."*

Explicit `feed_rate` / `plunge_rate` / `spindle_speed` on a `Tool` override the derived
values, and are validated — they become F and S words verbatim, and a negative F is not a
slow cut but undefined behaviour at the controller. Pinning the cutting feed also
rescales the ramp and plunge feeds by the same factor, so the relationship the preset
encodes survives the override.

---

## Validation

Generation fails, listing every problem at once, when:

- a **hole or pocket** no operation claims would be left in the finished part
- a hole or pocket is claimed by two operations and would be cut twice
- two features sit exactly on top of each other in the DXF (see below)
- the part is a 2.5D / multi-layer DXF (multi-tool is 2D only)
- an operation names a tool that isn't in the list, or two tools share a slot
- the tool doesn't suit the operation (V-tool milling, drill profiling, chamfer without a
  V-tool)
- a chamfer doesn't fit its tool, its hole, its pocket, or the part
- a `perimeter` or `chamfer` operation carries a depth
- a feed, speed, diameter, depth or width isn't a positive real number
- **anything is scheduled after a profile that cuts the part free** — see below
- a part has no operations at all

Neither coverage check is conditional on the part happening to have an operation of that
kind, and both cover pockets as well as holes. Gating them (as this first did) meant a
plan of `[pockets, perimeter]` on a five-hole plate passed silently and drilled nothing —
the exact failure the check exists to prevent.

### Scoping by index: absent vs empty

An **absent** `indices` key means "select by size/area range", and an empty range means
every feature. An `indices` key that is **present but empty** means the user picked
nothing and selects nothing. Testing it for truthiness conflates the two, and a cleared
feature picker in the UI then falls through to the range branch and cuts every hole in
the part with that tool.

### Coincident duplicates

Operations are matched to features by rounded geometry (see *Feature identity* above), so
two features that round to the same key are indistinguishable to every scope and to the
cut-twice guard — which collapses them into one and stays silent while the machine bores
the hole a second time. A duplicate is a CAD mistake (a pasted-over circle), so it is
reported by location rather than guessed at.

### The profile frees the part

Tabs are what make "profile, then chamfer" safe: the part stays anchored until a
deliberate tab-removal pass at the very end of the program. When a team's config turns
tabs off (`machining.tabs.enabled: false`), or turns off automatic removal, a perimeter
cuts clean through — and anything scheduled afterwards runs beside, or on top of, a part
lying loose on the sacrifice board under a spinning cutter.

Single-tool mode never had to think about this because it always cut the profile last.
Multi-tool is the first mode that can schedule work after one, so `_validate_profile_order`
refuses such a plan outright: there is no toolpath that makes it safe. With tabs enabled
nothing is reported and the deferral described above handles it.

Warnings don't block the download: a feed clamped by the machine's limit, a flute count
the material won't clear chips with, an operation whose scope matched nothing, a depth
past the stock thickness.

Layout overlap is checked against the **largest** tool in the job, not the one the profile
uses — a wide V-tool riding one part's edge reaches further into its neighbour than the
profile cutter does.

---

## Using it

### In the browser

Every flat 2D job includes **Tools & Ops** between Parts and Layout. Fill in the tool
table, then per part either use the suggested setup derived from the part's own features,
or build the list by hand. A one-cutter job uses the same workflow with one tool row.

`static/multitool.js` owns that step. It calls `/part-features` to survey each part so
scopes can be offered against real hole sizes, and posts to `/process-multitool`.

### From the CLI, offline

```bash
uv run python frc_cam_postprocessor.py --ops-file examples/multitool_job.json out.nc
```

The job file is the same JSON the browser posts, except each part names a `dxf` path
(resolved relative to the job file) instead of an upload index. See
`examples/multitool_job.json`, and `run_ops_file` in `frc_cam_postprocessor.py` for the
full format. Add `--config PenguinCAM-config.yaml` to use your team's settings.

---

## Related

- `docs/LOCAL_MODE.md` — running UV-CAM without Onshape
- `docs/TOOL_COMPENSATION_GUIDE.md` — how offsets work for each feature type
- `docs/Z_COORDINATE_SYSTEM.md` — where Z=0 is and why re-zeroing matters
- `docs/ASSUMPTIONS.md` — controller compatibility rules the output must obey
