# Multi-Tool & Local Mode — Status and Handoff

Where this work stands, what is unverified, and what is left. Written at the end of the
session that built it. Read `MULTI_TOOL_GUIDE.md` and `LOCAL_MODE.md` for *how it works*;
this file is only about *what state it is in*.

---

## ⚠ The one thing that matters most

**No multi-tool program has ever been cut on real stock.** Every check in this project is
static or simulated. That is not a small caveat:

- A bug that made drilling emit **end-mill toolpaths** (helical entry, lateral feed moves
  on a twist drill) passed the full test suite, four independent review agents, and a
  G-code audit. It was found by a human reading the output.
- The fix for it then **reintroduced a ZMIN under-report** — the header claimed −0.008"
  while the drill went to −0.083" — which again passed everything until the audit was
  pointed at it specifically.

Both produced **well-formed, rule-compliant, fully-passing programs that were wrong.**
Dry-run above the work before committing to a cut, especially anything drilled.

**The browser UI has never been rendered.** No Node, no browser harness in this
environment. `check_js.py` catches syntax damage and the HTTP round-trips prove the
server contract, but nothing has verified that a panel appears, a control is reachable,
or a layout is sane. Treat the Tools & Ops step as unproven at the pixel level.

---

## What was built

| Area | Files |
|---|---|
| Multi-tool operations model | `tooling.py` |
| Drill sizes, tap drills, purposes | `drill_sizes.py` |
| Drilling, chamfer, peck expansion | `frc_cam_postprocessor.py` |
| Local (no-Onshape) mode | `local_mode.py`, `penguincam_local.py` |
| Operations editor UI | `static/multitool.js`, `wizard.js`/`.css`/`.html` |
| Routes | `/process-multitool`, `/part-features`, `/api/drill-sizes`, `/api/tooling/presets` |
| Verification | `gcode_audit.py`, `check_js.py`, `tests/test_multitool.py` |

Design rationale lives in the module docstrings and `MULTI_TOOL_GUIDE.md`; it is not
repeated here.

---

## Verification: what each check is actually worth

`make test` runs all four:

| Check | Covers | Blind to |
|---|---|---|
| 732 unit tests | Model, scoping, ordering, drilling, guards, feeds/speeds, resume programs | Anything the author assumed wrong — they encode the same premises as the code |
| `gcode_test.py` | Output vs Fusion 360 fixtures | Only the fixture parts |
| `gcode_audit.py` | **Simulates** 66 programs: ZMIN vs real depth, rapids through material, lateral drill moves, per-pass engagement, canned cycles, comment rules, every standalone resume file, and refusals that must yield no program | Geometry correctness; anything not modelled |
| `check_js.py` | JS syntax damage, bracket balance | Behaviour, layout, everything visual |

**`gcode_audit.py` earns its place.** It is deliberately built on different premises from
the tests and it found a bug the whole suite missed. Run it after any toolpath change.

---

## Safety guards in place

Each exists because it was actually violated at some point in development:

- **Profile ordering** — refuses a plan where anything is cut after a part is freed
  (tabs disabled + later operations, or a tabless profile beside another part).
- **Spindle power** — depth of cut clamped so a full-width pass stays inside what the
  spindle can drive. A 3/8" cutter in 6061 wanted 2.2 hp from a 2.1 hp spindle.
- **Depth of cut** — never raised above the tested material preset, only lowered.
- **Tool/operation fit** — V-tool cannot mill, drill cannot be fed sideways, chamfer needs
  a V-tool.
- **Drill sizing** — a drill only gets holes drawn at its own size, ±tolerance; undersize
  substitutions state the consequence.
- **Feature coverage** — every hole and pocket must be cut exactly once.
- **No canned cycles** — GRBL 1.1 has no G81–G89, and PenguinCAM's own estimator, preview
  and simulator all parse only G0–G3.
- **Flute count in aluminum** — 3+ flute cutters are refused outright, not derated. The
  gullets pack, the packed chips weld, and the tool snaps; this is the WCP-0543 failure.
- **Aluminum safety envelope** — a shop's own preset is clamped to the router ceiling at
  the point every G-code path shares, because the old config template shipped 55 IPM and
  a 0.200" pass and changing the built-in defaults did not protect anyone carrying that
  file. Feed, pass depth, plunge and RPM are clamped down only; the corner feed floor is
  held at or above 0.6 so a slowed corner still shears a chip instead of rubbing.
- **Tool-scaled feeds** — presets are referenced to a 4mm cutter, so a smaller tool is
  derated (`scale_feeds_to_tool`), and spindle RPM is lowered as needed to keep the
  chipload at the *corner* floor above the rubbing threshold.
- **Drill feeds are not milling feeds** — every drilled hole, tube patterns included,
  goes through `feeds_speeds.calculate_drill_feeds`. Running a twist drill at the
  milling preset's 18,000 RPM is ~4x the surface speed HSS tolerates.
- **Chip evacuation is stated, not assumed** — every aluminum program opens with a
  mandatory preflight pause, and every tool change rechecks collet, stickout, runout and
  the air blast before restarting.
- **No inherited modal state** — `G92.1` is issued before the first motion of every
  program, so a stale temporary offset from an earlier run cannot shift the job. Each
  tool boundary also emits a standalone resume file that starts stopped, states what to
  verify, and only then rebuilds full modal state.

---

## Known-outstanding

**Unverified, not broken:**

1. Nothing cut on real stock (above).
2. UI never rendered in a browser (above).
3. `PenguinCAM-config.yaml` has three items marked `VERIFY` — **work envelope** (nominal
   X8 figures, not measured), **park position** (commented out), **safe height**
   (commented out). The envelope decides whether an oversized job is refused or accepted.

**Deliberately not done:**

4. Multi-tool is **2D only**. 2.5D takes depths from CAD layers and tubing runs a fixed
   program; both would need their own design. Guarded server-side, hidden in the UI, on
   the roadmap. (Tubing has since gained *pre-designed patterns* - see
   `TUBE_PATTERNS.md` - but those still run the fixed single-tool tube program.)

4b. **Nothing from `tube_patterns.py` has been cut on real stock either.** The audit gap
   is narrower than it was: tube programs now also get the G92-cancellation check, the
   aluminum preflight and chip-evacuation checks, and a rule that no axial facing level
   exceeds one cutter diameter. Their ZMIN and rapid-below-top claims are still outside
   `gcode_audit.py` - those checks assume the plate Z-frame - and are covered instead by
   tube-frame unit tests (`test_a_drilled_hole_goes_fully_through_the_wall`,
   `test_no_rapid_traverses_inside_the_tube`, and
   `test_a_hole_job_reports_the_drill_in_its_header`). That is a weaker instrument than
   the simulator: it shares the code's own premises. The warning at the top of this
   file still applies to tube patterns.
5. **Unauthenticated CPU-heavy endpoints** — `/process`, `/process-job`,
   `/process-multitool`, `/part-outline`, `/part-features` have never been behind the
   OAuth gate, which only ever covered the two HTML page routes. Pre-existing; flagged and
   left alone as out of scope.
6. **Open redirect** in `/config/refresh` via a forged `Referer`. Pre-existing, both
   branches.
7. `--host 0.0.0.0 --debug` exposes the Werkzeug debugger (RCE). Warned about loudly;
   not blocked.

**Worth a look sometime:**

8. `tap_drill_for` matches a hole against a thread's nominal *and* clearance diameter.
   That is what lets a CAD file drawn at 0.1935 be tapped 10-32, but it means a genuine
   clearance hole can be tapped by mistake if the user picks the wrong purpose. Ambiguity
   is reported, never guessed.
9. Job cost is O(parts × operations) — each operation re-reads the DXF. Capped at 120
   operations / 60 parts. Fine for real plates; would need caching to go much beyond.
10. **FIELD FAILURE, 2026-08-24 (fixed):** a 1/8" 4-flute end mill snapped on a real
   0.125" 6061 part (WCP-0543). Verified by simulating both generated programs with an
   independent engagement checker (`gcode_audit.max_lateral_engagement`):
   - The program matching what the operator watched — full plate thickness in one
     pass, "faster than ever" — is the one generated 40 seconds BEFORE the depth
     ceiling was set: a single 0.133" full-width pass at 85.9 IPM, tabs included,
     *legal* under the automatic per-pass limit (1.27 x D = 0.159" > the whole
     plate). Two programs a minute apart is a stale-download trap: the filename
     timestamp and the header's "limited to ... by operator" line tell them apart.
   - The ceiling program measured clean (worst true bite ~0.033" vs its 0.031"
     ceiling, within instrument error) — its five perimeter passes pre-thinned the
     tabs before removal ran.
   Fixed regardless: **(a)** the 85.9 IPM was 2-flute chipload theory against the 55
   IPM the machine had actually been tested at — metal feeds are now anchored to the
   preset's diameter-scaled tested rate (`tooling._anchor_metal_feed`); **(b)** tab
   removal genuinely ignored `max_slotting_depth` in code (it slots whatever is
   standing in one move) — safe here only because the passes had thinned the tabs
   first; it now steps down like every other cut, fed the true standing-tab height
   by the contour generator; **(c)** the audit permanently carries this program
   shape plus the engagement checker, which fails any future path that escapes the
   per-pass limit by more than the checker's error band.
11. **FIXED: the drilled tube pattern ran its 0.201" twist drill at the material preset's
   18,000 RPM with a 15 IPM plunge** — about 950 SFM at 0.0008 IPR in aluminum, i.e.
   ~4× the surface speed HSS tolerates while feeding ~4× too lightly. That is the
   rubbing/work-hardening regime that burns and snaps drills.
   The tube pattern now uses `feeds_speeds.calculate_drill_feeds`, clamps to the
   spindle's 6,000 RPM floor and the aluminum 15 IPM plunge ceiling, and pecks at D/3.
   Generated drilling is regression-tested separately from milling.

---

## Environment notes

- **`PYTHONIOENCODING=utf-8` is required** to run the tests on a Windows console. The
  post-processor prints emoji on error paths; when a test fails while capturing that
  output, unittest's own reporting dies on cp1252 and the run aborts with the real failure
  hidden. This costs an hour if you do not know it.
- `make` was installed via `winget install ezwinports.make`. All three targets work.
- `uv` manages the venv; `make local` runs the app with no Onshape and no network.
