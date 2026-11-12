"""Ports: what a higher layer gives to a lower layer, by name.

The code has these packages, one for each layer: `breadcrumbs` (common: the store, the reader, the verbs, the servers,
the command line), `breadcrumbs_spacegraph` (the viewers) and one for each harness (`breadcrumbs_hermes`,
`breadcrumbs_claude`, `breadcrumbs_codex`, `breadcrumbs_opencode`, `breadcrumbs_grok`). A package imports its own layer
and the layers below it, and no layer above it (breadcrumbs-common/tests/test_layers.py). A harness package imports no
other harness package.

Each package above common gives its part by ONE module, its adapter: `<package>/adapter.py`. load() finds the packages
by their NAME: each top-level package that starts with "breadcrumbs_", in sorted order. It imports `<package>.adapter`.
Common holds no list of them. An adapter calls these functions:

    layer(name, file)        the adapter announces its layer (and the folder of its package); False on a second call
    give(name, thing)        a layer gives a function (a port) under a name
    get(name)                a lower layer asks: the function, or None when no layer gives it
    need(name)               the same, but LookupError when no layer gives it
    command(name, add, run)  a layer gives a command of `python -m breadcrumbs`
    commands()               the command line asks for them
    harness(key, ...)        a harness gives its entry of the harness table (core.py: identify, `core setup`, `ingest`,
                             the usage of its requests, and its icon: the viewers draw it beside the brand)
    image(file, name)        the data: URI of an image file that lies beside `file` (an adapter gives its icon by it)
    mcp_add(cli)             the setup function of a program that adds a server by "<cli> mcp add NAME -- COMMAND..."
    harnesses()              core asks for the table
    folders(), pythonpath()  the folders that hold the packages, for a child process that must find the same code

The ports, each a function (the adapter of hermes gives them; the adapter of spacegraph gives none):

    neighbor()                 the weight of the neighbor smoothing of a search, for this process
    dial(root, key)            the value of the dial `key` for the store at root (its config.json, the environment)
    state(path, fn, connect)   fn(conn) on a read-only connection to the session log of the harness (a state.db)
    codebase(folder)           the codebase that holds a folder, or None
    profile(home)              the name of the profile whose home folder is `home`
    hide(home)                 a hide(sid, prefix=False) -> bool: the sessions of that profile that no view shows
    blocked(home)              why no view of that profile opens, or "" when one may
    price(model, provider)     (input, cache read, cache write) in USD per 1M tokens, or None (breadcrumbs/usage.py)
    ace_join(lines, requests)  [(ace.log line, the main request that it shaped)] (breadcrumbs/usage.py AFTER)

A layer that is not there gives nothing: get() is then None, and the caller answers as it does for a store that no
harness wrote. Nothing here names a layer: load() imports what it finds, at the first ask.
"""
import base64
import importlib
import mimetypes
import os
import pkgutil
import shlex

PREFIX = "breadcrumbs_"  # the start of the name of the package of a layer above common
ADAPTER = "adapter"  # the module of that package that gives its part: <package>/adapter.py

_given = {}  # name -> function
_commands = []  # in the order given
_harnesses = {}  # key -> {"label", "match", "setup", "order", "source", "usage", "icon"}
_layers = {}  # layer -> the folder that holds its package (on PYTHONPATH), in the order announced
_loaded = False


def layer(name, file):
    """An adapter announces its layer, with its own file. True the first time. False when the layer announced itself
    before: the same package then loaded twice under two names (the plugin folder of Hermes loads breadcrumbs_hermes as
    `<plugin>.breadcrumbs_hermes`), and the second adapter must register nothing again."""
    if name in _layers:
        return False
    _layers[name] = os.path.dirname(os.path.dirname(os.path.abspath(file)))
    return True


def give(name, thing):
    _given[name] = thing


def command(name, add, run, after=None, before=None, hidden=False):
    """Give a command to `python -m breadcrumbs`. add(sub) adds its parser to the subparsers; run(args, home) runs it
    and returns the exit code (home: the store folder). It lands directly after (or before) the command of common
    that `after` (or `before`) names, so the help keeps its order; commands with one anchor keep the order in which
    they were given. hidden: the usage line of the help leaves the name out."""
    _commands.append({"name": name, "add": add, "run": run, "after": after, "before": before, "hidden": hidden})


def harness(key, label, match, setup, order, source=None, usage=None, icon=None):
    """Give a harness to the table of core.py. match: the words of a client's self-name (MCP clientInfo.name, the A2A
    header) that name this harness, in lower case. setup(cmd) -> the text that adds the server command `cmd` (a list)
    to the harness. order: the place in the table. identify asks in this order and `core setup` prints in it, so the
    table does not depend on which adapter loads first. source(path) -> an iterator of the sessions of the harness
    program, each (id, title, meta, rows) as breadcrumbs/ingest.py session() takes them (path: where the program keeps
    them, or None for its own default); `ingest HARNESS [PATH]` reads it into the store. None: the harness has no
    source. usage(path) -> an iterator of the usage records of the model requests in the same place (breadcrumbs/usage.py
    record()); `ingest` adds them to usage.log of the store home. None: the harness gives no usage. icon: the mark of
    the program as a data: URI of an image (image(__file__, "icon.png")); the viewers draw it beside the brand, so a
    viewer says which harness it shows. None: the viewers draw the first letter of the label in its place."""
    _harnesses[key] = {"label": label, "match": tuple(match), "setup": setup, "order": order, "source": source,
                       "usage": usage, "icon": icon}


def image(file, name):
    """The data: URI of the image `name` in the folder of `file` (pass __file__), or None when it is not there or is
    not an image. An icon is small (a few KB): it travels in the answer of a viewer, so no server of files is needed."""
    path = os.path.join(os.path.dirname(os.path.abspath(file)), name)
    kind = mimetypes.guess_type(path)[0] or ""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not kind.startswith("image/") or not data:
        return None
    return "data:%s;base64,%s" % (kind, base64.b64encode(data).decode("ascii"))


def mcp_add(cli, name="breadcrumbs"):
    """The setup function of a program whose command is "<cli> mcp add NAME -- COMMAND...": setup(cmd) -> that line for
    the server command `cmd` (a list)."""
    return lambda cmd: "%s mcp add %s -- %s" % (cli, name, shlex.join(cmd))


def load():
    """Import the adapters once. A package with no adapter (common alone) has no ports, and that is no error."""
    global _loaded
    if not _loaded:  # the flag is set after the imports: a second thread waits on the import lock, then sees the ports
        for name in sorted({m.name for m in pkgutil.iter_modules() if m.ispkg and m.name.startswith(PREFIX)}):
            try:
                importlib.import_module(name + "." + ADAPTER)
            except ModuleNotFoundError as e:
                if e.name != name + "." + ADAPTER:  # a package of that name with no adapter gives nothing
                    raise
        _loaded = True


def get(name):
    load()
    return _given.get(name)


def need(name):
    thing = get(name)
    if thing is None:
        raise LookupError("breadcrumbs: no layer gives %r (see breadcrumbs/ports.py)" % name)
    return thing


def commands():
    load()
    return list(_commands)


def harnesses():
    """The harness table: {key: {"label", "match", "setup", "order", "source", "usage", "icon"}}, in the order of the entries."""
    load()
    return dict(sorted(_harnesses.items(), key=lambda kv: (kv[1]["order"], kv[0])))


def folders():
    """The folders that hold the packages of the layers that loaded (common first): what a child process needs on
    PYTHONPATH to find the same code."""
    load()
    own = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [own] + [f for f in _layers.values() if f != own]


def pythonpath(env=None):
    """The value of PYTHONPATH for a child process: folders(), then the entries of `env` (a PYTHONPATH value) that are
    not there yet."""
    paths = folders()
    return os.pathsep.join(paths + [p for p in (env or "").split(os.pathsep) if p and p not in paths])
