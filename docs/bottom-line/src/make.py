#!/usr/bin/env python3
"""Make the bottom-line page: index.html (breadcrumbs/view.html with a replay of recorded answers) and data.json.

    python3 docs/bottom-line/src/make.py

It builds the demo store in a scratch folder (make-store.py), serves it with the plugin's own `serve` on a free port, asks
the server the questions that the page asks (the same names and values, see CASES), and writes each answer under its
question into data.json. The page then answers api() from data.json, so it needs no server. A question that is not in
data.json shows a note ("not in the demo") and nothing else changes. The scratch folder and the server go away at the end.
The key of a question: the name, "?", then name=value pairs sorted by name and joined with "&" (the token k is left out).
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(OUT))
FOCUS_UNIT_M = {"request": ("cost_usd", "u_prompt", "ace_sent", "ace_held", "u_cr", "ace_raw"), "turn": ("cost_usd", "ctx_bytes"), "event": ("cost_usd", "ctx_bytes")}  # cost_usd has no request: the page asks for it first, then takes another measure
LEVELS = (0.0625, 0.125, 0.25, 0.5, 1)  # the levels of a crumb that the map asks for
TIME_M_FOCUS = ("cost_usd", "u_prompt", "ace_held")   # a chat in focus, on the time axis
EXPAND_M = ("cost_usd", "u_prompt", "ace_held")


def qkey(name, q):
    return name + "?" + "&".join("%s=%s" % (k, q[k]) for k in sorted(q) if k != "k")


class Bake:
    def __init__(self, base, k):
        self.base, self.k, self.data, self.miss = base, k, {}, []

    def ask(self, name, **q):
        key = qkey(name, q)
        if key in self.data:
            return self.data[key]
        url = "%s/api/%s?%s" % (self.base, name, "&".join("%s=%s" % (a, urllib.request.quote(str(b), safe="")) for a, b in {**q, "k": self.k}.items()))
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                self.data[key] = json.load(r)
        except Exception as e:  # a question that the server cannot answer is left out
            self.miss.append((key, str(e)))
            return None
        return self.data[key]


def main():
    scratch = tempfile.mkdtemp(prefix="bottom-line-")
    srv = None
    try:
        subprocess.run([sys.executable, "-B", os.path.join(HERE, "make-store.py"), scratch], check=True, stdout=subprocess.DEVNULL)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
        env = dict(os.environ, PYTHONPATH=REPO)
        srv = subprocess.Popen([sys.executable, "-B", "-W", "ignore", "-m", "breadcrumbs", "--home", os.path.join(scratch, "breadcrumbs"),
                                "serve", "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
        line = srv.stdout.readline()
        k = re.search(r"#k=([0-9a-f]+)", line).group(1)
        b = Bake("http://127.0.0.1:%d" % port, k)
        top = b.ask("measures", unit="time", m="none", incl="1")
        meas = [m["key"] for m in top["dims"]["measure"] if m["has"]]
        scopes = [{}] + [{"codebase": s["codebase"]} for s in top["dims"]["scope"] if s["kind"] == "codebase"] + \
                 [{"codebase": s["codebase"], "branch": s["branch"]} for s in top["dims"]["scope"] if s["kind"] == "branch"]
        sess = [r for r in top["dims"]["family"] if r["kind"] == "sess"]
        # 1. the grid: every measure, SELF and INCLUSIVE; every scope chip (INCLUSIVE)
        for sc in scopes:
            b.ask("measures", unit="time", m="none", incl="1", **sc)
            for m in meas:
                b.ask("measures", unit="time", m=m, incl="1", **sc)
        for m in meas:
            b.ask("measures", unit="time", m=m, incl="0")
        # 2. a row opened: the members of each session, for three measures
        for m in EXPAND_M:
            for r in sess:
                if r["more"]:
                    b.ask("measures", unit="time", m=m, incl="1", root=r["key"])
        # 3. a chat in focus: on the time axis, then request, turn and event
        for r in sess:
            sid = r["sid"]
            for m in TIME_M_FOCUS:
                b.ask("measures", unit="time", m=m, incl="1", sid=sid, lines="1")
            b.ask("measures", unit="request", m="none", sid=sid, lines="1")
            for unit, ms in FOCUS_UNIT_M.items():
                for m in ms:
                    b.ask("measures", unit=unit, m=m, incl="1", sid=sid, lines="1")
            b.ask("ace", sid=sid)
        # 4. the rest of what the page asks on its own
        b.ask("ace", sid=""); b.ask("trail"); b.ask("calendar", limit="300")
        for n in b.ask("graph")["nodes"]:  # the map: its boxes show the text of each crumb at a level
            for lv in LEVELS:
                b.ask("crumb", id=n["id"], level=str(lv).rstrip("0").rstrip(".") if lv != 1 else "1")
        for r in sess:
            if r.get("ref"):
                got = b.ask("measures/labels", refs=r["ref"])
        data = {k_: v for k_, v in b.data.items() if v is not None}
        # drop the unused fields of one answer? No: the page reads what the server sent.
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(OUT, "data.json"), "w", encoding="utf-8") as f:
            f.write(text)
        page = open(os.path.join(REPO, "breadcrumbs", "view.html"), encoding="utf-8").read()
        open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(replay(page))
        print("questions", len(data), "bytes", len(text), "missing", len(b.miss), b.miss[:5])
    finally:
        if srv:
            srv.terminate()
            try: srv.wait(10)
            except Exception: srv.kill()
        shutil.rmtree(scratch, ignore_errors=True)


API_OLD = '''const api = (p, q = {}) => fetch("api/" + p + "?" + new URLSearchParams({...q, k: KEY}))
  .then(r => r.ok ? r.json() : Promise.reject(new Error(p + ": HTTP " + r.status)));'''
API_NEW = '''// DEMO: api() answers from data.json (recorded answers of the real server, made by src/make.py), not from a server.
const DEMO = fetch("data.json").then(r => r.json());
const demoKey = (p, q) => p + "?" + Object.keys(q).filter(k => k !== "k").sort().map(k => k + "=" + q[k]).join("&");
let demoNoteAt = 0;
const demoMiss = what => { if (Date.now() - demoNoteAt < 4000) return; demoNoteAt = Date.now(); console.warn("demo miss", what); const m = $("dnote"); m.textContent = "Not in this demo: " + what + ". Run /viz on your own store for every view."; m.style.display = "block"; setTimeout(() => { m.style.display = "none"; }, 5000); };
const api = (p, q = {}) => DEMO.then(D => {
  if (p === "measures/labels") { const out = {}; for (const ref of String(q.refs || "").split("|")) Object.assign(out, D["measures/labels?refs=" + ref] || {}); return out; }
  const key = demoKey(p, q);
  if (key in D) return D[key];
  (window.__miss = window.__miss || []).push(key);
  demoMiss(p === "measures" ? "this combination of measure, unit and chat" : p);
  return Promise.reject(new Error(p + ": not in the demo"));
});'''
BANNER = '''<div id="dbadge" style="position:fixed;right:10px;bottom:30px;z-index:99;background:#1e1e2e;border:1px solid #45475a;border-radius:6px;padding:4px 10px;font:12px/1.4 var(--sans);color:#f9e2af">Demo store · invented numbers · recorded answers of the real viewer · <a href="../" style="color:#94e2d5">site</a></div>
<div id="dnote" style="display:none;position:fixed;left:50%;top:52px;transform:translateX(-50%);z-index:99;background:#1e1e2e;border:1px solid #f9e2af;border-radius:6px;padding:6px 12px;font:13px/1.4 var(--sans);color:#f9e2af;pointer-events:none"></div>'''


def replay(page):
    assert API_OLD in page, "view.html changed: the api() lines are not where make.py expects them"
    page = page.replace(API_OLD, API_NEW, 1)
    page = page.replace('<html><head><meta charset="utf-8"><title>breadcrumbs</title>',
                        '<html><head><meta charset="utf-8"><title>breadcrumbs: the bottom line</title>\n<script>if (!location.hash) history.replaceState(null, "", location.pathname + "#measures=1");</script>', 1)
    assert "<body>" in page
    return page.replace("<body>", "<body>\n" + BANNER, 1)


if __name__ == "__main__":
    main()
