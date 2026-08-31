# Local Mode — running UV-CAM without Onshape

Read this before changing `local_mode.py`, `penguincam_local.py`, or any of the
authentication gates in `frc_cam_gui_app.py`.

The hosted app gates everything behind Onshape OAuth. That gate does three jobs at once:
it identifies the user, it's where the team's `PenguinCAM-config.yaml` comes from, and —
deliberately — it keeps anonymous internet traffic off the public site.

None of that applies on a laptop next to the router. The DXF or STEP file is already on disk, the
operator is standing at the keyboard, and the shop wifi may not reach the internet at all.

**Local mode is that second deployment.** Same app, same routes, same post-processor, with
the OAuth gate lifted and the team config read from a file.

---

## Running it

```bash
make local
# or
uv run python penguincam_local.py
```

It picks a free port starting at 6238, binds to `127.0.0.1`, and opens a browser.

| Flag | Effect |
|---|---|
| `--port N` | Listen on a specific port instead of the first free one from 6238 |
| `--config PATH` | Use this `PenguinCAM-config.yaml` |
| `--host HOST` | Bind address (default `127.0.0.1`) |
| `--no-browser` | Don't open a browser window |
| `--debug` | Flask debug mode with the auto-reloader |

Flat 2D parts come from DXF files dropped on the page. A single-solid STEP file can be
dropped in 2.5D mode for top-down features such as through-holes, pockets, steps, and
counterbores; stock thickness and feature depths come from the solid. The importer chooses
the machinable side from the feature directions and refuses assemblies, features from both
sides, side holes, undercuts, sloped faces, fillets, and free-form surfaces rather than
silently flattening them. G-code downloads straight back to disk.

Onshape one-click import and Google Drive upload are simply not part of this deployment —
everything else, including [multi-tool operations](MULTI_TOOL_GUIDE.md), works the same.

### Fully offline, no browser

```bash
uv run python frc_cam_postprocessor.py --ops-file myjob.json out.nc
```

See `docs/MULTI_TOOL_GUIDE.md` and `examples/multitool_job.json`.

---

## Team configuration

There's no Onshape to fetch `PenguinCAM-config.yaml` from, so local mode reads it from
disk. In order of precedence:

1. `--config PATH` (or the `PENGUINCAM_CONFIG` environment variable)
2. `PenguinCAM-config.yaml` (or `.yml`) in the working directory
3. Built-in Team 6238 defaults

Download your team's config from your Onshape classroom and drop it next to the app, and
local runs use the same feeds, machine limits, and tab settings as the hosted app.

Two deliberate differences from the hosted config path:

- A `--config` path that doesn't exist is **reported**, not silently ignored. A typo that
  quietly fell back to defaults would have the machine running on someone else's feeds.
- A malformed config file falls back to defaults **and says so**, rather than reporting a
  team number that was never loaded. (`TeamConfig.from_yaml` returns Team 6238's defaults
  on a parse error, which is right for the hosted app — a bad config in someone's Onshape
  classroom shouldn't lock them out — but wrong here.)

Editing the config file and clicking the reload glyph in the header re-reads it; no
restart needed.

---

## How the gate is lifted

`local_mode.is_local_mode()` reads `PENGUINCAM_LOCAL` from the environment, and
`frc_cam_gui_app.py` reads that flag **once at import**, so every gate in the process
agrees about which mode it's in. `_require_onshape_auth()` returns `None` immediately in
local mode; nothing else about the routes changes.

`penguincam_local.py` sets the variable before importing the app. If it is unset — as it
is on Railway and Vercel — every gate behaves exactly as it did before, which is what
keeps the hosted deployment unaffected by any of this.

The launcher also pins `EMBED_COOKIES=0`. A fixed `FLASK_SECRET_KEY` would otherwise
switch the app into cross-site cookie mode (`SameSite=None; Secure`), which exists for
embedding in the Onshape iframe over HTTPS. Local mode serves plain `http://` and embeds
in nothing, and a `Secure` cookie there is browser-dependent at best.

---

## Security

**Binding to loopback is what makes lifting the gate safe.** Only someone at that keyboard
can reach the app.

Rate limits stay on. Local mode removes authentication and nothing else, so
`--host 0.0.0.0` puts an unauthenticated UV-CAM on your network — the launcher warns
out loud before it does that. Don't expose the port, and don't set `PENGUINCAM_LOCAL` on a
deployed instance.
