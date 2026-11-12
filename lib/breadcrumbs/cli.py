"""Command line: python -m breadcrumbs [--home DIR] COMMAND ...

  bc REF [LEVEL]            show a breadcrumb at a level 0..1 (default 0.25)
  find QUERY [K]            search; prints handles with their best line
  log [REF]                 commits on a ref (default main), newest first
  stats                     blobs, bytes on disk, raw bytes, ratios
  verify                    re-read every blob and check every CRC32C
  summarize [REF...] [--all]  small-model summaries (--all: every session and closed span)
  publish REF [--deep]      make a public copy of a crumb (the original stays private)
  share REF... --out FILE   export the public crumbs reachable from REF as an IPFS CAR file
  import FILE               import a CAR file as local public crumbs
  project NAME [REF...]     add public copies of crumbs to a shared project (no REF: list its heads)
  push NAME [--car FILE]    write the project head as a CAR, add it to IPFS (kubo), print its CID
  pull NAME CID|FILE        fetch a project head from IPFS (or read a CAR file) and join it
  ingest-hermes PROFILE     backfill a profile's state.db (opened read-only)
  ingest HARNESS [PATH]     read the sessions of a harness program into the store (hermes: its state.db)
  prune-index               drop index rows that older versions made for crumb/crumb_find calls
  install-hermes PROFILE    write the plugin shim into the profile
  sources PROFILE           the codebases in the profile's tool calls: calls, chats, last use (islands)
  term [--url URL] [--every S]  the explorer page drawn in Kitty or Ghostty (Kitty graphics protocol)
  core mcp|sock|a2a|setup [HARNESS]  the one read-only core of every harness (find, open, zoom, trail, stores, health):
                            mcp: MCP over stdio; sock: Unix socket (--tcp PORT adds loopback TCP); a2a: A2A on 127.0.0.1;
                            setup: the command that adds it to hermes, claude-code, codex or opencode
  open [web|kitty|ghostty] [--print]  the explorer in the browser (default), or in a new Kitty or Ghostty window that
                            runs term; --print shows the command line and runs nothing; --port P: the web port (8765)
  slot PROFILE [PROVIDER]   run the profile's memory provider (or PROVIDER) THRU breadcrumbs (--off: undo)
  seats PROFILE             the other memory systems: the way each one is taken in, where it is, its data
  seat clone NAME PROFILE   copy a provider that Hermes took out of its core from Hermes's git history
  seat snap NAME PROFILE    keep the exact bytes of a system's state files (--path adds more)
  seat harvest NAME PROFILE keep each record that a system stores as a searchable crumb
  seat restore NAME|ID PROFILE  write a snap's files back (--force replaces files that are there)
  seat replay NAME PROFILE --from REF  give the kept texts of REF to the system NAME (Hermes's Python)
  hive serve [NAME=DIR...] [--all NAME]  serve stores read-only on 127.0.0.1:4700 (public crumbs; --all: whole)
  hive up [--port P]        kubo: listen for the hive on P (and Hermes A2A), forward to each peer of config.json "hive"
  hive find QUERY [K] [--ms MS]  search the hive peers; a peer that cannot answer in MS passes
  hive open ID@PEER/STORE [LEVEL]  show a remote breadcrumb (each part is checked)
  hive card                 each peer: its stores, capacity and counts
  vault export DIR [--all]  write an Obsidian vault: one note per session (--all: private crumbs too)
  vault import DIR          read an Obsidian vault: one crumb per note, its exact bytes; [[links]] link crumbs
  migrate SYSTEM FILE       bring another memory system's export in, with its ids (chatgpt, claude, hermes,
                            mem0, openmemory, supermemory, hindsight, honcho, retaindb: see migrate.py)

PROFILE is a profile name or a HERMES_HOME dir (a path).
--home defaults to $BREADCRUMBS_HOME, else $HERMES_HOME/breadcrumbs, else ~/.hermes/breadcrumbs.
--graal runs the command under GraalPy (see breadcrumbs/runtime.py) when one is found.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from . import ports

def profile_home(profile):
    root = os.path.expanduser("~/.hermes")
    return root if profile in ("", "default") else os.path.join(root, "profiles", profile)


def default_home():
    if os.environ.get("BREADCRUMBS_HOME"):
        return os.environ["BREADCRUMBS_HOME"]
    return os.path.join(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), "breadcrumbs")


def hermes_home(arg):
    """A profile name, or a HERMES_HOME dir given as a path."""
    if os.sep in arg or "/" in arg or arg.startswith(("~", ".")):
        return os.path.abspath(os.path.expanduser(arg))
    return profile_home(arg)


def open_all(home):
    from .history import Refs
    from .index import Index
    from .store import Store
    from .zoom import Zoom
    store = Store(os.path.join(home, "store"))
    index = Index(os.path.join(home, "index.db"))
    return store, index, Refs(os.path.join(home, "refs")), Zoom(store, cues=index.signature, words=index.tiers)


def ipfs(*args, stdout=subprocess.DEVNULL, timeout=300):
    """Run kubo (the ipfs binary; BREADCRUMBS_IPFS overrides the PATH) with the user's IPFS_PATH."""
    exe = os.environ.get("BREADCRUMBS_IPFS") or shutil.which("ipfs")
    if not exe:
        raise SystemExit("breadcrumbs: no ipfs binary (kubo); install it, set BREADCRUMBS_IPFS, or use --no-ipfs")
    p = subprocess.run([exe, "--timeout", "%ds" % timeout, *args], stdout=stdout, stderr=subprocess.PIPE,
                       timeout=timeout + 30)
    if p.returncode:
        raise SystemExit("breadcrumbs: ipfs %s failed: %s" % (args[0], p.stderr.decode("utf-8", "replace")[-300:]))


def hive_stores(specs, whole):
    """NAME=DIR specs -> {name: (dir, scope)}: scope "all" for the names in whole, else "shared"."""
    out = {}
    for spec in specs:
        name, eq, root = spec.partition("=")
        if not (name and eq and root):
            raise SystemExit("breadcrumbs: a hive store is NAME=DIR, not %r" % spec)
        out[name] = (os.path.abspath(os.path.expanduser(root)), "all" if name in whole else "shared")
    return out


def hive_main(args, home):
    from . import hive
    if args.hcmd == "serve":
        stores = hive_stores(args.stores or ["main=" + home], args.all)
        server = hive.Server(stores, port=args.port, workers=args.workers, depth=args.depth,
                             max_ms=args.max_ms).start()
        print(json.dumps({"hive": "127.0.0.1:%d" % server.port, "proto": hive.PROTO, "stores": stores}), flush=True)
        try:
            while True:
                time.sleep(1)  # a sleep loop: Ctrl-C works on Windows too
        except KeyboardInterrupt:
            pass
        server.close()
        return 0
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f).get("hive") or {}
    except (OSError, ValueError, AttributeError):
        cfg = {}
    if args.hcmd == "up":  # kubo listeners for this host, and forwards to each peer
        ports = {hive.PROTO: args.port, hive.A2A: cfg.get("a2a")}  # a2a: this host's Hermes A2A port
        for proto, port in ports.items():
            ipfs("p2p", "close", "-p", proto, timeout=10)
            if port:
                ipfs("p2p", "listen", proto, "/ip4/127.0.0.1/tcp/%d" % int(port), timeout=10)
        for name, peer in sorted((cfg.get("peers") or {}).items()):
            for proto, key in ((hive.PROTO, "addr"), (hive.A2A, "a2a")):
                if peer.get("peer") and peer.get(key):
                    port = int(peer[key].rpartition(":")[2])
                    ipfs("p2p", "forward", proto, "/ip4/127.0.0.1/tcp/%d" % port, "/p2p/" + peer["peer"], timeout=10)
                    print("forward", name, proto, "127.0.0.1:%d" % port)
        return 0
    h = hive.Hive.from_config(cfg)
    if h is None:
        raise SystemExit("breadcrumbs: no hive peers in %s" % os.path.join(home, "config.json"))
    try:
        if args.hcmd == "find":
            ms = h.ms if args.ms is None else args.ms
            print(hive.render(h.ask({"op": "find", "q": args.query, "k": args.k}, ms), args.k, ms)[0], end="")
        elif args.hcmd == "open":
            ref, peer, store = hive.split(args.ref)
            print(h.open(peer, store, ref, args.level)[0], end="")
        elif args.hcmd == "card":
            print(json.dumps(h.ask({"op": "card"}, max(h.ms, 1000.0)), indent=1))
    finally:
        h.close()
    return 0


def dir_bytes(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        total += sum(os.path.getsize(os.path.join(base, f)) for f in files)
    return total


def ingest_command(args, home):
    """`ingest HARNESS [PATH]`: the sessions that the source of the harness gives (ports.harness), into the store. When the
    harness gives the usage of its requests too, they go to usage.log of the store home (breadcrumbs/usage.py append(): a
    request that is there already is not added again), and the stats get their totals and cache metrics ("usage")."""
    from . import usage
    from .ingest import ingest_sessions
    table = ports.harnesses()
    if args.harness not in table:
        print("breadcrumbs: harness is one of %s" % ", ".join(table), file=sys.stderr)
        return 2
    source = table[args.harness]["source"]
    if source is None:
        print("breadcrumbs: %s has no session source to ingest" % args.harness, file=sys.stderr)
        return 1
    store, index, refs, _zoom = open_all(home)
    try:
        message = "ingest " + (os.path.basename(args.path) if args.path else args.harness)
        stats = ingest_sessions(store, index, refs, source(args.path), message)
    finally:
        index.close()
        store.close()
    read = table[args.harness].get("usage")
    records = list(read(args.path)) if read is not None else []
    if records:
        stats["usage"] = dict(usage.summary(records), added=usage.append(home, records))
    print(json.dumps(stats))
    return 0


def core_command(args, home):
    """`core mcp|sock|a2a|setup`: the servers of core.py. All are read-only. `home` is --home (None: the default only)."""
    from . import core as corelib
    stores = corelib.Stores(local=home or os.environ.get("BREADCRUMBS_HOME"))
    if args.mode == "setup":
        table = corelib.harnesses()
        names = [args.harness] if args.harness else list(table)
        if any(n not in table for n in names):
            print("breadcrumbs: harness is one of %s" % ", ".join(table), file=sys.stderr)
            return 2
        for n in names:
            print("%s:\n%s\n" % (table[n]["label"], table[n]["setup"](corelib.server_command("mcp"))))
        return 0
    core = corelib.Core(stores)
    path = None
    key = lambda: corelib.token(os.path.join(stores.base, "core.token"))  # noqa: E731
    try:
        if args.mode == "mcp":
            from . import mcp
            return mcp.serve(core)
        if args.mode == "a2a":
            from . import a2a
            server = a2a.make_server(core, args.port, key())
            print("breadcrumbs core a2a: http://127.0.0.1:%d/.well-known/agent-card.json (token: %s)" % (
                server.server_address[1], os.path.join(stores.base, "core.token")), file=sys.stderr, flush=True)
            servers = [server]
        else:
            from . import sock
            path = args.unix or sock.default_path(core)
            servers: list = [sock.make_unix(core, path)]
            print("breadcrumbs core sock: %s" % path, file=sys.stderr, flush=True)
            if args.tcp is not None:
                servers.append(sock.make_tcp(core, args.tcp, key()))
                print("breadcrumbs core sock: 127.0.0.1:%d (token: %s)" % (
                    servers[-1].server_address[1], os.path.join(stores.base, "core.token")), file=sys.stderr, flush=True)
        import threading
        for s in servers[1:]:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            servers[0].serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for s in servers:
                s.server_close()
        if args.mode == "sock" and path and os.path.exists(path):
            os.unlink(path)
        return 0
    except OSError as e:
        print("breadcrumbs core: %s" % e, file=sys.stderr)
        return 1
    finally:
        core.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="breadcrumbs", description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=None)
    ap.add_argument("--graal", action="store_true", help="run under GraalPy when it is available")
    sub = ap.add_subparsers(dest="cmd", required=True)
    given = {c["name"]: c for c in ports.commands()}  # the commands of the layers above: see ports.command

    def add(name, **kw):
        """A command of this module, with the given commands that land before it and after it."""
        for c in given.values():
            if c["before"] == name:
                c["add"](sub)
        parser = sub.add_parser(name, **kw)
        for c in given.values():
            if c["after"] == name:
                c["add"](sub)
        return parser

    p = add("bc"); p.add_argument("ref"); p.add_argument("level", nargs="?", type=float, default=0.25)
    p = add("find"); p.add_argument("query"); p.add_argument("k", nargs="?", type=int, default=8)
    p = add("log"); p.add_argument("ref", nargs="?", default="main")
    add("stats")
    add("verify")
    p = add("summarize"); p.add_argument("refs", nargs="*"); p.add_argument("--all", action="store_true")
    p = add("publish"); p.add_argument("ref"); p.add_argument("--deep", action="store_true")
    p = add("share"); p.add_argument("refs", nargs="+"); p.add_argument("--out", required=True)
    p = add("import"); p.add_argument("car")
    p = add("project"); p.add_argument("name"); p.add_argument("refs", nargs="*")
    p = add("push"); p.add_argument("name"); p.add_argument("--car")
    p.add_argument("--no-ipfs", action="store_true", help="write the CAR file only")
    p = add("pull"); p.add_argument("name"); p.add_argument("source", help="a CID or a CAR file")
    p = add("ingest", help="read the sessions of a harness program into the store")
    p.add_argument("harness", help="the key of a harness that has a session source (see `core setup`)")
    p.add_argument("path", nargs="?", default=None, help="where the program keeps its sessions (default: its own place)")
    add("prune-index")
    p = add("supersede", help="tag crumb OLD as superseded by crumb NEW (--undo: retract the tag)")
    p.add_argument("old"); p.add_argument("new"); p.add_argument("--undo", action="store_true")
    p = add("migrate", help="bring another memory system's export in, with its ids")
    p.add_argument("system", choices=("chatgpt", "claude", "hermes", "mem0", "openmemory", "supermemory",
                                      "hindsight", "honcho", "retaindb")); p.add_argument("file")
    p = add("vault", help="the memory as an Obsidian vault (export), a vault as crumbs (import)")
    p.add_argument("action", choices=("export", "import")); p.add_argument("dir")
    p.add_argument("--all", action="store_true", help="export: the private crumbs too")
    p = add("core", help="the one core of every harness: MCP, A2A or a socket (see breadcrumbs/core.py)")
    p.add_argument("mode", choices=("mcp", "sock", "a2a", "setup"))
    p.add_argument("harness", nargs="?", help="setup: hermes, claude-code, codex or opencode (default: all)")
    p.add_argument("--unix", default=None, help="sock: the socket path (default: core.sock next to the default store)")
    p.add_argument("--tcp", type=int, default=None, help="sock: also listen on 127.0.0.1:PORT (needs the token)")
    p.add_argument("--port", type=int, default=4701, help="a2a: the port on 127.0.0.1")
    add("help", help="show this list")
    p = add("hive", help="search other hosts within a time budget (see breadcrumbs/hive.py)")
    hs = p.add_subparsers(dest="hcmd", required=True)
    q = hs.add_parser("serve"); q.add_argument("stores", nargs="*", help="NAME=DIR (default: main=--home)")
    q.add_argument("--all", action="append", default=[], help="serve this store whole, not only its public crumbs")
    q.add_argument("--port", type=int, default=4700); q.add_argument("--workers", type=int, default=2)
    q.add_argument("--depth", type=int, default=32); q.add_argument("--max-ms", type=float, default=2000)
    q = hs.add_parser("up"); q.add_argument("--port", type=int, default=4700)
    q = hs.add_parser("find"); q.add_argument("query"); q.add_argument("k", nargs="?", type=int, default=8)
    q.add_argument("--ms", type=float, default=None)
    q = hs.add_parser("open"); q.add_argument("ref"); q.add_argument("level", nargs="?", type=float, default=0.25)
    hs.add_parser("card")
    for name, c in given.items():
        if name not in sub.choices:  # its anchor is no command here: it lands at the end
            c["add"](sub)
    sub.metavar = "{%s}" % ",".join(n for n in sub.choices if not (n in given and given[n]["hidden"]))
    cli_args = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(cli_args)
    home = args.home or default_home()
    if args.graal and sys.implementation.name != "graalpy":
        from .runtime import find_graalpy, graal_argv
        exe = find_graalpy()
        if exe:
            env = dict(os.environ, PYTHONPATH=ports.pythonpath(os.environ.get("PYTHONPATH")))
            sys.stdout.flush()
            os.execve(exe, graal_argv(exe, [a for a in cli_args if a != "--graal"]), env)
        print("breadcrumbs: no GraalPy found; running on %s" % sys.implementation.name, file=sys.stderr)

    if args.cmd in given:
        return given[args.cmd]["run"](args, home)
    if args.cmd == "core":
        return core_command(args, args.home)
    if args.cmd == "ingest":
        return ingest_command(args, home)
    if args.cmd == "hive":
        return hive_main(args, home)

    if args.cmd == "help":
        ap.print_help()
        return 0

    if args.cmd == "bc":  # read-only, like the Reader: safe while a gateway runs (the dispatch handbook)
        from .reader import Reader
        try:
            v = Reader(home)
        except FileNotFoundError:
            print("no store at %s" % home, file=sys.stderr)
            return 1
        try:
            sys.stdout.write(v.crumb(args.ref, args.level)["text"])
        except (KeyError, ValueError):
            print("no crumb %s in %s" % (args.ref, home), file=sys.stderr)
            return 1
        finally:
            v.close()
        return 0

    if args.cmd == "vault" and args.action == "export":  # read-only, like bc
        from .reader import Reader
        from .vault import export_vault
        try:
            v = Reader(home)
        except FileNotFoundError:
            print("no store at %s" % home, file=sys.stderr)
            return 1
        try:
            print(json.dumps(export_vault(v, args.dir, everything=args.all)))
        except FileExistsError as e:
            print("breadcrumbs: %s" % e, file=sys.stderr)
            return 1
        finally:
            v.close()
        return 0

    store, index, refs, zoom = open_all(home)
    try:
        if args.cmd == "find":
            from .finder import find
            from .index import NEIGHBOR
            neighbor = ports.get("neighbor")  # the dial of the harness; no harness: the default of the dial
            sys.stdout.write(find(store, index, refs, zoom, args.query, args.k, neighbor() if neighbor else NEIGHBOR))
        elif args.cmd == "log":
            from .history import log
            for cid, c in log(store, refs.get(args.ref)):
                print(cid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c.meta.get("ts", 0))),
                      len(c.kids), "roots", c.meta.get("msg", ""))
        elif args.cmd == "stats":
            store.flush()
            raw = sum(store._len)
            pack = os.path.getsize(os.path.join(home, "store", "blobs.pack"))
            from .crc32c import BACKEND
            out = {"home": home, "runtime": sys.implementation.name, "crc32c": BACKEND,
                   "blobs": len(store), "unique_raw_bytes": raw, "pack_bytes": pack,
                   "pack_ratio": round(raw / pack, 2) if pack else None,
                   "store_bytes": dir_bytes(os.path.join(home, "store")),
                   "index_bytes": os.path.getsize(os.path.join(home, "index.db")),
                   "refs": len(refs.names())}
            print(json.dumps(out, indent=1))
        elif args.cmd == "verify":
            t0, n, bad = time.perf_counter(), 0, 0
            for blob_id in list(store.ids()):
                try:
                    store.get(blob_id)
                except ValueError:
                    bad += 1
                n += 1
            print(json.dumps({"blobs": n, "bad": bad, "seconds": round(time.perf_counter() - t0, 3)}))
            return 1 if bad else 0
        elif args.cmd == "summarize":
            from . import summarize as sm
            from .history import commit
            runner = sm.Llama()
            if hasattr(runner, "available") and not runner.available():
                print("breadcrumbs: no model: set BREADCRUMBS_SUM_MODEL and BREADCRUMBS_LLAMA", file=sys.stderr)
                return 2
            targets = [refs.get(r) or r for r in args.refs]
            if args.all:  # innermost spans first, so a session can use its span summaries
                for name in sorted(refs.names()):
                    if name.startswith("sess-"):
                        targets += sm.closed_spans(store, refs.get(name)) + [refs.get(name)]
            made, failed, t0 = [], 0, time.perf_counter()
            for cid in dict.fromkeys(targets):
                had = index.summary_of(cid)
                try:
                    sid = sm.summarize(store, index, cid, runner)
                except Exception as e:  # one bad summary must not stop the rest
                    failed += 1
                    print("breadcrumbs: summary of %s failed: %s" % (cid, str(e)[-200:]), file=sys.stderr)
                    continue
                if sid and not had:
                    made.append(sid)
                    store.flush()
                    index.commit()
            if made:
                commit(store, refs, "summaries", made, "summarize %d" % len(made))
            print(json.dumps({"targets": len(dict.fromkeys(targets)), "made": len(made), "failed": failed,
                              "seconds": round(time.perf_counter() - t0, 1)}))
            return 1 if failed else 0
        elif args.cmd == "publish":
            from .share import publish
            print(publish(store, args.ref, args.deep))
        elif args.cmd == "share":
            from .share import export_car
            roots, stats = export_car(store, args.refs, args.out)
            print(json.dumps({"out": args.out, "roots": roots, **stats}))
        elif args.cmd == "import":
            from .history import commit
            from .share import import_car
            local = import_car(store, index, args.car)
            commit(store, refs, "import", local, "import " + os.path.basename(args.car))
            print(json.dumps({"roots": local}))
        elif args.cmd == "project":
            from .crumbs import get_crumb
            from .share import PROJ, project_add
            if args.refs:
                print(json.dumps({"project": args.name, "head": project_add(store, refs, args.name, args.refs)}))
            head = refs.get(PROJ + args.name)
            while head:  # the heads, newest first (first parents)
                c = get_crumb(store, head)
                print(json.dumps({"head": head, "cid": c.meta.get("cid"), "kids": len(c.kids),
                                  "joins": len(c.heritage) > 1}))
                head = c.heritage[0] if c.heritage else None
        elif args.cmd == "push":
            from .share import push
            path = args.car or os.path.join(home, "share", args.name + ".car")
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            cid, head = push(store, refs, args.name, path)
            if not args.no_ipfs:
                ipfs("dag", "import", "--pin-roots", path)
            print(json.dumps({"project": args.name, "cid": cid, "head": head, "car": path}))
        elif args.cmd == "pull":
            from .share import pull
            src = args.source
            if not os.path.isfile(src):  # a CID: kubo fetches the blocks from its peers
                src = os.path.join(home, "share", args.source + ".car")
                os.makedirs(os.path.dirname(src), exist_ok=True)
                with open(src + ".tmp", "wb") as f:
                    ipfs("dag", "export", args.source, stdout=f)
                os.replace(src + ".tmp", src)
            print(json.dumps({"project": args.name, "head": pull(store, index, refs, args.name, src), "car": src}))
        elif args.cmd == "migrate":
            from .migrate import migrate_file
            print(json.dumps(migrate_file(store, index, refs, args.system, args.file)))
        elif args.cmd == "vault":  # import: export is read-only (above)
            from .vault import import_vault
            print(json.dumps(import_vault(store, index, refs, args.dir)))
        elif args.cmd == "supersede":
            from . import tags
            from .crumbs import get_crumb
            try:
                tag = tags.supersede(store, index, refs, args.old, args.new, undo=args.undo)
            except KeyError as e:
                print("breadcrumbs: no crumb %s in %s" % (e.args[0] if e.args else "", home), file=sys.stderr)
                return 1
            except ValueError as e:
                print("breadcrumbs: %s" % e, file=sys.stderr)
                return 1
            old, new = get_crumb(store, tag).heritage
            print(json.dumps({"tag": tag, "old": old, "new": new, "undo": args.undo}))
        elif args.cmd == "prune-index":
            from .ingest import prune_index
            n, rows = prune_index(store, index)
            print("pruned %d crumbs, %d index rows (calls and results of crumb and crumb_find; the crumbs stay)"
                  % (n, rows))
    finally:
        index.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
