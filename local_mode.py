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
    """A double-quoted YAML scalar.

    Diameters are why it exists: 1/4" ends in the quote character, and pasting it raw
    produced `diameter: "1/4""`, which is not YAML at all. Control characters matter for
    the same reason and are worse: a newline inside a saved bit's name put a line at
    column 0 in the middle of the block, which then stopped _strip_tools_block early, so
    every later save AND delete failed - a tools panel bricked until someone edited the
    YAML by hand."""
    text = (str(value).replace('\\', '\\\\').replace('"', '\\"')
            .replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t'))
    # Any other control character would also end the scalar (or make an unreadable
    # file); YAML's \xNN escape keeps them inside the quotes where they belong.
    text = ''.join(ch if ch >= ' ' and ch != '\x7f' else '\\x%02x' % ord(ch)
                   for ch in text)
    return f'"{text}"'


def _yaml_scalar(value) -> str:
    """A YAML scalar for a value of unknown type. Numbers and booleans stay bare;
    anything else is quoted, and anything structured is dumped in flow style so a
    hand-written mapping or list on a bit survives a rewrite."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f'{value:g}' if value == value and value not in (float('inf'), float('-inf')) \
               else _yaml_quoted(value)
    if value is None:
        return 'null'
    if isinstance(value, (dict, list, tuple)):
        import yaml as _yaml
        return _yaml.safe_dump(value, default_flow_style=True).strip().rstrip('...').strip()
    return _yaml_quoted(value)


def _render_tools_block(tools) -> str:
    """The `tools:` block, written the way a person would write it.

    Hand-rolled rather than yaml.safe_dump'd so the block reads like the rest of the
    file (block sequences, quoted names, no !!python tags, keys in a fixed order) and so
    a diff of the config after saving a bit is one readable hunk."""
    lines = [TOOLS_BLOCK_SENTINEL, 'tools:']
    if not tools:
        lines.append('  []')
        return '\n'.join(lines) + '\n'
    known = ('name', 'diameter', 'flutes', 'type', 'included_angle')
    for tool in tools:
        diameter = tool.get('diameter_text') or tool.get('diameter')
        lines.append(f'  - name: {_yaml_quoted(tool.get("name", ""))}')
        lines.append(f'    diameter: {_yaml_quoted(diameter)}')
        lines.append(f'    flutes: {_yaml_scalar(tool.get("flutes", 1))}')
        lines.append(f'    type: {_yaml_quoted(tool.get("type", "endmill"))}')
        angle = tool.get('included_angle')
        if angle is not None:
            lines.append(f'    included_angle: {_yaml_scalar(angle)}')
        # Anything else the team wrote on this bit by hand - a vendor part number, a
        # stickout someone measured, a note that it is chipped. The app does not use
        # these, which is exactly why it must not eat them: rewriting the block from
        # the fields we happen to understand silently deleted them.
        for key in sorted(k for k in tool if k not in known and k not in
                          ('id', 'source', 'diameter_text')):
            lines.append(f'    {key}: {_yaml_scalar(tool[key])}')
    return '\n'.join(lines) + '\n'


def _tools_block_span(text: str):
    """Where an existing top-level `tools:` block starts and ends, or None.

    The block is its `tools:` line, the sentinel comment directly above it if this
    module wrote one, and every indented or `- ` line that follows. It stops at the
    first line in column 0 that is not part of the sequence - INCLUDING a comment,
    because a comment there is the team's, not ours. Two versions of this got that
    wrong in opposite directions: the first swallowed the documentation above the
    block, the second swallowed the notes below it.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(r'^(\ufeff)?["\']?tools["\']?\s*:', line):
            start = i
            break
    if start is None:
        return None

    head = start
    if head > 0 and lines[head - 1].rstrip('\n') == TOOLS_BLOCK_SENTINEL:
        head -= 1

    def belongs(line):
        # An indented line, or a sequence entry written at column 0 (`- name: ...`),
        # which is ordinary YAML style and used to orphan the sequence when only the
        # `tools:` line was removed.
        return bool(line) and (line[0].isspace() or line.lstrip().startswith('- '))

    end = start + 1
    while end < len(lines):
        if not lines[end].strip():
            look = end
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look < len(lines) and belongs(lines[look]):
                end = look
                continue
            break
        if belongs(lines[end]):
            end += 1
            continue
        break
    return head, end, lines


def _replace_tools_block(text: str, block: str) -> str:
    """Put `block` where the existing `tools:` block is, or append it if there is none.

    Replacing in place rather than stripping and appending keeps the block where the
    team put it - moving it to the end of the file is both a surprising diff and how
    the notes underneath it came to be deleted.
    """
    span = _tools_block_span(text)
    if span is None:
        body = text.rstrip('\n') + '\n' if text.strip() else ''
        return body + '\n' + block
    head, end, lines = span
    return ''.join(lines[:head]) + block + ''.join(lines[end:])


def read_raw_tools():
    """The `tools:` list exactly as the config file has it, entries and all.

    Callers edit THIS and hand it back, so an entry the app cannot parse - a typo'd
    key, an unknown tool type, a bit someone is still filling in - survives a save that
    had nothing to do with it. Rebuilding the block from the app's own validated view
    quietly deleted every one of those, and it was the next unrelated save that did it.
    Returns [] when there is no readable list.
    """
    path = find_local_config_path()
    if not path:
        return []
    import yaml
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh.read())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    raw = (data or {}).get('tools') if isinstance(data, dict) else None
    return [dict(e) for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


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
        # newline='' keeps the file's own line endings visible instead of translating
        # them to \n, so the rewrite can put back what was there.
        with open(path, 'r', encoding='utf-8', newline='') as fh:
            original_bytes_newline = fh.read()
        original = original_bytes_newline.replace('\r\n', '\n')
    except (OSError, UnicodeDecodeError) as exc:
        return False, f'Could not read {os.path.basename(path)}: {exc}'

    try:
        before = yaml.safe_load(original) or {}
    except yaml.YAMLError as exc:
        return False, f'{os.path.basename(path)} is not valid YAML, so it was left alone: {exc}'

    updated = _replace_tools_block(original, _render_tools_block(tools))

    try:
        after = yaml.safe_load(updated) or {}
    except yaml.YAMLError as exc:
        return False, f'Refused to save: the edit would not parse ({exc})'

    before_rest = {k: v for k, v in before.items() if k != 'tools'}
    after_rest = {k: v for k, v in after.items() if k != 'tools'}
    if before_rest != after_rest:
        return False, ('Refused to save: rewriting the bit list would have changed the '
                       'rest of the config. Add the bits by hand instead.')

    # Write the file the way we found it. A config edited on Windows has CRLF line
    # endings and a config someone chmod'd 600 is 600 on purpose; rewriting either is a
    # change the team did not ask for and did not see. And follow a symlink rather than
    # replacing it - os.replace on the link would leave the real file frozen with the
    # old contents while the app cheerfully reported success.
    target = os.path.realpath(path)
    newline = '\r\n' if '\r\n' in original_bytes_newline else '\n'
    try:
        mode = os.stat(target).st_mode & 0o777
    except OSError:
        mode = None

    tmp = target + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline=newline) as fh:
            fh.write(updated)
            fh.flush()
            os.fsync(fh.fileno())   # the rename is atomic; the CONTENT reaching disk is not
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)   # atomic: a crash mid-write cannot truncate the config
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f'Could not write {os.path.basename(path)}: {exc}'
    return True, path
