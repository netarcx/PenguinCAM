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
	@echo "Auditing generated G-code (independent of the unit tests)..."
	@uv run python gcode_audit.py
	@echo ""
	@echo "Checking browser JavaScript (no Node available - see check_js.py)..."
	@uv run python check_js.py static/wizard.js static/multitool.js static/tube_designer.js static/gcode_viewer.js static/source_onshape.js
