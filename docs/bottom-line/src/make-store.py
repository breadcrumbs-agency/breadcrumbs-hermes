#!/usr/bin/env python3
"""The demo store of the bottom-line page: the seeded demo memory (demo/seed.py) and the made-up state.db, ace.log and
usage.log of tests/test_view.py build_fixture, then a made-up two days before it, so that the page shows habits.
All numbers are invented. Usage: make-store.py DIR   (DIR is made; the store is DIR/breadcrumbs)"""
import os, sys, sqlite3, random, time, datetime
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path[:0] = [ROOT, ROOT + "/tests"]
os.environ["BREADCRUMBS_SUMMARIZE"] = "0"
import test_view as tv  # noqa: E402

home = sys.argv[1]
os.makedirs(home, exist_ok=True)
fx = tv.build_fixture(home, spread=900)
t_today = fx["t"]["today"][0]
rnd = random.Random(13)
SONNET, OPUS, HAIKU = tv.SONNET, tv.OPUS, tv.HAIKU
db = sqlite3.connect(os.path.join(home, "state.db"))
ace_f = open(os.path.join(home, "breadcrumbs", "ace.log"), "a")
use_f = open(os.path.join(home, "breadcrumbs", "usage.log"), "a")
midnight = datetime.datetime.fromtimestamp(t_today).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
PRICE = {SONNET: 3.0, OPUS: 15.0, HAIKU: 0.8}  # usd per million prompt tokens, made up for the picture
n = 0
# (days back, hour, minutes, repo, branch, model, requests, big)  -> a habit: late nights in repo A on Opus, small morning chats in B on Sonnet
plan = []
for d in (1, 2):
    plan.append((d, 8.7 + rnd.random() * .2, 14, tv.REPO_B, "main", SONNET, 5, False))
    plan.append((d, 9.6 + rnd.random() * .2, 22, tv.REPO_B, "main", SONNET, 7, False))
    plan.append((d, 13.1 + rnd.random() * .2, 25, tv.REPO_B, "fix/cassette", SONNET, 8, False))
    plan.append((d, 16.7 + rnd.random() * .2, 12, tv.REPO_B, "fix/cassette", SONNET, 4, False))
    plan.append((d, 21.0 + rnd.random() * .2, 80, tv.REPO_A, "main", OPUS, 18, True))
    plan.append((d, 23.2, 55, tv.REPO_A, "feature/kettle", OPUS, 16, True))
def stamp(ts): return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
for d, hr, mins, repo, br, model, reqs, big in plan:
    n += 1
    sid = "wk%02d" % n
    t0 = midnight - d * 86400 + hr * 3600
    t1 = t0 + mins * 60
    tot = dict(i=0, o=0, cr=0, cw=0, cost=0.0)
    raw = 6000 if not big else 30000
    for k in range(reqs):
        ts = t0 + k * (mins * 60 / reqs)
        raw += rnd.randint(2500, 9000) * (3 if big else 1)
        target = 80000 if big else 40000
        fold = raw > target
        sent = int(raw * 0.55) if fold else raw
        act = "fold" if fold else "pass"
        if fold: raw = sent
        held = raw if not fold else sent
        line = ("%s pid=%d sess=%s act=%s in=%d out=%d last=%d base=%d tpb=%.4f r=%.4f folds=%d layer=%d target=%d lay=%d\n" %
                (stamp(ts), 1000, sid, act, int(sent / 0.55) if fold else raw, sent, 0, 3000, 4.0, 1.0, k // 5, 9 if k > 2 else 0, target, 400 if k > 2 else 0))
        ace_f.write(line)
        cr = int(sent * (0.88 if k else 0)); inp = sent - cr + 80; out = rnd.randint(150, 700)
        use_f.write("%s sess=%s turn=t%d req=r%d n=%d task=main model=%s resp=%s prov=anthropic in=%d cr=%d cw=%d out=%d rsn=%d prompt=%d est=0 msgs=%d tools=0 dur=%.3f fin=stop\n" %
                    (stamp(ts + 4), sid, k // 2 + 1, k, k + 1, model, model, inp, cr, 400 if fold else 0, out, 0, inp + cr, 10 + k * 2, 2 + out / 150))
        tot["i"] += inp; tot["cr"] += cr; tot["o"] += out; tot["cw"] += 400 if fold else 0
        tot["cost"] += (inp + tot["cw"]*0) / 1e6 * PRICE[model] + cr / 1e6 * PRICE[model] * 0.1 + out / 1e6 * PRICE[model] * 5
    # the helper calls that a plugin or Hermes core makes for this chat (a task name, not "main")
    for task, mdl, ptok, ot in (("title_generation", HAIKU, 300, 12),) + ((("compression", HAIKU, 9000, 700),) if big else ()) + ((("approval", HAIKU, 700, 20),) if rnd.random() < .5 else ()):
        use_f.write("%s sess=%s turn=t1 req=aux-%08x n=1 task=%s model=%s resp=%s prov=anthropic in=%d cr=0 cw=0 out=%d rsn=0 prompt=%d est=0 msgs=2 tools=0 dur=0.600 fin=stop\n" %
                    (stamp(t0 + 5), sid, rnd.getrandbits(32), task, mdl, mdl, ptok, ot, ptok))
        c = ptok / 1e6 * PRICE[HAIKU] + ot / 1e6 * PRICE[HAIKU] * 5
        db.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, HAIKU, "anthropic", "", "", task, 1, ptok, ot, 0, 0, 0, c, 0.0, "estimated", "official_docs_snapshot", t0 + 5, t0 + 6))
    # two made-up plugins that ask side questions: Hermes bills them as plugin_side_question:<plugin_id> (agent/side_question_request.py)
    for pid, mdl, ptok, ot, cnt in (("cost-guard", SONNET, 2600, 140, 3 + rnd.randint(0, 3)), ("notes-sync", HAIKU, 5200, 380, 2 + rnd.randint(0, 2))):
        if pid == "notes-sync" and not big:
            continue
        for k in range(cnt):
            use_f.write("%s sess=%s turn=t%d req=aux-%08x n=1 task=plugin_side_question model=%s resp=%s prov=anthropic in=%d cr=0 cw=0 out=%d rsn=0 prompt=%d est=0 msgs=3 tools=0 dur=1.100 fin=stop\n" %
                        (stamp(t0 + 60 + k * 240), sid, k + 1, rnd.getrandbits(32), mdl, mdl, ptok, ot, ptok))
        c = cnt * (ptok / 1e6 * PRICE[mdl] + ot / 1e6 * PRICE[mdl] * 5)
        db.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, mdl, "anthropic", "", "", "plugin_side_question:" + pid, cnt, ptok * cnt, ot * cnt, 0, 0, 0, c, 0.0, "estimated", "official_docs_snapshot", t0 + 60, t1))
        tot["cost"] += c
    db.execute("INSERT INTO sessions (id,source,created_source,started_at,ended_at,last_activity_at,message_count,tool_call_count,api_call_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,cwd,git_repo_root,git_branch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, "cli", "cli", t0, t1, t1, reqs * 2, reqs, reqs, tot["i"], tot["o"], tot["cr"], tot["cw"], 0, tot["cost"], 0.0, repo, repo, br))
    db.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, model, "anthropic", "", "", "", reqs, tot["i"], tot["o"], tot["cr"], tot["cw"], 0, tot["cost"], 0.0, "estimated", "provider_models_api", t0, t1))
db.commit(); db.close(); ace_f.close(); use_f.close()
print("store", home, "extra chats", n, "repos", tv.REPO_A, tv.REPO_B)
