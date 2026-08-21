# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PenguinCAM is a web-based CAM post-processor that generates CNC G-code from DXF files exported from Onshape. Built for FRC Team 6238, it automates the CAD-to-CNC workflow for flat plates without requiring CAM software.

**Live deployment:** https://penguincam.popcornpenguins.com

## Development Commands

**Important**: This project uses `uv` for Python environment management locally. All Python commands should be run with `uv run`:

```bash
# Install dependencies
make install

# Run development server (opens http://localhost:6238)
uv run python frc_cam_gui_app.py

# Run locally with no Onshape sign-in and no cloud services (see docs/LOCAL_MODE.md)
make local

# Run every check: unit tests, G-code comparison, the independent audit, and JS integrity
make test

# On Windows, prefix test runs with this or a FAILING test aborts the run with the real
# message hidden (the post-processor prints emoji; unittest's reporting dies on cp1252):
#   PYTHONIOENCODING=utf-8 make test

# Run postprocessor directly (CLI)
uv run python frc_cam_postprocessor.py INPUT.dxf OUTPUT.nc \
  --material plywood \
  --thickness 0.25 \
  --tool-diameter 0.157

# Run a multi-tool job from a JSON file, fully offline (see docs/MULTI_TOOL_GUIDE.md)
uv run python frc_cam_postprocessor.py --ops-file examples/multitool_job.json OUTPUT.nc

# Test any Python module import
uv run python -c "from frc_cam_gui_app import app; print('OK')"
```

## Dependency Management

**CRITICAL**: This project uses `requirements.txt` for dependency specification (NOT `pyproject.toml`).

- **Local development**: Uses `uv` with `requirements.txt` (run `make install` or `uv pip install -r requirements.txt`)
- **Deployment**: Railway reads `requirements.txt` to install dependencies
- **Adding dependencies**: Edit `requirements.txt` directly, then run `uv pip install -r requirements.txt`
- **DO NOT** create or use `pyproject.toml` - it will cause confusion with Railway deployment

```bash
# Add a new dependency
echo "new-package>=1.0.0" >> requirements.txt
uv pip install -r requirements.txt
```

## Development Rules

**Always run `make test` after making any code changes.** If tests fail, fix the errors before proceeding with other work. Do not commit or consider a change complete until all tests pass.

## G-code Generation Rules

**CRITICAL**: The CNC machines that run our G-code have strict requirements. Violating these rules will cause machine failures:

### Nested Comments - FORBIDDEN
- **NEVER use nested parenthesis comments** in G-code output
- ❌ Bad: `(Outer comment (nested comment) more text)`
- ✅ Good: `(Outer comment, nested text, more text)`
- CNC controllers will fail or produce unpredictable behavior with nested comments
- There is a unit test (`test_no_nested_comments`) but it doesn't catch every case since some G-code is conditional
- **Square brackets are also forbidden inside comments** (`test_no_square_brackets_in_comments`) - some controllers read them as expressions
- For any text that came from a user, a tool name, or CAD (part names especially), run it through `sanitize_comment()` in `frc_cam_postprocessor.py` rather than hand-rolling a replacement - it handles all three rules at once

### Unicode Characters - FORBIDDEN
- **All G-code must be pure ASCII** - no unicode characters
- ❌ Bad: `(Cut depth: 0.25″)` (curly quotes), `(Feedrate → 75 IPM)` (arrows)
- ✅ Good: `(Cut depth: 0.25")` (straight quotes), `(Feedrate: 75 IPM)` (colon)
- There is a unit test (`test_no_unicode_characters`) but it doesn't catch every case

### Best Practices
When generating G-code comments:
1. Use commas or semicolons instead of nested parentheses
2. Use straight ASCII quotes and standard punctuation
3. Test conditional code paths manually if they generate comments
4. Be especially careful with f-strings that include measurements or user data

## Git Operations

**NEVER open a pull request.** Not to upstream, not on this fork, not as a draft, and not
because a `git push` printed the "Create a pull request" hint. Pushing a branch is the end
of the job — report the branch URL and stop. Do not suggest opening one, do not offer it
as a next step, and do not mention `gh pr create` as a convenience. Opening a PR puts the
work in front of other people and starts a conversation whose timing is the maintainer's
call, not the assistant's. If a PR is wanted, it will be asked for explicitly.

Committing, pushing and forking are fine **when asked**. Anything else outward-facing —
making a repo public, changing visibility, pushing to upstream rather than a fork — needs
confirming first, because it is hard to undo.

Always check for secrets before a push. `PenguinCAM-config-2129.yaml` is deliberately
tracked (machine specs and feeds, nothing private); every other `PenguinCAM-config-*.yaml`
stays gitignored so another team's config cannot be committed by accident.

## Architecture

```
Browser (wizard.html + Three.js)
    ↓ HTTP POST /process, /process-job
Flask Server (frc_cam_gui_app.py)
    ↓ subprocess
G-code Generator (frc_cam_postprocessor.py)
    ↓
.nc file → 3D visualization / download / Drive upload
```

**Key files:**
- `frc_cam_gui_app.py` - Flask routes, Onshape OAuth, Drive integration
- `frc_cam_postprocessor.py` - Core G-code generation (`FRCPostProcessor` class)
- `tooling.py` - Multi-tool jobs: one post-processor per operation, stitched with manual tool changes
- `drill_sizes.py` - Standard drill index, tap drills, and picking a drill for a hole
- `gcode_audit.py` - Simulates generated programs and checks physical claims. Built on
  DIFFERENT premises from the unit tests on purpose - it has caught bugs the whole suite
  passed. Run it after any toolpath change.
- `check_js.py` - Static integrity check for `static/*.js`. There is no Node here, so this
  is the only thing standing between a JS syntax error and a wizard that will not start.
- `local_mode.py` + `penguincam_local.py` - Local (no-Onshape) deployment and its launcher
- `templates/wizard.html` - Multi-part wizard UI (the whole app; served at `/`, `/app`, and the Onshape panel), with `static/wizard.js` + `static/gcode_viewer.js` (Three.js 3D visualization)
- `onshape_integration.py` - Onshape API client for one-click export
- `penguincam_auth.py` - Google Workspace OAuth (optional)
- `google_drive_integration.py` - Drive upload (optional)

## Documentation

Detailed documentation lives in the `docs/` directory. **Read these before modifying related code:**

| File | When to Read |
|------|--------------|
| `MULTI_TOOL_STATUS.md` | **Read first when resuming multi-tool work** - what is unverified and what is left |
| `MULTI_TOOL_GUIDE.md` | Changing `tooling.py`, the multi-tool routes, `static/multitool.js`, drilling or the chamfer code |
| `TUBE_PATTERNS.md` | Changing `tube_patterns.py`, `load_tube_pattern`, or the tube branch of `/process` |
| `LOCAL_MODE.md` | Changing `local_mode.py`, `penguincam_local.py`, or any authentication gate |
| `Z_COORDINATE_SYSTEM.md` | Modifying Z-axis calculations, safe heights, cut depths, or plunge moves |
| `TOOL_COMPENSATION_GUIDE.md` | Changing offset logic for perimeters, pockets, or holes |
| `ASSUMPTIONS.md` | Adding/changing G-code output; lists controller compatibility requirements |
| `MACHINE_CHECKLIST.md` | Updating G-code header comments or safety checks |
| `DEPLOYMENT_GUIDE.md` | Changing environment variables, Railway config, or OAuth redirect URIs |
| `AUTHENTICATION_GUIDE.md` | Modifying Google OAuth flow, session handling, or domain restrictions |
| `INTEGRATIONS_GUIDE.md` | Changing Onshape API calls or Google Drive upload logic |
| `ONSHAPE_SETUP.md` | Updating the Onshape browser extension or import URL format |
| `quick-reference-card.md` | Changing UI workflows or default settings that affect end users |

## Testing

Tests compare PenguinCAM output against Fusion 360 CAM output using the `pygcode` library. Test fixtures are DXF files with known expected G-code.

```bash
make test  # Runs all comparison tests
```
