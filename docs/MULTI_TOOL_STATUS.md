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
| 352 unit tests | Model, scoping, ordering, drilling, guards | Anything the author assumed wrong — they encode the same premises as the code |
| `gcode_test.py` | Output vs Fusion 360 fixtures | Only the fixture parts |
| `gcode_audit.py` | **Simulates** 20 programs: ZMIN vs real depth, rapids through material, lateral drill moves, canned cycles, comment rules | Geometry correctness; anything not modelled |
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
   the roadmap.
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

---

## Environment notes

- **`PYTHONIOENCODING=utf-8` is required** to run the tests on a Windows console. The
  post-processor prints emoji on error paths; when a test fails while capturing that
  output, unittest's own reporting dies on cp1252 and the run aborts with the real failure
  hidden. This costs an hour if you do not know it.
- `make` was installed via `winget install ezwinports.make`. All three targets work.
- `uv` manages the venv; `make local` runs the app with no Onshape and no network.
