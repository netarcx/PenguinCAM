.PHONY: install test local

install:
	@command -v uv >/dev/null 2>&1 || { echo "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	@echo "Installing dependencies from requirements.txt..."
	uv pip install -r requirements.txt

# Run PenguinCAM as a local desktop app: no Onshape sign-in, no cloud, DXF files from
# disk. Bound to localhost; pass ARGS to forward flags (e.g. make local ARGS=--port=7000).
local:
	uv run python penguincam_local.py $(ARGS)

test:
	@echo "Running unit tests..."
	@uv run python -m unittest discover -s tests --buffer
	@echo ""
	@echo "Running system tests..."
	@uv run python gcode_test.py --quiet
	@echo ""
	@echo "Checking the feeds model against the tuned presets..."
	@uv run python validate_feeds_speeds.py
	@echo ""
	@echo "Auditing generated G-code (independent of the unit tests)..."
	@uv run python gcode_audit.py
	@echo ""
	@echo "Checking browser JavaScript..."
# Prefer a real parser. check_js.py is a brace/string/regex balancer and cannot see
# undefined identifiers, use-before-definition, duplicate const, await outside async,
# or ASI hazards; `node --check` catches all of those and takes ~0.2s for the six
# files. Node is not guaranteed on a deploy host, so fall back rather than require it.
	@if command -v node >/dev/null 2>&1; then \
		for f in static/*.js; do node --check "$$f" || exit 1; done; \
		echo "== node --check -> 0 problem(s)"; \
	else \
		echo "== node not found, using check_js.py only"; \
	fi
	@uv run python check_js.py static/wizard.js static/multitool.js static/tube_designer.js static/gcode_viewer.js static/source_onshape.js static/bed_leveling.js
