"""The adapter of the package breadcrumbs_spacegraph: it gives the commands that draw to `python -m breadcrumbs`
(serve, open, viz, term, spacegraph). This layer gives no port: the reader that common needs is breadcrumbs/reader.py,
and the viewers build on it. ports.load() imports this module by the name of its package (breadcrumbs_*); no module
imports it.

Each function imports its module when it is called, not before: a command that draws nothing loads no viewer, and a
patched name of a module (a test) is read at the call. The part of spacegraph imports no module of a harness; it asks
the ports, as view.py does.
"""
import os
import sys

from breadcrumbs import ports


# -- the commands that draw ---------------------------------------------------------------------------------------
def _draws(run):
    """run for a command that draws: a profile that opted out of visualization (the port "blocked") gets one line."""
    def guarded(args, home):
        blocked = ports.get("blocked")
        why = blocked(os.path.dirname(os.path.abspath(home))) if blocked and not getattr(args, "url", None) else ""
        if why:
            print("breadcrumbs: %s" % why, file=sys.stderr)
            return 1
        return run(args, home)
    return guarded


def _add_serve(sub):
    p = sub.add_parser("serve", help="the explorer page on 127.0.0.1 (read-only)")
    p.add_argument("--port", type=int, default=8765); p.add_argument("--open", action="store_true")


def _serve(args, home):
    from .view import make_server, token
    server = make_server(home, port=args.port)
    url = "http://127.0.0.1:%d/#k=%s" % (server.server_address[1], token(home))
    print("breadcrumbs explorer (read-only):", url, flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _add_open(sub):
    p = sub.add_parser("open", help="the explorer in the default browser, or in a new Kitty or Ghostty window")
    p.add_argument("where", nargs="?", choices=("web", "kitty", "ghostty"), default="web")
    p.add_argument("--print", dest="show", action="store_true", help="show the command line that would run; run nothing")
    p.add_argument("--port", type=int, default=None, help="web: the port of the server (default 8765, else a free port)")


def _open(args, home):
    from . import launch
    return launch.run(args.where, home, port=args.port, show=args.show)


def _add_viz(sub):
    p = sub.add_parser("viz", help="open the explorer here: a cmux split, else Kitty/Ghostty, else the browser")
    p.add_argument("--port", type=int, default=8765)


def _viz(args, home):
    from . import viz
    return viz.run(home, port=args.port)


def _add_term(sub):
    p = sub.add_parser("term", help="the explorer page in Kitty or Ghostty (Kitty graphics protocol)")
    p.add_argument("--url", default=None, help="default: serve --home on a free port")
    p.add_argument("--every", type=float, default=10.0, help="seconds per frame")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=800)
    p.add_argument("--wait-ms", type=int, default=8000, help="page time for its API calls (Chrome virtual time)")
    p.add_argument("--frames", type=int, default=0, help="stop after N frames (0: until Ctrl-C)")


def _term(args, home):
    from . import term
    server = None
    if args.url is None:
        import threading
        from .view import make_server, token
        server = make_server(home, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        args.url = "http://127.0.0.1:%d/#k=%s&motion=0" % (server.server_address[1], token(home))  # one picture: the camera of before
    try:
        return term.run(args.url, every=args.every, width=args.width, height=args.height,
                        budget_ms=args.wait_ms, frames=args.frames)
    finally:
        if server:
            server.shutdown()
            server.server_close()


def _add_spacegraph(sub):
    p = sub.add_parser("spacegraph", help="native SpaceGraph viewer in Kitty/Ghostty (no Chrome)")
    p.add_argument("--port", type=int, default=8765, help="port for the view API server"); p.add_argument("--motion", choices=("0", "1"), default=None, help="0: the camera of the old page (no momentum); or BREADCRUMBS_MOTION=0")


def _spacegraph(args, home):
    from .spacegraph_kitty import SpaceGraphKitty
    app = SpaceGraphKitty(home, port=args.port, motion=args.motion)
    app.run()
    return 0


if ports.layer("spacegraph", __file__):
    ports.command("serve", _add_serve, _draws(_serve), before="core")
    ports.command("open", _add_open, _draws(_open), before="core")
    ports.command("viz", _add_viz, _draws(_viz), before="core")
    ports.command("term", _add_term, _draws(_term), after="help")
    ports.command("spacegraph", _add_spacegraph, _draws(_spacegraph), after="help")
