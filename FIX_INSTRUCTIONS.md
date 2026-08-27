# Bug-sweep fix instructions

A five-way code review (2026-08-27, on branch `tube-designer`) found the issues below.
Every finding was verified against real generated G-code or by executing the code path —
none are speculative. This document is the complete work order: locations, the *decided*
fix for each issue (do not re-design), how to reproduce, and how to prove the fix.

## Ground rules

- Read `CLAUDE.md` first. Its rules apply throughout: `uv run` for all Python,
  `make test` after every change, `sanitize_comment()` for any text entering G-code
  comments, pure-ASCII G-code, no nested parens or square brackets in comments,
  **never open a pull request**.
- Work on a new branch `bug-sweep` off `tube-designer`.
- One commit per numbered fix (or per lettered sub-fix where large). Run
  `PYTHONIOENCODING=utf-8 make test` before every commit; also run
  `uv run python gcode_audit.py` after any toolpath change.
- **Every fix gets a regression test** in `tests/` that fails before the fix and passes
  after. Several fixes below also require making `gcode_audit.py` able to catch the bug
  class — those are called out explicitly.
- Line numbers below are as of commit `7fe7ff9`. They will drift as you edit — locate by
  the quoted code, not the number.
- Do the phases in order. Phase A items are active bit-breakers.

Useful repro scratch commands are collected at the bottom.

---

## Phase A — bit-breakers / machine crashers

### A1. Tube facing & cut-to-length rapid-plunge through the top wall

**Where:** `frc_cam_postprocessor.py` — `_generate_parametric_tube_facing`
(walls-only plunges at ~6274, 6293, 6343, 6353) and `_generate_cut_to_length`
(~7274, 7293, 7342, 7352). Pass math in `_calculate_tube_operation_passes` (~6083–6093).

**Bug:** Passes after the first assume mid-tube is hollow and emit `G0 Z{z_cut}`
(comment says "Rapid plunge (in air)"). The top wall spans the full tube width, and the
per-pass depth is `min(0.3-or-0.51, tool_diameter)`-derived, so pass 1 does **not**
necessarily cut through the top wall. Concrete case: 1/8" tool, 1x1 tube, 1/8" wall →
5 passes of 0.101"; pass 1 floor Z=0.899, wall bottom Z=0.875; pass 2 rapids from safe
height to Z=0.798 straight through 0.024" of solid 6061 at mid-tube X. The default 4 mm
tool clears by only ~0.001" — inside extrusion wall tolerance. The uncleared web is also
never removed by any later pass, so cut-to-length can fail to sever the tube.

**Fix (both functions, both roughing and finishing ladders):**
1. A pass may use the "walls only" branch **only when the previous pass's floor is already
   through the top wall with margin**: walls-only requires
   `z_top - pass_num * depth_per_pass <= z_top - wall_thickness - 0.02`
   (i.e. `pass_num * depth_per_pass >= wall_thickness + 0.02`; the 0.02" margin covers
   extrusion wall tolerance). Otherwise the pass runs the full-width branch.
2. Independently, change every mid-tube re-entry plunge (the ones at
   `front_wall_inner_x`) from `G0 Z` to `G1 Z ... F{plunge_rate}` — cheap insurance
   against proud saw-end stock, which the review measured at only ~0.043" clearance at
   mid-span. The initial plunge at `start_x` (which is off the tube in X) may stay G0.
3. Update the `(in air)` comments to say what is actually guaranteed.

**Prove it:** unit test generating 1x1 facing with `tool_diameter=0.125`,
`wall_thickness=0.125`: assert no `walls only` pass appears until cumulative depth
exceeds `wall_thickness + 0.02`, and assert no `G0 Z` below the previous pass floor at
any X inside `[front_wall_inner_x, back_wall_inner_x]`. Also extend
`gcode_audit.py`'s tube checks to simulate the tube cross-section (top wall = full width)
and flag any rapid Z move that enters un-cleared wall material — this audit gap is why
the bug shipped.

### A2. Aluminum protections bypassed by material spelling; unknown materials get plywood numbers

**Where:** `team_config.py:957–966` (`get_material_preset`),
`feeds_speeds.py:219–224` (`canonical_material_key`), `tooling.py:537–542`
(`resolve_feeds_material`), `feeds_speeds.py:274` (`DRILLING.get(...) or DRILLING['plywood']`),
`frc_cam_postprocessor.py:487–489` (dead warning).

**Bug (verified):** `--material al6061` (also `al`, `alu6061`, any typo like
`aluminun`) is not recognized as aluminum, and `get_material_preset` fabricates a full
plywood preset (75 IPM, 18000 RPM) relabeled with the requested name — so aluminum runs
at plywood feeds with **zero** aluminum protections (no envelope clamp, no chipload
guard, no preflight M0). Separately, in multi-tool jobs any material id not in
`feeds_speeds.MATERIALS` (brass, delrin, garolite…) silently falls back to the plywood
chipload model and **overwrites** the team's tuned preset feeds.

**Fix:**
1. `canonical_material_key`: recognize aluminum by exact tokens `al`, `alu`,
   `aluminium`, `aluminum` (word-split on non-letters) or the substrings `6061`/`7075`
   anywhere in the id. Use token matching, not substring `'al' in`, to avoid false
   positives like "alder".
2. `get_material_preset`: for a material with no team-config entry and no built-in
   preset, **do not fabricate a preset**. Return an empty dict so the caller's guard
   fires — and upgrade that guard (`frc_cam_postprocessor.py:487`) from a warning to a
   hard `ValueError` listing the known material names. A wrong feed table in metal is a
   broken bit; refusal is correct.
3. `tooling.resolve_feeds_material`: if the id is not a `feeds_speeds` material **but the
   team config defines a preset for it**, use the team preset's feeds as-is (skip the
   model overlay — do not overwrite tuned numbers with plywood math) and emit a warning
   naming the material. If neither model nor team preset knows it, raise a
   `ToolingError`. Apply the same rule to the drilling fallback at `feeds_speeds.py:274`.

**Prove it:** tests: `al6061` produces a program containing the aluminum preflight M0
and an RPM ≤ the aluminum envelope; `unobtainium` raises with the material list;
multi-tool `material='brass'` with a team preset keeps the preset feed, without one it
refuses. Check `make local` and the wizard dropdown still pass (they send canonical ids).

### A3. Unvalidated `scope.point_angle` drives the drill below the table

**Where:** `tooling.py:413–421` (`Operation.drill_point_angle`).

**Bug (verified):** `float(raw)` with no range check. `point_angle: 5` makes
`drill_point_length` compensation compute a huge tip length; the final peck lands at
`G1 Z-2.2239` — 2.2" below the sacrifice board — in a program that generates
"successfully".

**Fix:** validate in `Operation` parsing: included angle must be finite and within
**60–150°**, else `ToolingError` naming the field and the accepted range (mention 118
and 135 as the common values). Keep the existing [1°,179°] clamp in
`drill_point_length` as defense in depth.

### A4. Unvalidated `scope.spot_depth` — `G1 Z-99.75` and `Znan`

**Where:** `tooling.py:407–411`.

**Bug (verified):** no finiteness/positivity/magnitude check. `spot_depth: 100` ships a
commanded feed move 99.75" down; `NaN` (which `json.loads` accepts) emits `G1 Znan` then
crashes the cycle-time estimator with an HTTP 500.

**Fix:** `spot_depth` must be positive, finite, and ≤ min(stock thickness, 0.25").
While here, sweep **every** numeric field parsed in `Operation.from_dict` /
`Tool.from_dict` (`min_diameter`, `max_diameter`, `width`, diameters, flutes, …) through
one shared positive-finite validator — the module already has `_finite`/`_expect_positive`
helpers; make their use universal so no raw `float(raw)` remains.

**Prove it:** tests posting ops-files with `spot_depth: 100`, `NaN`, `-1`, and
`point_angle: 5` all get clean `ToolingError`s, not programs.

### A5. Part-name engraving runs with a twist drill in the spindle

**Where:** `tooling.py:1891–1895` (`generate_multitool_job`), plus `gcode_audit.py:87`.

**Bug (verified):** engraving attaches to the part's **first** body with no tool-type
check, and drill ops typically come first — so the program loads a twist drill, promises
"Axial plunge only", then feeds it sideways at 75 IPM to engrave.

**Fix:**
1. Attach engraving to the part's first **milling** body (`tool.type != 'drill'`;
   end mill or v-bit). If the part has no milling body at all, skip engraving that part
   and append a warning saying why.
2. Make the audit able to catch this class: `gcode_audit.py` currently sets
   `in_drill` from section banners, and the engrave block has its own banner. Track the
   *currently loaded tool* instead (parse the tool-change / `(Load T…)` comments and the
   header tool line) and flag any lateral G1/G2/G3 while a drill is loaded, regardless of
   banner.

**Prove it:** test: job `[holes(drill T1), perimeter(endmill T2)]` with `engrave=True`
puts the ENGRAVE section inside T2's body. Audit test: hand it the pre-fix output and
assert it now reports drill_lateral.

### A6. Tab system: collapse on small parts, and tabs thinned to one pass-depth

**Where:** `frc_cam_postprocessor.py` `_generate_contour_gcode` — placement math
~5374–5386; final-pass-only tab lifts ~5372, 5502; `_tab_material_top` ~5341–5344.

**Bug (both verified):**
- (a) `cutting_length = contour_length - ramp_distance` with no guard. Aluminum's 4°
  ramp is ~4.6" on 1/4" stock, so any part with a shorter perimeter gets **negative** tab
  spacing — a 1"×1" part generates
  `(Tabs: 3 tabs - desired spacing: 6.00", actual: -0.03")` with all tabs stacked at one
  point. Part comes loose mid-cut.
- (b) Tab lifts exist only on the final pass. Intermediate passes cut straight through
  the tab zones at their pass depth, so on 5-pass 1/4" aluminum the standing tabs are
  0.054" tall instead of the configured 0.15" — a third of the designed holding
  cross-section.

**Fix:**
1. Distribute tabs over the **full** contour length:
   `num_tabs = max(3, ceil(contour_length / tab_spacing))`,
   `actual_tab_spacing = contour_length / num_tabs` — never the ramp-reduced length, so
   spacing is always positive and the ramped stretch of the perimeter also carries tabs.
2. In segment processing, a tab zone lifts to `tab_z` on **every pass whose floor is
   below `tab_z`** — intermediate passes included, and within the final pass's ramp the
   lift applies wherever the ramp depth is already below `tab_z` (where the ramp is still
   above `tab_z`, the material is naturally intact and no lift is needed).
3. If after this a part is still too small to hold 3 tabs of the configured width
   (`3 * tab_width > contour_length`), refuse with a clear error telling the operator the
   part is too small for tabbed profiling at these settings.

**Prove it:** tests: (i) 1"×1" aluminum part → all tab spacings positive, tab centers
pairwise ≥ tab_width apart, header spacing positive; (ii) simulate Z along the perimeter
across all passes and assert standing material at each tab center is ≥ configured tab
height (this is a good `gcode_audit.py` check too — audit the emitted program, not the
generator); (iii) tab-removal pass still clears the (now full-height) tabs — re-check
`_generate_tab_removal_gcode` stepping against the new tab height, since it currently
steps from the *thinned* height.

### A7. Island gouging: unguarded full-depth links in the island-aware pocket path

**Where:** `frc_cam_postprocessor.py` `_generate_pocket_gcode_from_polygon`
~5185–5248 (bare `G1 X.. Y.. F{feed}` links at ~5207, 5220, 5239). The safe pattern
already exists in `_link_and_cut_ring` (~4663–4682); only circular rings are diverted
(`_detect_circular_ring` ~5090).

**Bug:** between contour rings and before the final island-boundary trace, the tool
feeds in a straight line at full depth from wherever it is — across a
square/rectangular island if the geometry says so. Reached from 2.5D layers with
interiors (`_generate_multilayer_gcode` ~3728).

**Fix:** route every ring-to-ring and ring-to-boundary link in this function through the
same guard as `_link_and_cut_ring`: if the straight link segment is not entirely inside
the already-cleared region (pocket polygon minus expanded islands, minus tool radius),
retract to safe Z, rapid over, and ramp back down. Refactor to share the existing
helper rather than duplicating it.

**Prove it:** test: 2.5D part, rectangular pocket with a rectangular island → assert no
cutting-depth `G1` segment intersects the island polygon (dilate island by tool radius,
check every consecutive coordinate pair at cut Z). Add the same island-intersection
check to `gcode_audit.py` for pocket sections.

### A8. Spindle RPM never re-issued between same-tool operations

**Where:** `tooling.py:1656–1684` (`assemble_job`).

**Bug (verified):** per-op derived RPM differs for the same tool (holes→slot,
pockets→pocket feed models), but only the first body's `S` word is ever emitted.
Verified case: pockets body announces 12000 RPM in its comment and feeds F30 while the
spindle still turns 9320 → chipload 29% above what the aluminum guard validated;
reversed order lands in the rubbing regime.

**Fix:** at each body boundary (same tool, no tool change), if the incoming body's
`pp.spindle_speed` differs from the previous body's, emit `S{rpm}` followed by
`G4 P1` (S alone is legal with M3 active). Tool-change boundaries already re-issue S —
leave them alone.

**Prove it:** test: aluminum job `[holes, pockets, perimeter]` on one tool → an `S`
word appears before the pockets body matching its announced RPM.

---

## Phase B — silent wrong-part output

### B1. DXF polyline bulge arcs silently dropped

**Where:** `frc_cam_postprocessor.py:1131–1141` (closed LWPOLYLINE/POLYLINE),
`:1227–1229` (HATCH polyline paths), `:1258–1268` (multilayer fallback),
`dxf_geometry.py:127–130` (open LWPOLYLINE).

**Bug (verified):** vertices read via `get_points('xy')` / `v[0], v[1]`, discarding the
bulge → a slot with semicircular ends loads as a plain rectangle, silently. Onshape is
unaffected (exports LINE/ARC); Fusion/SolidWorks/QCAD/LibreCAD exports are wrong.

**Fix:** at all four sites, when the entity has any nonzero bulge, flatten with
`ezdxf.path.make_path(entity).flattening(tol)` using the same chord tolerance the
existing arc tessellation uses (find the constant it uses; do not invent a new one).
Bulge-free polylines can keep the fast path.

**Prove it:** test building an LWPOLYLINE slot 1.0×0.25 with `bulge=1` semicircular
ends → loaded path bounding box is 1.25 long and the path has many vertices along the
arcs.

### B2. Tube header emits raw account name; other unsanitized comment sites

**Where:** `frc_cam_postprocessor.py:6704–6705` (tube pattern header);
also timestamp at `:2948/:2950` and `:6452/:6703`; `machine_coolant` at `:3004`;
`G53` park at `:2682`.

**Fix (all one-liners):**
1. `( User: {sanitize_comment(self.user_name, 'unknown')} )` — the plate header at
   `:2948` already does this and its comment explains why; mirror it.
2. Run `timestamp_display` through `sanitize_comment` at every header emission site —
   the field is client-supplied (`request.form.get('timestamp')`) and `[:16]` truncation
   is not sanitization.
3. `(Coolant: {sanitize_comment(self.machine_coolant, 'None')})`.
4. Park line: `f'G53 G0 X{px:.4f} Y{py:.4f}'` — unformatted YAML floats can emit
   `X1e-05`, which GRBL rejects.

**Prove it:** extend the existing sanitization tests: set
`user_name="Trent (Coach) Fox José"`, a hostile timestamp, and `coolant: "Air (comp)"`
in a tube + plate program; assert output is ASCII with no nested parens/brackets
(reuse `test_no_nested_comments` helpers).

### B3. Short slots misclassified as round holes

**Where:** `frc_cam_postprocessor.py:1166–1198` (`_path_as_circle`, 0.97 circularity).

**Bug (verified):** stadium contours up to ~1.3:1 aspect pass — a 0.20"×0.26"
adjustment slot in a 2.5D/STEP part is machined as a 0.235" round hole at the centroid.

**Fix:** a path is a circle only if (a) circularity ≥ 0.99 **and** (b) every vertex
radius about the centroid is within ±1.5% of the mean radius. Tessellated true circles
measure ~0.998 circularity with tiny radius spread; stadiums fail (b) immediately.

**Prove it:** tests: tessellated circle still classifies as hole; 0.20×0.26 stadium
does not (and therefore machines as a pocket/contour).

### B4. Open perimeter silently dropped; 0.1" gaps silently welded

**Where:** `frc_cam_postprocessor.py:1316–1322` (no `on_open_loop` passed),
`dxf_geometry.py:88` (`close_tolerance=0.1`) and `:148–149` (silent drop).

**Fix:**
1. Pass an `on_open_loop` callback from the direct-DXF path (the Onshape path already
   does at `onshape_integration.py:1263`) that records a warning with the chain's
   endpoints and gap size.
2. After perimeter identification: if any dropped open chain's bounding box exceeds the
   chosen perimeter's, make it a **hard error** ("outer profile did not close — gap of
   X at (x,y)") — that is the pocket-promoted-to-perimeter case, which profiles through
   the middle of the part with tabs.
3. When path-closing bridges a gap larger than 0.02", append a warning naming the gap
   size and location (keep 0.1 as the hard tolerance).

**Prove it:** tests: rectangle with a 0.15" gap in its outline + an inner pocket →
hard error, not a program; 0.08" gap → program plus a warning mentioning `0.08`.

---

## Phase C — units and CLI-only hazards

### C1. Tube modes and mm

**Where:** `frc_cam_postprocessor.py:6742` and `:6469` (hard-coded `G20`),
CLI `:7940–7942` (facing ignores `--units`/`--z-zero`), `:8005–8010` (tube DXF branch
builds a mm pp).

**Fix:** tube modes are inch-only, everywhere. In `main()`, refuse `--units mm` for any
`--mode tube-*` with a clear message (matching the refusals already in
`load_tube_pattern`/`load_tube_design`). Refuse `--z-zero` for tube modes too, with a
message that tube jobs zero at the jig (G54) and the flag does not apply — silently
ignoring it (current behavior) hides a real operator mistake.

### C2. mm-mode Z-frame constants never converted (standard mode)

**Where:** `frc_cam_postprocessor.py` `_apply_z_frame` ~2526–2530
(`clearance_height`, `sacrifice_board_depth`), drill retract `material_top + 0.1` at
~4067 and ~4161, spot approach `+0.05` at ~4139, `PECK_RETURN_CLEARANCE = 0.02` at
~4009, `tab_spacing` default.

**Bug:** feeds and some preset lengths are converted for mm, but these inch constants
are used verbatim → 0.5 mm traverse clearance over the stock, 0.008 mm through-cut
overcut (part left attached), 0.1 mm peck chip-clear, tabs every 2 mm.

**Fix:** add a helper on the post-processor, e.g. `def _len(self, inches): return
inches * 25.4 if self.units == 'mm' else inches`, and route every listed constant
through it. Grep for other bare inch literals used as lengths in emission paths while
there (`0.1`, `0.05`, `0.02`, `0.25` near Z math) and convert any that are lengths.

**Prove it:** test: mm program on 6.35 mm stock → retract ≥ 12 mm above stock,
through-cut overcut ≥ 0.5 mm, peck return clearance ≥ 0.5 mm.

### C3. No `$INSUNITS` cross-check

**Fix:** in `load_dxf`, read `doc.header.get('$INSUNITS', 0)`. If it names a unit
(1=in, 4=mm) that contradicts `self.units`, append a prominent warning (print + a
`self.warnings` entry that the routes surface) stating both units and the 25.4×
consequence. Warn, don't auto-convert — the header is unreliable in the wild.

### C4. CLI classify order (circle-perimeter parts cleared as a giant hole)

**Where:** `frc_cam_postprocessor.py:8048–8049` (tube DXF branch) and `:8146–8147`
(standard branch): `classify_holes()` runs before `identify_perimeter_and_pockets()`.
The route does the reverse, with a comment saying identify **must** come first
(`frc_cam_gui_app.py:1482`).

**Fix:** swap the two calls at both CLI sites to match the route.

**Prove it:** test: DXF whose outer boundary is a circle, run through the CLI path →
the circle appears once as perimeter, zero times as a hole.

### C5. CLI tube-holes mode overrides explicit feed flags

**Where:** `frc_cam_postprocessor.py:8024–8027` vs `apply_twist_drill_feeds` in
`load_tube_pattern` (~1838).

**Fix:** re-apply explicit `--feed-rate`/`--plunge-rate` **after** the loader runs
(honoring the "explicit flags come last" comment at ~8017), still subject to
`validate_aluminum_cutting_parameters` caps so an explicit flag cannot exceed the
aluminum ceilings.

---

## Phase D — remaining correctness and guards

### D1. Tap-drill acceptance tolerance spans five drill sizes

**Where:** `tooling.py:1081` (tap branch of `plan_drilled_holes`).

**Fix:** new constant `TAP_DRILL_TOLERANCE = 0.002` used for tap-purpose acceptance,
independent of the clearance-snap tolerance; the job-level `drill_size_tolerance` must
not widen tap acceptance beyond 0.003. (Rationale: `drill_sizes.tap_drill_for` itself
uses 0.002; ±0.010 accepts #25 through #19 for a 10-32 — stripped threads one way,
broken taps the other.)

**Prove it:** test: 10-32 tap holes + a #19 (0.1660) drill → refused; #21 (0.1590) →
accepted.

### D2. Spot-drill coverage validation broken both ways

**Where:** `tooling.py:1721–1768` (`_validate_feature_coverage`).

**Fix:** exclude `purpose: spot` ops from both the claimed set and the double-claim
check. Then: a hole claimed **only** by a spot op gets a warning ("spotted but never
drilled in this job — drill press assumed"); spot + drill on the same holes is legal
(that is the documented workflow). A real cutting op still must claim each hole exactly
once.

### D3. Helix entry gouges narrow pockets (plain path)

**Where:** `frc_cam_postprocessor.py:4726–4743` (`_generate_pocket_gcode` helix entry).
The island-aware sibling already clamps at `:5118–5122`.

**Fix:** port the same clamp: helix radius limited so the swept entry bore stays within
90% of the entry point's distance to the pocket boundary; if even the minimum helix
doesn't fit, fall back the same way the island path does.

**Prove it:** test: 0.20"-wide slot pocket, 0.157" tool, plywood → no entry move's
swept radius (arc center distance + tool radius) exceeds the pocket half-width.

### D4. Tube generator missing wall/height guard

**Where:** `frc_cam_postprocessor.py` `generate_tube_pattern_gcode` (~6580, z_offset at
~6825/6883). Only the Flask route validates `thickness < tube_height/2`.

**Fix:** raise `ValueError` inside the generator when
`not (0 < material_thickness < tube_height / 2)` — the route keeps its friendlier
message; the generator guard protects the CLI and any future caller.

### D5. Face-2 mirror axis from drawing extents

**Where:** `frc_cam_postprocessor.py:6769–6796`, `frc_cam_gui_app.py:1211–1246, 1413`
(`_detect_tube_dims`).

**Fix:** mirror face 2 about the **declared** tube width (the whitelisted size the
operator selected), never the DXF bounding box. If the detected extent differs from the
declared width by more than 0.05", warn that the drawing has no face outline and face-2
X placement depends on the declared width.

### D6. Facing/cut-to-length overtravel not in envelope checks

**Where:** sweep bounds at `:6207–6209` and `:7206–7208`; checks at
`frc_cam_gui_app.py:1525` and `frc_cam_postprocessor.py:6653–6662`.

**Fix:** envelope checks must use the real swept extents: X from
`-(tool_radius + 0.05)` to `tube_width + tool_radius + 0.05`, and for cut-to-length Y up
to `tube_length + tool_radius + finish_stock + j_offset + arc_radius`. Refuse when the
swept extent exceeds travel, with the actual numbers in the message.

### D7. Non-aluminum chipload hard-refusal regression

**Where:** `frc_cam_postprocessor.py:770–784` (refusal applies to all materials) vs
`:703–725` (RPM coordination is aluminum-only).

**Fix:** extend the RPM-coordination (lower RPM so chipload reaches the material
minimum, floor at the machine's min usable RPM) to all modeled materials, keeping the
refusal only when coordination cannot reach the minimum. This restores older two-flute
plywood configs (e.g. 70 IPM) to working, now with a corrected RPM instead of a refusal.

### D8. `_validate_profile_order` false refusal

**Where:** `tooling.py:1784`.

**Fix:** the early-return condition should be `job.config.tabs_enabled` alone. With
`tabs_enabled=True, remove_tabs=False` no removal pass is ever emitted (deferral at
~1385 is gated on `remove_tabs`), so the part stays anchored and post-profile ops are
safe; the current message ("cut free and left loose") is untrue.

### D9. `smallest_tool_diameter` ignores the job's tolerance

**Where:** `tooling.py:531`.

**Fix:** use `self.drill_size_tolerance` (the job's configured value) instead of
`DEFAULT_DRILL_SIZE_TOLERANCE`.

### D10. Hole ≈ tool diameter under permissive team config

**Where:** `frc_cam_postprocessor.py` `classify_holes` ~1715–1730,
`_generate_hole_gcode` ~4262–4274. Needs `machining.holes.min_millable_multiplier: 1.0`
in a team config to trigger.

**Fix:** (a) when a hole is dropped because the tool is too large, append to
`self.errors` (fail generation) instead of emitting only a G-code comment;
(b) when `final_toolpath_radius < 0.001`, generate the peck-drill entry **without** the
spiral pass rather than emitting a degenerate `G3 ... I-0.0000 J0` (GRBL error:33).

### D11. `feeds_speeds._resolve` unknown preset key

**Where:** `feeds_speeds.py:342–345`.

**Fix:** `presets[spec['preset']]` — raise `ValueError` naming the unknown preset and
the available names instead of `.get(..., {})`, which currently surfaces as a
`KeyError: 'preferred_rpm'` 500 in the calculator API.

### D12. `validate_feeds_speeds.py` fails at HEAD and is not wired into `make test`

**Fix:** update its `PRESETS` to the current values (commit 7fe7ff9 changed aluminum
`preferred_rpm` 18000→14000; also sync the omio plywood 70→75 IPM drift vs
`team_config.py`), confirm it exits 0, then add it to the Makefile `test` target so the
drift gate it promises actually runs.

### D13. `_offset_coordinate` regex rewrites comment text

**Where:** `frc_cam_postprocessor.py:6024–6044`.

**Fix:** apply the Y/Z offset regex only to the portion of the line before the first
`(` or `;`. Comments keep their original numbers (they describe the pre-offset frame —
if any comment *should* reflect the offset, rewrite that comment at its emission site
instead).

### D14. Cleanups (single commit)

- Remove dead `pp.tube_height` assignments (`frc_cam_gui_app.py:1473, 1507`,
  postprocessor `:8013`) and the unused `z_final` at `:6216`/`:7215`.
- Fix the stale header text "holes < 0.188 skipped" — sub-tool-size holes are actually
  peck-drilled at snapped size (only sub-tolerance holes error); make the message say
  that.
- `examples/multitool_job.json` points at `../sample_part.dxf`, which has **no holes or
  pockets**, so 3 of its 5 operations emit nothing. Add a script-generated
  `examples/example_plate.dxf` (perimeter + a few 0.201" holes + one ≥0.3" bore + one
  pocket) and point the example at it, so the shipped example demonstrates every op.
- Add an optional `flute_length` field to `Tool`/the `tools:` config block; when total
  cut depth exceeds it (or exceeds 4× diameter when unspecified), append a warning to
  the program header and the route response. Warning only — no refusal.

---

## Verification recipes (used in the review; reuse in tests)

```bash
# A2: aluminum bypass — before fix: plywood feeds, no preflight M0
uv run python frc_cam_postprocessor.py <plate.dxf> out.nc \
  --material al6061 --thickness 0.25 --tool-diameter 0.157

# A6a: tab collapse — before fix: "actual: -0.03"" in the header
python: ezdxf LWPOLYLINE square (0,0)-(1,1), save tiny.dxf
uv run python frc_cam_postprocessor.py tiny.dxf out.nc \
  --material aluminum --thickness 0.25 --tool-diameter 0.157
grep 'Tabs:' out_*.nc

# A1: tube wall rapid-plunge — before fix: pass 1 floor Z=0.899 (wall bottom 0.875),
# pass 2 "walls only" rapids to Z=0.798 at mid-tube X
uv run python frc_cam_postprocessor.py out.nc --mode tube-facing --tube-size 1x1 \
  --tube-length 12 --material aluminum --thickness 0.125 --tool-diameter 0.125
grep -n 'walls only' out_*.nc

# Note: the CLI appends a timestamp to the output filename — glob for out_*.nc.
```

## Definition of done

- `PYTHONIOENCODING=utf-8 make test` passes, including the new regression tests and the
  newly wired `validate_feeds_speeds.py`.
- `uv run python gcode_audit.py` passes, and now catches: drill-lateral regardless of
  banner (A5), tube rapid-into-wall (A1), pocket links crossing islands (A7), and
  standing-tab height (A6).
- Every Phase A item has a test that fails on `tube-designer` and passes on `bug-sweep`.
- Branch `bug-sweep` pushed. **No pull request** — report the branch and stop.
