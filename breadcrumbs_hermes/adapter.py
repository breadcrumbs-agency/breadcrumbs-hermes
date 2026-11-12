"""The adapter of the package breadcrumbs_hermes: what the Hermes plugin gives to the layers under it. ports.load()
imports this module by the name of its package (breadcrumbs_*); no module imports it. The plugin entry of Hermes (the
__init__.py of the plugin folder) imports it too, as `<plugin>.breadcrumbs_hermes.adapter`: ports.layer() makes the
registration happen once, whichever import comes first.

    ports      neighbor dial state codebase profile hide blocked  (for the viewers, the hive and the searches)
               price  (the rates of a model, for the cache metrics: breadcrumbs/usage.py)
               ace_join  (each ace.log line with the request that it shaped: ace.join)
    commands   ace dials ingest-hermes sources seats seat no-vis  (island, seats, dials, ace, the hide list)
               slot install-hermes  (the installers of the plugin into a Hermes profile)
    harness    hermes  (the entry of the harness table of core.py, with the source of its sessions for `ingest`)

Each function imports its module when it is called, not before: a command loads only the modules that it needs, and a
patched name of a module (a test) is read at the call.
"""
import json
import os
import subprocess
import sys
import time

from breadcrumbs import ports

SHIM = '''r"""breadcrumbs memory provider (plugin shim, written by `python -m breadcrumbs install-hermes`).

Source: {repo}
"""
import os
import sys

_REPO = {repo!r}  # the folders of the packages (breadcrumbs, breadcrumbs_spacegraph, breadcrumbs_hermes), joined by os.pathsep
_NAME = {name!r}
_DEV = {dev!r}  # True: import the repo code fresh at every agent build (long-lived gateways)


def _provider_class():
    for folder in reversed(_REPO.split(os.pathsep)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    if _DEV:
        for mod in [m for m in list(sys.modules) if m == "breadcrumbs" or m.startswith(("breadcrumbs.", "breadcrumbs_"))]:
            sys.modules.pop(mod, None)
    from breadcrumbs_hermes.hermes import BreadcrumbsProvider
    return BreadcrumbsProvider


def register(ctx):
    # Hermes finds a plugin's memory provider and context engine by these two calls in this file's text
    provider = _provider_class()(name=_NAME)  # first: with _DEV it reloads the package
    ctx.register_memory_provider(provider)
    from breadcrumbs_hermes import ace
    from breadcrumbs_hermes.hermes import register_plugin  # the handbook, the intercept and the islands
    register_plugin(ctx, provider)
    if hasattr(ctx, "register_context_engine") and ace.wanted(name=_NAME):
        ctx.register_context_engine(ace.Ace(name=_NAME))
'''

PLUGIN_YAML = """name: {name}
version: 0.0.1
description: Content-addressed breadcrumb memory with exact provenance and zoom levels 0..1.
"""


def hermes_config(profile, *args):
    """`hermes -p PROFILE config ARGS...`: the last line of its output (Hermes's own notices dropped)."""
    p = subprocess.run(["hermes", "-p", profile or "default", "config", *args], capture_output=True, text=True,
                       timeout=120)
    if p.returncode:
        if args[:1] == ("get",) and "not set" in (p.stderr or "") + (p.stdout or ""):
            return ""  # Hermes exits 1 on a key that is not set: that is no value, not a failure
        raise SystemExit("breadcrumbs: hermes config %s failed: %s" % (" ".join(args), (p.stderr or p.stdout)[-300:]))
    lines = [line.strip() for line in p.stdout.splitlines() if line.strip() and not line.startswith("hermes:")]
    return lines[-1] if lines else ""


NO_PROVIDER = {"", "none", "null", "builtin", "(not set)"}


def slot_command(args):
    """The pass-through installer: the profile's memory provider (or args.provider) runs in breadcrumbs' slot and
    breadcrumbs takes its place; --off gives the slot its place back. Other settings in config.json stay."""
    from breadcrumbs import cli
    root = os.path.join(cli.profile_home(args.profile), "breadcrumbs")
    path = os.path.join(root, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    current = hermes_config(args.profile, "get", "memory.provider")
    if args.off:
        old = cfg.pop("slot", "")
        if not old:
            print("%s: no slot" % args.profile)
            return 0
        _write_json(path, cfg)
        hermes_config(args.profile, "set", "memory.provider", old)
        print("%s: %s is the memory provider again. breadcrumbs keeps the copies it made." % (args.profile, old))
        return 0
    name = args.provider or ("" if current.lower() in NO_PROVIDER or current == args.name else current)
    if not name:
        print("%s: no slot (memory provider: %s)%s" % (args.profile, current or "none",
                                                     "; the slot holds " + cfg["slot"] if cfg.get("slot") else ""))
        return 0
    cfg["slot"] = name
    _write_json(path, cfg)
    if current != args.name:
        hermes_config(args.profile, "set", "memory.provider", args.name)
    print("%s: %s runs in breadcrumbs' slot, and %s is the memory provider. A gateway restart loads it.\n"
          "undo: python -m breadcrumbs slot %s --off" % (args.profile, name, args.name, args.profile))
    return 0


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


# -- the ports: its data for the views and the searches --------------------------------------------------------
def _neighbor():
    from .hermes import NEIGHBOR
    return NEIGHBOR


def _dial(root, key):
    from . import dials
    return dials.value(dials.config(root), None, key)


def _state(path, fn, connect=None):
    from . import logview
    return logview.read(path, fn, connect)


def _codebase(folder):
    from .island import root_of
    return root_of(folder)


def _profile(home):
    from .island import profile_of
    return profile_of(home)


def _hide(home):
    from .novis import Hide
    return Hide(home)


def _blocked(home):
    from . import novis
    return novis.blocked(home)


_PRICES = {}  # (model, provider) -> rates or None: one ask of Hermes's table for each, in a process


def _price(model, provider):
    """The price port (breadcrumbs/usage.py): (input, cache read, cache write) in USD per 1M tokens, from Hermes's own table
    (agent/usage_pricing.py get_pricing_entry), or None: no Hermes on the path, an unknown model, or a missing rate.
    Imported at the first ask, inside a try: a view without Hermes still opens. No price is copied into this package."""
    key = (model, provider or None)
    if key not in _PRICES:
        try:
            from agent.usage_pricing import get_pricing_entry
            e = get_pricing_entry(model, provider=provider or None)
            rates = (e.input_cost_per_million, e.cache_read_cost_per_million, e.cache_write_cost_per_million) if e else None
            _PRICES[key] = None if rates is None or None in rates else tuple(float(x) for x in rates)
        except Exception:  # no Hermes here, or its table failed: no price
            _PRICES[key] = None
    return _PRICES[key]


def _ace_join(lines, requests):
    """The port ace_join (breadcrumbs/usage.py AFTER): each ace.log line with the main request that it shaped (ace.join)."""
    from .ace import join
    return join(lines, requests)


def _stored(home, fn):
    """fn(store, index, refs) on the open store of home; the store and the index close after it."""
    from breadcrumbs import cli
    store, index, refs, _zoom = cli.open_all(home)
    try:
        return fn(store, index, refs)
    finally:
        index.close()
        store.close()


def _add_ace(sub):
    sub.add_parser("ace", help="the size of each chat against its ACE target (from ace.log; read-only)")


def _ace(args, home):  # read-only
    from . import ace
    lines = ace.status_lines(home)
    print("\n".join(lines) if lines else "no ace.log at %s" % home)
    return 0 if lines else 1


def _add_dials(sub):
    p = sub.add_parser("dials", help="every knob and dial with its value and where the value came from (read-only)")
    p.add_argument("--model", help="the model whose entries of config.json count")
    p.add_argument("--markdown", action="store_true", help="the tables of README \"Knobs and dials\"")


def _dials(args, home):  # read-only
    from . import dials
    print(dials.markdown() if args.markdown else "\n".join(dials.show(home, args.model)))
    return 0


def _add_ingest(sub):
    p = sub.add_parser("ingest-hermes"); p.add_argument("profile")


def _ingest(args, home):
    def run(store, index, refs):
        from breadcrumbs import cli
        from .hermes_ingest import ingest_state_db
        db = os.path.join(cli.profile_home(args.profile), "state.db")
        print(json.dumps(ingest_state_db(db, store, index, refs)))
        return 0
    return _stored(home, run)


def _add_sources(sub):
    p = sub.add_parser("sources", help="the codebases in a profile's tool calls (islands)")
    p.add_argument("profile")


def _sources(args, home):
    from breadcrumbs import cli
    from . import island
    home = cli.hermes_home(args.profile)
    own = island.own_root(home)
    for root, (calls, chats, last) in sorted(island.scan(home).items(), key=lambda kv: -kv[1][0]):
        print("%7d calls %5d chats  %s  %s%s" % (calls, chats, time.strftime("%Y-%m-%d", time.localtime(last)),
                                               island.short(root), "  (the island of this profile)" * (root == own)))
    return 0


def _add_seats(sub):
    p = sub.add_parser("seats"); p.add_argument("profile"); p.add_argument("--repo", default=None)


def _add_seat(sub):
    p = sub.add_parser("seat"); p.add_argument("action", choices=("clone", "snap", "harvest", "restore", "replay"))
    p.add_argument("name"); p.add_argument("profile"); p.add_argument("--repo", default=None)
    p.add_argument("--path", action="append", default=[]); p.add_argument("--from", dest="src", default="memory")
    p.add_argument("--force", action="store_true")


def _seats(args, home):
    """`seats`, and `seat clone`: no store needed."""
    from breadcrumbs import cli
    from . import seats
    hh, repo = cli.hermes_home(args.profile), args.repo or seats.HERMES_REPO
    if args.cmd == "seats":
        for r in seats.survey(hh, repo):
            print("%-14s %-7s %-20s %d" % (r["name"], r["way"], ",".join(r["where"]) or "-", r["data"]))
        return 0
    try:
        path = seats.clone(args.name, os.path.join(hh, "plugins"), repo)
    except (LookupError, FileExistsError) as e:
        print("breadcrumbs: %s" % e, file=sys.stderr)
        return 1
    with open(os.path.join(path, "CLONED.json"), encoding="utf-8") as f:
        meta = json.load(f)
    print("cloned %s from %s at %s into %s" % (args.name, meta["repo"], meta["commit"], path))
    return 0


def _seat(args, home):
    if args.action == "clone":
        return _seats(args, home)

    def run(store, index, refs):
        from breadcrumbs import cli
        from . import seats
        hh = cli.hermes_home(args.profile)
        try:
            if args.action == "snap":
                sid = seats.snap(store, refs, args.name, hh, seats.state_paths(args.name, hh) + args.path)
                print(json.dumps({"seat": args.name, "snap": sid}))
                return 0 if sid else 1
            if args.action == "harvest":
                got, new = seats.harvest(store, index, refs, args.name, hh)
                print(json.dumps({"seat": args.name, "records": got, "new": len(new)}))
            elif args.action == "restore":
                sid = seats.snap_of(store, refs, args.name)
                print(json.dumps({"snap": sid, "written": seats.restore(store, sid, hh, force=args.force)}))
            else:  # replay
                provider = seats.load_provider(args.name, hh)
                try:
                    sent = seats.replay(store, refs, args.src, provider, args.name)
                finally:
                    getattr(provider, "shutdown", lambda: None)()
                print(json.dumps({"seat": args.name, "from": args.src, "sent": len(sent)}))
        except (LookupError, FileExistsError, ValueError) as e:
            print("breadcrumbs: %s" % e, file=sys.stderr)
            return 1
        return 0
    return _stored(home, run)


def _add_novis(sub):
    p = sub.add_parser("no-vis")  # private: no help line, and the usage line leaves it out (see breadcrumbs/novis.py)
    p.add_argument("action", nargs="?", choices=("list", "find", "add", "rm"), default="list")
    p.add_argument("names", nargs="*")


def _novis(args, home):
    from . import novis
    return novis._command(args.action, args.names)


def _add_slot(sub):
    p = sub.add_parser("slot", help="run another memory provider THRU breadcrumbs (a pass-through)")
    p.add_argument("profile"); p.add_argument("provider", nargs="?", default="")
    p.add_argument("--off", action="store_true", help="give the slot its place back as the memory provider")
    p.add_argument("--name", default="breadcrumbs", help="breadcrumbs' provider name in this profile")


def _slot(args, home):
    return slot_command(args)


def _add_install(sub):
    p = sub.add_parser("install-hermes"); p.add_argument("profile")
    p.add_argument("--name", default="breadcrumbs", help="provider and plugin dir name")
    p.add_argument("--dev", action="store_true", help="re-import the repo code at every agent build")


def _install(args, home):
    from breadcrumbs import cli
    target = os.path.join(cli.profile_home(args.profile), "plugins", args.name)
    shim = os.path.join(target, "__init__.py")
    if os.path.exists(shim):
        with open(shim, encoding="utf-8") as f:
            if "plugin shim, written by" not in f.read():
                print("breadcrumbs: %s holds a plugin this command did not write (a catalog install?); "
                      "not overwriting it. Use --name for a dev shim." % target, file=sys.stderr)
                return 1
    os.makedirs(target, exist_ok=True)
    with open(shim, "w", encoding="utf-8") as f:
        f.write(SHIM.format(repo=ports.pythonpath(), name=args.name, dev=args.dev))
    with open(os.path.join(target, "plugin.yaml"), "w", encoding="utf-8") as f:
        f.write(PLUGIN_YAML.format(name=args.name))
    print("wrote", target)
    print("activate: hermes -p %s config set memory.provider %s   (undo: hermes -p %s memory off)"
          % (args.profile, args.name, args.profile))
    return 0


def _source(path):
    """The sessions of a state.db for `ingest hermes [PATH]`; no PATH: the state.db of the default profile."""
    from breadcrumbs import cli
    from . import hermes_ingest
    return hermes_ingest.sessions(path or os.path.join(cli.profile_home("default"), "state.db"))


def _setup(cmd):
    return "# ~/.hermes/config.yaml\nmcp_servers:\n  breadcrumbs:\n    command: %s\n    args: %s" % (
        cmd[0], json.dumps(cmd[1:]))


if ports.layer("hermes", __file__):
    ports.give("neighbor", _neighbor)
    ports.give("dial", _dial)
    ports.give("state", _state)
    ports.give("codebase", _codebase)
    ports.give("profile", _profile)
    ports.give("hide", _hide)
    ports.give("blocked", _blocked)
    ports.give("price", _price)
    ports.give("ace_join", _ace_join)
    ports.command("ace", _add_ace, _ace, after="log")
    ports.command("dials", _add_dials, _dials, after="log")
    ports.command("ingest-hermes", _add_ingest, _ingest, after="pull")
    ports.command("slot", _add_slot, _slot, after="vault")
    ports.command("install-hermes", _add_install, _install, after="vault")
    ports.command("sources", _add_sources, _sources, after="vault")
    ports.command("seats", _add_seats, _seats, before="hive")
    ports.command("seat", _add_seat, _seat, before="hive")
    ports.command("no-vis", _add_novis, _novis, after="hive", hidden=True)
    ports.harness("hermes", "Hermes", ("hermes",), _setup, order=10, source=_source)
