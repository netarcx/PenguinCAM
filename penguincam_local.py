#!/usr/bin/env python
"""Launch PenguinCAM as a local desktop app - no Onshape, no cloud, no sign-in.

    uv run python penguincam_local.py

Starts the same Flask app the hosted site runs, but with the Onshape OAuth gate lifted
(see local_mode.py), bound to the loopback interface, and opens a browser at it. Parts
come from DXF files on disk; G-code is downloaded straight back to disk.

    --port N        listen on a specific port (default: 6238, or the next free one)
    --config PATH   use this PenguinCAM-config.yaml instead of the built-in defaults
                    (by default a PenguinCAM-config.yaml in the working directory is
                    picked up automatically)
    --host HOST     bind address; defaults to 127.0.0.1 and should stay there unless you
                    intend to expose the app, which local mode does not authenticate
    --no-browser    don't open a browser window

Binding to loopback is what makes lifting the auth gate safe: only someone at this
keyboard can reach the app. Passing --host 0.0.0.0 puts an unauthenticated PenguinCAM on
the network, so the launcher says so out loud before it does that.
"""

import argparse
import os
import socket
import sys
import threading
import webbrowser

DEFAULT_PORT = 6238


def port_is_free(host: str, port: int) -> bool:
    """Whether nothing is listening on this port.

    Tested by CONNECTING, not by binding. Binding with SO_REUSEADDR - and Werkzeug sets it
    too - succeeds on Windows even while another socket is actively listening, so the
    bind test reported "free" for the exact case it existed to catch: a PenguinCAM still
    running from earlier. Two servers then held the same port, every request went to the
    first, and the second silently did nothing with the config you just passed it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.35)
        if probe.connect_ex(('127.0.0.1' if host == '0.0.0.0' else host, port)) == 0:
            return False        # something answered
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))    # no SO_REUSEADDR: we want the honest answer
            return True
        except OSError:
            return False


def find_free_port(host: str, preferred: int, attempts: int = 20) -> int:
    """First free port at or after `preferred`. A shop laptop often has something else on
    6238 already (a previous run that didn't exit cleanly, most often), and failing to
    start with an address-in-use traceback is a poor greeting."""
    for port in range(preferred, preferred + attempts):
        if port_is_free(host, port):
            return port
    raise SystemExit(f"No free port between {preferred} and {preferred + attempts - 1}. "
                     f"Pass --port to choose one yourself.")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Run PenguinCAM locally, without Onshape or any cloud service.')
    parser.add_argument('--port', type=int, default=None,
                        help='Port to listen on (default: 6238, or the next free one)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Bind address (default: 127.0.0.1 - loopback only)')
    parser.add_argument('--config', default=None,
                        help='Path to a PenguinCAM-config.yaml with your team settings')
    parser.add_argument('--no-browser', action='store_true', help="Don't open a browser")
    parser.add_argument('--debug', action='store_true',
                        help='Run Flask in debug mode with the auto-reloader')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.config:
        if not os.path.isfile(args.config):
            print(f"ERROR: config file not found: {args.config}")
            return 1
        os.environ['PENGUINCAM_CONFIG'] = os.path.abspath(args.config)

    # Set before importing the app: the app reads the flag once at import time so every
    # gate in the process agrees about which mode it is in.
    os.environ['PENGUINCAM_LOCAL'] = '1'
    os.environ.setdefault('FLASK_SECRET_KEY', 'penguincam-local-development-key')
    # A fixed secret key would otherwise switch the app into cross-site cookie mode
    # (SameSite=None; Secure), which is for embedding in the Onshape iframe over HTTPS.
    # Local mode serves plain http:// and embeds in nothing, and a Secure cookie there is
    # at best browser-dependent - so keep the default Lax cookie and a session that works.
    os.environ.setdefault('EMBED_COOKIES', '0')

    from frc_cam_gui_app import app  # noqa: E402  (import order is deliberate)
    import local_mode

    # `--port N` means N, and nothing else. Comparing against the default value as a
    # sentinel meant an explicit `--port 6238` was indistinguishable from no flag at all
    # and got silently relocated to 6239.
    if args.port is not None:
        if not port_is_free(args.host, args.port):
            print(f"ERROR: port {args.port} is already in use. Stop whatever is on it, "
                  f"or omit --port to take the next free one.")
            return 1
        port = args.port
    else:
        port = find_free_port(args.host, DEFAULT_PORT)
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{port}/"

    # Actually load it, rather than just reporting the path we found: a malformed or
    # non-UTF-8 file still HAS a path, and printing it implied a config was in effect
    # while the app quietly ran on defaults.
    loaded_config, config_path = local_mode.load_local_team_config()

    print("=" * 70)
    print("PenguinCAM - local mode")
    print("=" * 70)
    if loaded_config is not None:
        print(f"  Team config : {config_path}")
        print(f"                {loaded_config.team_name} (#{loaded_config.team_number})")
    elif config_path:
        print(f"  Team config : {config_path}")
        print(f"                ** NOT LOADED - see the message above. Running on "
              f"built-in defaults. **")
    else:
        print(f"  Team config : built-in defaults (no PenguinCAM-config.yaml found)")
    print(f"  Onshape     : not used - add parts by dropping DXF files on the page")
    print(f"  Address     : {url}")
    if args.host not in ('127.0.0.1', 'localhost'):
        print("  ** WARNING: local mode has no sign-in. Binding to "
              f"{args.host} exposes it to everyone on your network. **")
        if args.debug:
            print("  ** WARNING: --debug also exposes the Werkzeug debugger, which "
                  "allows ARBITRARY CODE EXECUTION by anyone who can reach this port. **")
    print("\n  Press Ctrl+C to stop.\n")
    print("=" * 70)

    if not args.no_browser:
        # Fired on a timer rather than before app.run(), so the browser opens against a
        # server that is already listening instead of racing it to a connection refused.
        opener = threading.Timer(1.0, lambda: webbrowser.open(url))
        opener.daemon = True     # Ctrl+C in the first second should not still open a tab
        opener.start()

    try:
        app.run(host=args.host, port=port, debug=args.debug, use_reloader=args.debug)
    except KeyboardInterrupt:      # pragma: no cover - Werkzeug usually absorbs this
        pass
    # Printed unconditionally: Werkzeug's serve_forever swallows the KeyboardInterrupt,
    # so the handler above almost never fires and the goodbye never appeared.
    print("\nPenguinCAM stopped.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
