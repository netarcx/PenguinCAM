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
