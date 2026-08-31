# PenguinCAM container image.
#
# One image, two deployments - which one you get is decided entirely by environment
# variables at `docker run` time, never baked in here:
#
#   hosted (Railway)   : PORT is set by the platform; Onshape OAuth gates everything.
#   local (shop box)   : PENGUINCAM_LOCAL=1 EMBED_COOKIES=0, no sign-in, DXF from disk.
#
# See docs/LOCAL_MODE.md. Nothing below sets PENGUINCAM_LOCAL or EMBED_COOKIES,
# because doing so would silently change how the hosted deployment authenticates.

# Official Python image from Docker Hub (no GitHub downloads).
FROM python:3.11-slim

# Written by the UP2 deployment workflow and checked after the build so the job cannot
# report success for a stale image from Docker's cache.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA

# PYTHONUNBUFFERED so log() output reaches `docker logs` as it happens rather than
# sitting in a pipe buffer until the process exits - the difference between watching
# a job run and staring at nothing.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=6238

WORKDIR /app

# Open CASCADE's headless Python wheel still links against these small runtime
# libraries even though PenguinCAM does not use its viewer.  Keep the package list
# explicit and discard apt metadata so STEP support does not pull in a desktop stack.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libexpat1 libgl1 libx11-6 \
    && rm -rf /var/lib/apt/lists/*

# Requirements first so the dependency layer survives application edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Run as a non-root user. The app writes only into a tempfile.mkdtemp() directory,
# so it needs no write access to /app itself.
RUN useradd --create-home --uid 10001 penguin \
    && chown -R penguin:penguin /app
USER penguin

EXPOSE 6238

# /api/drill-sizes is an ungated JSON endpoint that touches no session, no config and
# no network - the cheapest honest "is the app actually serving?" signal here. The
# root page would work too, but it renders a template and can redirect.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/drill-sizes', timeout=4)" || exit 1

# ONE worker, deliberately - this is gunicorn's default, and the app depends on it:
# FileTokenManager keeps download tokens in process memory (a second worker would
# 404 half the downloads) and /process writes every upload to the same fixed
# UPLOAD_FOLDER/input.dxf path. --timeout 300 because generating a large multi-tool
# program can outrun gunicorn's 30s default and get the worker killed mid-job.
CMD gunicorn frc_cam_gui_app:app \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --timeout 300 \
    --access-logfile - \
    --error-logfile -
