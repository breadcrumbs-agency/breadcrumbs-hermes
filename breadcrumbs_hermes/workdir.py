"""workdir: where a chat works, and the memory of that place (breadcrumbs/scope.py).

Hermes gives a memory provider no working directory: initialize gets the profile's home and nothing about the folder
of the chat. So the folder is read the way Hermes reads it: the cwd of the session (agent.runtime_cwd, where this
Hermes has it), else TERMINAL_CWD, else the folder of the process. Its project is its git repository, its
`.breadcrumbs` folder or its source tree and, with none of them, the folder itself (scope.py); that project owns the
store that the provider opens. A chat in another repository or folder has another memory. The home folder is the
exception: its project is the profile's default memory. A `.breadcrumbs` file that says `off` makes a place use the
default memory too.

    /scope              where this chat is, its memory, the scopes above it and the scopes that exist
    /scope up           the same, and how to read further than this scope (crumb_find scope "up" or "all")
    /viz                the explorer of the memory of this chat's scope (read-only, on this machine)

Off: BREADCRUMBS_SCOPE=off, or "scope": "off" in config.json. Then every chat uses the profile's default memory.

Not profiles: nothing here creates, clones or switches a Hermes profile, starts a bot, or edits Hermes's config.
"""
import io
import logging
import os
from typing import List, Optional, Tuple

from breadcrumbs import scope

LOCAL_SURFACES = ("cli", "tui", "desktop", "acp", "local")
VIZ_HELP = "open the explorer of the memory of this chat's scope (the map, graph, Gantt and Venn; read-only)"
VIZ_DISCLAIMER = ("`/viz` is not for messaging chats (%s). It opens a page on the machine that runs Hermes. "
                  "Use it in a local session.")
SCOPE_HELP = "where this chat's memory is: its git repository or folder, the scopes above it, the scopes that exist"
OFF = ("off", "0", "false", "no")


# -- the Hermes folders (read-only; no profile is made or changed here) ---------------------------------------
def hermes_root() -> str:
    """The Hermes root (~/.hermes, or a custom root): its profiles are under <root>/profiles."""
    try:
        from hermes_constants import get_default_hermes_root

        return str(get_default_hermes_root())
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".hermes")


def current_home() -> str:
    """The home of the profile that runs the command or the hook (a gateway keeps it in a context variable)."""
    from . import ace

    return ace._home()


def profile_of(home_dir: str, hermes: Optional[str] = None) -> str:
    hermes = hermes or hermes_root()
    home_dir = os.path.abspath(home_dir)
    return os.path.basename(home_dir) if os.path.dirname(home_dir) == os.path.join(hermes, "profiles") else "default"


def profiles(hermes: Optional[str] = None) -> List[Tuple[str, str]]:
    """(name, home) of the default profile, then of each folder in <root>/profiles, by name."""
    hermes = hermes or hermes_root()
    out = [("default", hermes)]
    base = os.path.join(hermes, "profiles")
    try:
        out += [(n, os.path.join(base, n)) for n in sorted(os.listdir(base))
                if not n.startswith(".") and os.path.isdir(os.path.join(base, n))]
    except OSError:
        pass
    return out


# -- the folder of a chat and its scope --------------------------------------------------------------------
def chat_cwd(kwargs: Optional[dict] = None) -> str:
    """The folder the chat works in: a `cwd` that the caller gives, else Hermes's session cwd, else TERMINAL_CWD,
    else the folder of this process."""
    for key in ("cwd", "terminal_cwd", "workdir"):
        value = (kwargs or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        from agent.runtime_cwd import scope_terminal_cwd, scoped_session_cwd

        value = scoped_session_cwd() or scope_terminal_cwd()
        if value:
            return value
    except Exception:  # a Hermes without agent.runtime_cwd, or outside Hermes
        pass
    return os.environ.get("TERMINAL_CWD") or os.getcwd()


def enabled(cfg: Optional[dict] = None) -> bool:
    """BREADCRUMBS_SCOPE=off or "scope": "off" in config.json turns the scopes off."""
    env = os.environ.get("BREADCRUMBS_SCOPE")
    if env is not None and env.strip():
        return env.strip().lower() not in OFF
    return str((cfg or {}).get("scope", "auto")).strip().lower() not in OFF


def scope_chain(path: str) -> List[Tuple[str, str]]:
    """[(kind, root)] of the projects that hold `path`, the nearest first: explicit markers and git repositories, else
    the nearest source tree, else the folder itself. [] when a `.breadcrumbs` file says off."""
    return scope.chain(path)


def is_home(root: str) -> bool:
    return os.path.realpath(root) == os.path.realpath(scope.HOME)


def resolve(base: str, kwargs: Optional[dict] = None, cfg: Optional[dict] = None) -> dict:
    """The memory of a chat: {"cwd", "on", "kind", "root", "store", "up": [(kind, root)]}. `base` is the breadcrumbs
    folder of the profile's home. In the home folder, with the scopes off, or where a `.breadcrumbs` file says off, store
    is `base` itself (the default memory)."""
    cwd = chat_cwd(kwargs)
    info = {"cwd": cwd, "on": enabled(cfg), "kind": "", "root": "", "store": base, "up": []}
    if not info["on"]:
        return info
    found = scope_chain(cwd)
    if found:
        (kind, root), above = found[0], found[1:]
        if kind == scope.DIR and is_home(root):  # the home folder: the default memory, which already holds its past
            info.update(kind="home", root=root)
        else:
            info.update(kind=kind, root=root, store=scope.ensure(base, kind, root), up=above)
    return info


def stores_above(base: str, info: dict) -> List[Tuple[str, str, str]]:
    """[(label, root, store folder)] of the scopes above the chat's scope that have a store, then the default
    memory when the chat is in a scope: where scope "up" reads."""
    out = []
    for kind, root in info.get("up", ()):
        folder = scope.store_home(base, root)
        if os.path.isfile(os.path.join(folder, "index.db")):
            out.append((scope.short(root), root, folder))
    if info.get("root") and info.get("store") != base and os.path.isfile(os.path.join(base, "index.db")):
        out.append(("default", "", base))
    return out


def stores_all(base: str, info: dict) -> List[Tuple[str, str, str]]:
    """[(label, root, store folder)] of every other store of the profile that has an index: where scope "all" reads."""
    out = [(scope.short(s["root"]), s["root"], s["path"]) for s in scope.known(base)
           if s["present"] and os.path.realpath(s["path"]) != os.path.realpath(info.get("store", ""))]
    if os.path.isfile(os.path.join(base, "index.db")) and os.path.realpath(base) != os.path.realpath(info.get("store", "")):
        out.append(("default", "", base))
    return out


# -- /scope ------------------------------------------------------------------------------------------------------
def status(base: str, info: dict, more: bool = False) -> str:
    lines = ["breadcrumbs scope"]
    where = scope.short(info["cwd"])
    if not info["on"]:
        lines.append("  scopes are OFF (BREADCRUMBS_SCOPE or config.json \"scope\"): every chat uses the default memory")
    elif info["kind"] == "home":
        lines.append("  here    %s  (the home folder: the default memory of this profile)" % where)
    elif info["root"]:
        what = {"dir": "this folder: no git repository, .breadcrumbs entry or project file above it",
                "git": "git repository", "explicit": ".breadcrumbs folder", "source": "source tree"}.get(info["kind"], info["kind"])
        lines.append("  here    %s  (%s%s)" % (where, what, "" if info["kind"] == "dir" else ": " + scope.short(info["root"])))
    else:
        lines.append("  here    %s  (a .breadcrumbs file here says off: the default memory)" % where)
    lines.append("  memory  %s%s" % (scope.short(info["store"]), "" if os.path.isfile(os.path.join(
        info["store"], "index.db")) else "  (empty: nothing is stored yet)"))
    for kind, root in info.get("up", ()):
        lines.append("  above   %s  (%s)" % (scope.short(root), kind))
    others = stores_all(base, info)
    if others:
        lines.append("  scopes with a memory:")
        lines += ["    %s" % label for label, _, _ in others]
    if more:
        lines.append("  Recall in this chat uses the memory above only. To read further, crumb_find takes "
                     "scope \"up\" (the scopes above, then the default memory) or \"all\" (every scope of this profile).")
        lines.append("  A folder with no git repository is its own project. To make a folder the project root, create a file or "
                     "folder named .breadcrumbs in it; a .breadcrumbs file that says off uses the default memory there.")
    return "\n".join(lines)


def command(base: str, info: dict, raw_args: str = "") -> str:
    arg = (raw_args or "").strip().lower()
    if arg in ("", "status", "up", "list", "all"):
        return status(base, info, more=arg in ("up", "all"))
    return "usage: /scope [up]"


# -- /viz --------------------------------------------------------------------------------------------------------
def open_viz(store: str, session_id: str = "", hermes: Optional[str] = None) -> str:
    """/viz: the explorer of one store, opened on this machine (viz.run: a cmux split, else the default browser).
    The answer names the place and never the URL: the URL holds the view token. A chat whose profile or session is
    tagged no-vis opens nothing."""
    from breadcrumbs_spacegraph import viz

    from . import novis

    why = novis.blocked(current_home(), session_id, hermes)
    if why:
        return "breadcrumbs: the explorer did not open: %s." % why
    said = io.StringIO()
    try:
        viz.run(store, err=said, terminal=False)
    except Exception as e:  # a chat command answers; it never raises
        return "breadcrumbs: the explorer did not open: %s: %s" % (type(e).__name__, e)
    where = "a cmux split" if "cmux split" in said.getvalue() else "the browser"
    return "breadcrumbs: the explorer of %s is open in %s (read-only, on this machine)." % (scope.short(store), where)


# -- the entry point ---------------------------------------------------------------------------------------------
def register(ctx, provider=None):
    """/scope and /viz, each where the Hermes context has register_command. No hook, no CLI command, no profile."""
    if not hasattr(ctx, "register_command"):
        return

    def base_and_info():
        base = os.path.join(current_home(), "breadcrumbs")
        info = getattr(provider, "scope_info", None)
        from . import dials
        return base, info or resolve(base, None, dials.config(base))

    def scope_command(raw_args: str = "", session_id: str = "", platform: str = "") -> str:
        base, info = base_and_info()
        return command(base, info, raw_args)

    def viz_command(raw_args: str = "", session_id: str = "", platform: str = "") -> str:
        if platform and platform not in LOCAL_SURFACES:
            return VIZ_DISCLAIMER % platform
        _, info = base_and_info()
        return open_viz(info["store"], session_id)

    ctx.register_command("scope", scope_command, description=SCOPE_HELP, args_hint="[up]")
    ctx.register_command("viz", viz_command, description=VIZ_HELP)
    logging.getLogger(__name__).debug("breadcrumbs: /scope and /viz registered")
