"""`viz`: open the explorer where the user is. One word, no options to know.

The explorer (the page and its read-only server) is a viewer of breadcrumbs-spacegraph. This module only finds where the
page opens, and it asks the layers above common for the parts that draw, through two ports:

    explorer(home, port, harness)  the URL of the explorer of this memory; its server is started, or reused
    term(url)                      the page drawn in this terminal (Kitty, Ghostty); None when the terminal is no such one
    term_argv(home, harness)      the command line of `term` for this memory (a tmux pane runs it)

In cmux the page opens as a browser split beside the terminal. In Kitty, Ghostty or WezTerm it draws in the terminal
(term). Anywhere else it opens the default browser. The server is shared: a second `viz` reuses it.

terminal_of(env) names the terminal the process runs in (cmux, tmux, zellij, screen, wezterm, kitty, ghostty, iterm2,
apple-terminal, vscode, warp, alacritty, windows-terminal, konsole, hyper, or "" for none we know). split_kind(env) is the
part of it that can open a pane beside the chat: cmux (a browser split), tmux, WezTerm and Kitty (a pane that runs `term`).
The others have no split command: the page opens in the OS browser, or `term` draws in the terminal that reads the Kitty
graphics protocol. `viz --where` prints what was found and what `viz` would do here.

autostart: a Hermes chat that starts in a terminal gets the explorer once (a split where there is one; a bare console:
the OS opens it). The Hermes part calls it at session start. BREADCRUMBS_VIZ=0 turns the autostart off.
"""
import os
import shlex
import shutil
import socket
import subprocess
import sys
import webbrowser

from . import ports

PORT = 8765
OFF = ("0", "off", "no", "false")


def _explorer():
    explorer = ports.get("explorer")
    if explorer is None:
        raise LookupError("the explorer is not installed (its viewers are breadcrumbs-spacegraph)")
    return explorer


def _term():
    return ports.get("term")


def _term_argv():
    return ports.get("term_argv")


def listening(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


# The name of a terminal, from the environment it leaves. The first match wins: a multiplexer or cmux comes before the
# emulator it runs in (cmux is a Ghostty window, tmux runs in anything).
def _has(*names):
    return lambda env: any(env.get(n) for n in names)


def _prog(*names):
    return lambda env: env.get("TERM_PROGRAM", "").lower() in names


def _term_is(*names):
    return lambda env: any(n in env.get("TERM", "").lower() for n in names)


TERMINALS = (
    ("cmux", _has("CMUX_SURFACE_ID", "CMUX_WORKSPACE_ID")),
    ("tmux", _has("TMUX")),
    ("zellij", _has("ZELLIJ", "ZELLIJ_SESSION_NAME")),
    ("screen", _has("STY")),
    ("wezterm", lambda env: bool(env.get("WEZTERM_PANE")) or _prog("wezterm")(env)),
    ("kitty", lambda env: bool(env.get("KITTY_WINDOW_ID")) or _term_is("xterm-kitty")(env)),
    ("ghostty", lambda env: bool(env.get("GHOSTTY_RESOURCES_DIR")) or _prog("ghostty")(env) or _term_is("ghostty")(env)),
    ("iterm2", lambda env: bool(env.get("ITERM_SESSION_ID")) or _prog("iterm.app")(env)),
    ("apple-terminal", _prog("apple_terminal")),
    ("vscode", _prog("vscode")),
    ("warp", lambda env: _prog("warpterminal")(env) or bool(env.get("WARP_IS_LOCAL_SHELL_SESSION"))),
    ("alacritty", lambda env: bool(env.get("ALACRITTY_WINDOW_ID")) or _term_is("alacritty")(env)),
    ("windows-terminal", _has("WT_SESSION")),
    ("konsole", _has("KONSOLE_VERSION")),
    ("hyper", _prog("hyper")),
)
# Terminals that read the Kitty graphics protocol (what `term` draws with). cmux is a Ghostty window.
GRAPHICS = ("cmux", "kitty", "ghostty", "wezterm")
SPLITS = ("cmux", "tmux", "wezterm", "kitty")
SPLIT_TOOL = {"cmux": "cmux", "tmux": "tmux", "wezterm": "wezterm", "kitty": "kitten"}  # the program on the PATH that splits


def terminal_of(env=os.environ):
    """The terminal this process runs in, by name, or "" when it is none we know (a gateway, cron, a plain pipe)."""
    for name, test in TERMINALS:
        if test(env):
            return name
    return ""


def split_kind(env=os.environ):
    """The terminal that can open a pane beside the chat: "cmux", "tmux", "wezterm" or "kitty"; else None (a bare
    console, or a terminal with no split command, such as Ghostty, iTerm2, Zellij or VS Code)."""
    name = terminal_of(env)
    return name if name in SPLITS else None


def split_argv(kind, env, command):
    """The command line that opens a pane to the right of this one and runs `command` (a list) in it. tmux, WezTerm
    and Kitty only: cmux opens a browser (see autostart). The pane is not focused, so the chat keeps the keyboard."""
    if kind == "tmux":
        return ["tmux", "split-window", "-h", "-d"] + (["-t", env["TMUX_PANE"]] if env.get("TMUX_PANE") else []) + [shlex.join(command)]
    if kind == "wezterm":
        return ["wezterm", "cli", "split-pane", "--right"] + (["--pane-id", env["WEZTERM_PANE"]] if env.get("WEZTERM_PANE") else []) + ["--"] + list(command)
    if kind == "kitty":  # needs allow_remote_control in kitty.conf; a refusal falls back like any other
        return ["kitten", "@"] + (["--to", env["KITTY_LISTEN_ON"]] if env.get("KITTY_LISTEN_ON") else []) + [
            "launch", "--type=window", "--location=vsplit", "--keep-focus"] + list(command)
    raise ValueError("no split command for %r" % kind)


def where(env=os.environ, which=shutil.which, platform=sys.platform):
    """What `viz` and the autostart find here: [(key, value)] lines for `viz --where`. Reads the environment and the PATH
    only: nothing opens, no server starts."""
    name = terminal_of(env)
    kind = split_kind(env)
    tool = SPLIT_TOOL.get(kind)
    have = bool(tool and which(tool))
    if env.get("BREADCRUMBS_VIZ", "").lower() in OFF:
        does = "nothing at session start (BREADCRUMBS_VIZ is off); `viz` still opens it"
    elif kind == "cmux" and have:
        does = "a browser split of cmux"
    elif kind in ("tmux", "wezterm", "kitty") and have:
        does = "a %s pane that runs `term` (it draws with the Kitty graphics protocol)" % kind
    elif can_launch(env, platform):
        does = "the page in the OS browser" + (" (no `%s` on the PATH for a split)" % tool if kind and not have else "")
    else:
        does = "nothing: no split, and no display to open a page"
    return [
        ("terminal", name or "unknown (no terminal markers in the environment)"),
        ("multiplexer", name if name in ("cmux", "tmux", "zellij", "screen") else "none"),
        ("split", ("%s%s" % (kind, "" if have else " (`%s` is not on the PATH)" % tool)) if kind else "none"),
        ("kitty graphics", "yes" if name in GRAPHICS else "no"),
        ("autostart", does),
    ]


def can_launch(env=os.environ, platform=sys.platform):
    """True when the OS can open a page: macOS and Windows always; Linux only with a display (else Python's
    webbrowser would start a text browser on the chat's own terminal)."""
    return platform in ("darwin", "win32") or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def autostart(home, env=os.environ, err=None, harness=None):
    """Hermes starts in a terminal: where the terminal can split (cmux: a browser split; tmux, WezTerm, Kitty: a pane
    that runs `term`), split a window for the explorer; elsewhere, ask the OS to open the page. Returns the terminal
    that split ("cmux", "tmux", "wezterm", "kitty"), "os", or None. A split that is refused falls back to the OS.
    harness: the program that starts it (the icon beside the brand)."""
    err = err or sys.stderr
    if env.get("BREADCRUMBS_VIZ", "").lower() in OFF:
        return None
    kind = split_kind(env)
    argv_of = _term_argv()
    if kind in ("tmux", "wezterm", "kitty") and argv_of and shutil.which(SPLIT_TOOL[kind]):
        r = subprocess.run(split_argv(kind, env, argv_of(home, harness)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            return kind
        print("breadcrumbs viz: %s could not split: %s" % (kind, r.stderr.decode("utf-8", "replace").strip()), file=err)
    url = _explorer()(home, PORT, harness)
    if kind == "cmux" and shutil.which("cmux"):
        r = subprocess.run(["cmux", "browser", "open", url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            return "cmux"
        print("breadcrumbs viz: cmux could not open it: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    if can_launch(env) and webbrowser.open(url):
        return "os"
    return None


def run(home, port=PORT, err=None, terminal=True, harness=None, env=os.environ):
    """terminal=False: the caller is not the user's shell (the /viz command of a chat), so the terminal is not a choice.
    harness: the harness that asks (the explorer may name it). cmux takes the page when this process runs in a cmux
    surface, or in no terminal we know (a gateway started from cmux has none of its markers); in any other terminal the
    cmux app is not the user's window, so it is not asked."""
    err = err or sys.stderr
    url = _explorer()(home, port, harness)
    cmux = shutil.which("cmux") if terminal_of(env) in ("cmux", "") else None
    if cmux:
        r = subprocess.run([cmux, "browser", "open", url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            print("breadcrumbs viz: opened in a cmux split", file=err)
            return 0
        print("breadcrumbs viz: cmux could not open it: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    term = _term()
    if terminal and sys.stdout.isatty() and term:
        rc = term(url)
        if rc is not None:
            return rc
    webbrowser.open(url)
    print("breadcrumbs viz: opened in the browser: " + url, file=err)
    return 0


def command(args, home):
    """`python -m breadcrumbs viz`: a profile that opted out of visualization (the port "blocked") gets one line."""
    blocked = ports.get("blocked")
    why = blocked(os.path.dirname(os.path.abspath(home))) if blocked else ""
    if why:
        print("breadcrumbs: %s" % why, file=sys.stderr)
        return 1
    if getattr(args, "where", False):
        for key, value in where():
            print("%-15s %s" % (key, value))
        return 0
    try:
        return run(home, port=args.port)
    except LookupError as e:
        print("breadcrumbs viz: %s" % e, file=sys.stderr)
        return 1
