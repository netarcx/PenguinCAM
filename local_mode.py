"""Local (offline) mode for PenguinCAM.

The deployed app gates everything behind Onshape OAuth: it is how a user is identified,
how the team's `PenguinCAM-config.yaml` is fetched, and - deliberately - how the public
site keeps anonymous traffic out. None of that applies when PenguinCAM is running on a
laptop next to the router, where the operator already has the DXF on disk and there may
not even be a network.

Local mode is that second deployment: same app, same routes, same post-processor, with
the OAuth gate lifted and the team config read from a file instead of from Onshape. It is
opt-in through the environment (`PENGUINCAM_LOCAL=1`, which `penguincam_local.py` sets),
so nothing about the hosted deployment changes by accident - if the variable is unset,
every gate behaves exactly as it did before.

Local mode only removes the *authentication* gate. It does not disable rate limits, and
the app should still be bound to localhost (which the launcher does), because lifting the
gate means anyone who can reach the port can use it.
"""

import glob
import os
import re

from logging_config import log

#: Environment variable that turns local mode on.
LOCAL_ENV_VAR = 'PENGUINCAM_LOCAL'

#: Filename patterns searched when no config is given explicitly. The bare
#: `PenguinCAM-config.yaml` is the name teams already keep in their Onshape classroom, and
#: the suffixed forms let a shop keep several side by side and still have them found
#: automatically - `PenguinCAM-config-2129.yaml`, `PenguinCAM-config-omio.yaml`. Without
#: the wildcard, renaming the file to something recognisable silently disabled discovery
#: and the machine quietly ran on built-in defaults.
DEFAULT_CONFIG_PATTERNS = ('PenguinCAM-config.yaml', 'PenguinCAM-config.yml',
                           'PenguinCAM-config-*.yaml', 'PenguinCAM-config-*.yml')

CONFIG_ENV_VAR = 'PENGUINCAM_CONFIG'


def is_local_mode() -> bool:
    """True when the app is running as a local, no-Onshape install."""
    return os.environ.get(LOCAL_ENV_VAR, '').strip().lower() in ('1', 'true', 'yes', 'on')


def find_local_config_path() -> str:
    """Path of the team config to load in local mode, or '' if there isn't one.

    An explicit `PENGUINCAM_CONFIG` wins - including pointing at a file that does not
    exist, which is reported rather than silently ignored, because a typo'd path that
    quietly falls back to defaults would have the machine running someone else's feeds.
    """
    explicit = os.environ.get(CONFIG_ENV_VAR, '').strip()
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        log(f"[LOCAL] {CONFIG_ENV_VAR} points at {explicit}, which does not exist - "
            f"using built-in defaults")
        return ''
    # Working directory first (the file you are standing next to wins), then the
    # directory the app itself lives in. Searching only the CWD meant `make local` found
    # the config but launching the same app from anywhere else silently did not, and the
    # only symptom was the machine quietly running on built-in defaults.
    for directory in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        for pattern in DEFAULT_CONFIG_PATTERNS:
            # Sorted so a directory holding several configs picks the same one every
            # time. Ambiguity is reported rather than resolved silently: running on a
            # different team's feeds because two files were present would be worse than
            # a line of output.
            matches = sorted(glob.glob(os.path.join(directory, pattern)))
            matches = [m for m in matches if os.path.isfile(m)]
            if not matches:
                continue
            if len(matches) > 1:
                log(f"[LOCAL] {len(matches)} configs match {pattern} in {directory}; "
                    f"using {os.path.basename(matches[0])}. Pass --config to choose.")
            return os.path.abspath(matches[0])
    return ''


def config_fingerprint() -> str:
    """Identity of the config currently on disk: path plus modification time.

    Lets a request cheaply tell whether the config it cached is still the config on
    disk - covering a file that was edited, a file that has appeared since, and a file
    that has been deleted (all three change the fingerprint). One stat() per page render.
    """
    path = find_local_config_path()
    if not path:
        return ''
    try:
        return f'{path}@{os.path.getmtime(path):.0f}'
    except OSError:
        return path


def load_local_team_config():
    """Load the local team config file.

    Returns (TeamConfig or None, path). None means "no usable config found, use the
    built-in defaults", which is the normal case for a team that has never written one.
    """
    path = find_local_config_path()
    if not path:
        return None, ''

    # Imported lazily so this module stays importable (for `is_local_mode`) even in an
    # environment where the heavier config machinery is unavailable.
    import yaml
    from team_config import TeamConfig

    # Parsed here rather than through TeamConfig.from_yaml, which reports a malformed file
    # and then hands back Team 6238's defaults. That fallback is right for the hosted app
    # - a bad config in someone's Onshape classroom should not lock them out - but here it
    # would have the app claim a config is loaded while running on someone else's feeds.
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh.read())
    except OSError as exc:
        log(f"[LOCAL] Could not read {path}: {exc} - using built-in defaults")
        return None, ''
    except UnicodeDecodeError as exc:
        # Not an OSError and not a YAMLError, so this escaped both handlers and came out
        # of _app_template_context as an HTTP 500 on EVERY page - with nothing pointing at
        # the config file. Notepad's "Unicode" (UTF-16) save option is all it takes.
        log(f"[LOCAL] {path} is not UTF-8 text: {exc} - using built-in defaults. "
            f"Re-save it as UTF-8.")
        return None, ''
    except yaml.YAMLError as exc:
        log(f"[LOCAL] {path} is not valid YAML: {exc} - using built-in defaults")
        return None, ''

    if not isinstance(data, dict):
        log(f"[LOCAL] {path} does not contain a PenguinCAM config - "
            f"using built-in defaults")
        return None, ''

    try:
        config = TeamConfig.from_dict(data)
    except Exception as exc:                      # structurally wrong, not just unparsable
        log(f"[LOCAL] {path} is not a usable PenguinCAM config: {exc} - "
            f"using built-in defaults")
        return None, ''

    log(f"[LOCAL] Loaded team config from {path}: "
        f"{config.team_name} (#{config.team_number})")
    return config, path


# --------------------------------------------------------------------------- saved bits

#: The one comment line the saved-bits writer owns. Anything else above the block is
#: the team's own documentation and is never touched.
TOOLS_BLOCK_SENTINEL = ('# --- PenguinCAM saved bits: written by the Tools panel, '
                        'safe to edit by hand ---')


def config_is_writable() -> bool:
    """True when there is a local config file the app is allowed to rewrite.

    Saving a bit edits the team's config file, so it is offered only when there IS one
    and the filesystem will take a write. The hosted app's config comes from Onshape and
    is not ours to rewrite; there the UI hands over YAML to paste instead."""
    path = find_local_config_path()
    return bool(path) and os.access(path, os.W_OK)


def _yaml_quoted(value) -> str:
    """A double-quoted YAML scalar. Diameters are the reason this exists: 1/4" ends in
    the quote character, and pasting it raw produced `diameter: "1/4""`, which is not
    YAML at all."""
    text = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{text}"'


def _render_tools_block(tools) -> str:
    """The `tools:` block, written the way a person would write it.

    Hand-rolled rather than yaml.safe_dump'd so the block reads like the rest of the
    file (block sequences, quoted names, no !!python tags, keys in a fixed order) and so
    a diff of the config after saving a bit is one readable hunk."""
    lines = [TOOLS_BLOCK_SENTINEL, 'tools:']
    if not tools:
        lines.append('  []')
        return '\n'.join(lines) + '\n'
    for tool in tools:
        diameter = tool.get('diameter_text') or tool.get('diameter')
        lines.append(f'  - name: {_yaml_quoted(tool.get("name", ""))}')
        lines.append(f'    diameter: {_yaml_quoted(diameter)}')
        lines.append(f'    flutes: {int(tool.get("flutes", 1))}')
        lines.append(f'    type: {tool.get("type", "endmill")}')
        if tool.get('type') == 'vbit' or tool.get('included_angle') is not None:
            angle = tool.get('included_angle')
            if angle is not None:
                lines.append(f'    included_angle: {float(angle):g}')
    return '\n'.join(lines) + '\n'


def _strip_tools_block(text: str) -> str:
    """Remove an existing top-level `tools:` block from the config text, leaving
    everything else byte-for-byte.

    Only ONE comment line is ever taken with it: the sentinel this module writes. The
    first version of this swallowed every comment line above `tools:`, which on a config
    whose saved-bits section is preceded by a page of hand-written documentation meant
    the second save deleted the documentation. A managed block gets to own exactly the
    line it wrote."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(r'^tools\s*:', line):
            start = i
            break
    if start is None:
        return text
    head = start
    if head > 0 and lines[head - 1].rstrip('\n') == TOOLS_BLOCK_SENTINEL:
        head -= 1
        # Blank lines between the previous content and our own block are ours too, so
        # repeated saves do not push the file down one line at a time.
        while head > 0 and not lines[head - 1].strip():
            head -= 1
    end = start + 1
    while end < len(lines):
        line = lines[end]
        # The block ends at the next line that starts in column 0 and is not a comment,
        # a blank, or part of the sequence.
        if line.strip() and not line[0].isspace() and not line.lstrip().startswith('#'):
            break
        end += 1
    # Trailing blank lines after the block belong to it too, so repeated saves do not
    # accumulate empty lines.
    while end < len(lines) and not lines[end].strip():
        end += 1
    return ''.join(lines[:head] + lines[end:])


def save_tools_to_config(tools):
    """Write the saved-bit list into the local team config file.

    Everything except the `tools:` block is preserved character for character - the
    config is full of hand-written comments that took someone an afternoon and a
    yaml.safe_dump round trip would delete every one of them.

    The result is re-parsed before it replaces the file, and the parse is compared with
    the original: if ANY key other than `tools` moved, the write is refused. A tool list
    is not worth a damaged machine config.

    Returns (ok, message_or_path).
    """
    path = find_local_config_path()
    if not path:
        return False, 'No local team config file to save into.'
    if not os.access(path, os.W_OK):
        return False, f'{os.path.basename(path)} is read-only.'

    import yaml

    try:
        with open(path, 'r', encoding='utf-8') as fh:
            original = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return False, f'Could not read {os.path.basename(path)}: {exc}'

    try:
        before = yaml.safe_load(original) or {}
    except yaml.YAMLError as exc:
        return False, f'{os.path.basename(path)} is not valid YAML, so it was left alone: {exc}'

    body = _strip_tools_block(original)
    if body and not body.endswith('\n'):
        body += '\n'
    updated = body.rstrip('\n') + '\n\n' + _render_tools_block(tools)

    try:
        after = yaml.safe_load(updated) or {}
    except yaml.YAMLError as exc:
        return False, f'Refused to save: the edit would not parse ({exc})'

    before_rest = {k: v for k, v in before.items() if k != 'tools'}
    after_rest = {k: v for k, v in after.items() if k != 'tools'}
    if before_rest != after_rest:
        return False, ('Refused to save: rewriting the bit list would have changed the '
                       'rest of the config. Add the bits by hand instead.')

    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(updated)
        os.replace(tmp, path)     # atomic: a crash mid-write cannot truncate the config
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f'Could not write {os.path.basename(path)}: {exc}'
    return True, path
