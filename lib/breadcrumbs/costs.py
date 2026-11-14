"""The cost profiles of models: the prices per million tokens, the prompt size where a price steps up, and the context window.

A profile comes from two places, and the one that names a model wins:
  BUILTIN           the prices that breadcrumbs was checked against, each with its source (Haiku 5.5 is here)
  <root>/costs.json the profiles that a user or an LLM fetched and checked: `cost fetch` prints the task for the LLM,
                    `cost set FILE` checks the profile and saves it. A model with no profile keeps the price of its
                    harness's own table (Hermes: agent/usage_pricing.py), as before.

File format (costs.json, all rates in USD per 1M tokens):

  {"models": {"claude-haiku-5-5": {
      "context_window": 1000000,
      "tiers": [
        {"upto": 100000, "input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
        {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625}],
      "sources": [{"url": "https://...", "retrieved": "2026-11-13", "quote": "the line that says the rate"}]}}}

A tier applies to a request whose prompt has at most "upto" tokens; the last tier has no "upto". The prompt is what the
provider counts: input, cache reads and cache writes together (usage.py: PROMPT). The cache write is the 5-minute one.
A model name is a glob (fnmatch, case-insensitive); an exact name is tried before the globs, in file order.
"""
import datetime
import fnmatch
import json
import os
import sys
import tempfile

RATES = ("input", "output", "cache_read", "cache_write")
FIELDS = {"context_window", "tiers", "sources"}
TIER_FIELDS = set(RATES) | {"upto"}
SOURCE_FIELDS = {"url", "retrieved", "quote"}
QUOTE_MAX = 300

# The prompt size at which Anthropic's price for Claude Haiku 5.5 steps up (dials.PRICE_STEPS reads it from BUILTIN).
HAIKU_STEP = 100_000

BUILTIN = {
    "*haiku-5[.-]5*": {
        "context_window": 1_000_000,
        "tiers": [
            {"upto": HAIKU_STEP, "input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
            {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625},
        ],
        "sources": [
            {"url": "https://platform.claude.com/docs/en/about-claude/pricing", "retrieved": "2026-11-13",
             "quote": "Claude Haiku 5.5: $0.10 / MTok for prompts up to 100,000 tokens; $0.50 / MTok for prompts over 100,000."},
            {"url": "https://platform.claude.com/docs/en/about-claude/pricing", "retrieved": "2026-11-13",
             "quote": "Output $0.50 / $2.50; 5m cache write $0.125 / $0.625; cache read $0.01 / $0.05 (up to / over 100,000)."},
            {"url": "https://platform.claude.com/docs/en/models/haiku-5-5/overview", "retrieved": "2026-11-13",
             "quote": "Claude Haiku 5.5: context window 1M tokens, max output 128K tokens."},
        ],
    },
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _date(s):
    try:
        datetime.date.fromisoformat(s)
        return len(s) == 10
    except (TypeError, ValueError):
        return False


def validate(profile):
    """(profile, problems): the clean profile (tiers as (upto, rates) pairs) and what is wrong with it (empty when fine)."""
    bad = []
    if not isinstance(profile, dict):
        return None, ["a profile is an object"]
    extra = set(profile) - FIELDS
    if extra:
        bad.append("unknown field(s) %s (a typing mistake?)" % ", ".join(sorted(extra)))
    cw = profile.get("context_window")
    if cw is not None and (not isinstance(cw, int) or isinstance(cw, bool) or cw < 1000):
        bad.append("context_window must be a whole number of tokens of 1000 or more")
    tiers = profile.get("tiers")
    out_tiers = []
    if not isinstance(tiers, list) or not tiers:
        bad.append("tiers must be a non-empty list")
    else:
        last = 0
        for i, t in enumerate(tiers):
            where = "tier %d" % (i + 1)
            if not isinstance(t, dict):
                bad.append("%s is not an object" % where)
                continue
            if set(t) - TIER_FIELDS:
                bad.append("%s: unknown field(s) %s" % (where, ", ".join(sorted(set(t) - TIER_FIELDS))))
            missing = [r for r in RATES if r not in t]
            if missing:
                bad.append("%s: missing %s" % (where, ", ".join(missing)))
            rates = {}
            for r in RATES:
                if r in t and (not _num(t[r]) or t[r] < 0):
                    bad.append("%s: %s must be a price of 0 or more (USD per 1M tokens)" % (where, r))
                elif r in t:
                    rates[r] = float(t[r])
            last_tier = i == len(tiers) - 1
            if "upto" in t:
                if last_tier:
                    bad.append("the last tier has no upto (it applies above the tier before it)")
                up = t["upto"]
                if not isinstance(up, int) or isinstance(up, bool) or up < 1 or up <= last:
                    bad.append("%s: upto must be a whole number of tokens, above the one before it" % where)
                else:
                    last = up
                out_tiers.append((up, rates))
            else:
                if not last_tier:
                    bad.append("%s has no upto but is not the last tier" % where)
                out_tiers.append((None, rates))
    sources = profile.get("sources")
    if not isinstance(sources, list) or not sources:
        bad.append("sources must list at least one page the rates were read from")
    else:
        for i, s in enumerate(sources):
            where = "source %d" % (i + 1)
            if not isinstance(s, dict) or set(s) - SOURCE_FIELDS or not {"url", "retrieved"} <= set(s):
                bad.append("%s needs url and retrieved (and an optional quote)" % where)
                continue
            if not (isinstance(s["url"], str) and s["url"].startswith("https://")):
                bad.append("%s: url must be an https address" % where)
            if not _date(s["retrieved"]):
                bad.append("%s: retrieved must be a date, YYYY-MM-DD" % where)
            if "quote" in s and (not isinstance(s["quote"], str) or len(s["quote"]) > QUOTE_MAX):
                bad.append("%s: quote must be text of %d characters or fewer" % (where, QUOTE_MAX))
    if bad:
        return None, bad
    clean = {"tiers": out_tiers, "sources": sources}
    if cw is not None:
        clean["context_window"] = cw
    return clean, []


def to_file(profile):
    """The clean profile as the file holds it: its tiers as objects."""
    tiers = []
    for upto, rates in profile["tiers"]:
        t = {} if upto is None else {"upto": upto}
        t.update(rates)
        tiers.append(t)
    out = {"tiers": tiers, "sources": profile["sources"]}
    if "context_window" in profile:
        out["context_window"] = profile["context_window"]
    return out


def _read(root):
    """(profiles, notes) of <root>/costs.json: the clean ones, and a note for each thing that is wrong."""
    try:
        with open(os.path.join(root, "costs.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return {}, ["costs.json: \"models\" must be an object"]
    good, notes = {}, []
    for name, raw in models.items():
        prof, bad = validate(raw)
        if bad:
            notes += ["costs.json %s: %s" % (name, b) for b in bad]
        else:
            good[name] = prof
    return good, notes


_cache = {}


def profiles(root=None):
    """(profiles, notes): the builtin profiles with those of <root>/costs.json over them. A file that is missing gives
    the builtin ones; a file that is bad gives them with a note. The file is read again when it changes."""
    saved, notes = {}, []
    if root:
        try:
            stamp = os.stat(os.path.join(root, "costs.json")).st_mtime_ns
        except OSError:
            stamp = None
        key = (os.path.abspath(root), stamp)
        if key not in _cache:
            _cache.clear()
            _cache[key] = _read(root)
        saved, notes = _cache[key]
    out = {name: validate(raw)[0] for name, raw in BUILTIN.items()}
    out.update(saved)
    return out, notes


def _saved_names(root):
    return set(_read(root)[0]) if root else set()


def lookup(model, root=None):
    """(name, profile) of a model: its exact name first, then the globs in order (the file's before the builtin ones),
    or (None, None)."""
    name_l = str(model or "").lower()
    if not name_l:
        return None, None
    table, _ = profiles(root)
    for key in table:
        if key.lower() == name_l:
            return key, table[key]
    for key in table:
        if fnmatch.fnmatchcase(name_l, key.lower()):
            return key, table[key]
    return None, None


def pick(price, prompt):
    """The rates (input, cache read, cache write) for a request whose prompt has prompt tokens. price is a tuple of
    rates, or a list of (upto, rates) tiers for a price that steps up with the prompt; None stays None."""
    if not isinstance(price, list):
        return price
    for upto, rates in price:
        if upto is None or prompt <= upto:
            return rates
    return price[-1][1]


def price_tiers(profile):
    """The price port's form of a profile: [(upto, (input, cache read, cache write)), ...]."""
    return [(upto, (r["input"], r["cache_read"], r["cache_write"])) for upto, r in profile["tiers"]]


def tier_for(profile, prompt):
    """The rates dict (input, output, cache_read, cache_write) of the tier that a prompt of that size pays."""
    for upto, rates in profile["tiers"]:
        if upto is None or prompt <= upto:
            return rates
    return profile["tiers"][-1][1]


def context_window(model, root=None):
    """The context window in tokens that a profile gives the model, or None."""
    _, prof = lookup(model, root)
    return prof.get("context_window") if prof else None


def template(model=""):
    """A profile to fill in: the fields, with null where a value is to be found on the web."""
    return json.dumps({"models": {model or "<model name>": {
        "context_window": None,
        "tiers": [
            {"upto": None, "input": None, "output": None, "cache_read": None, "cache_write": None},
            {"input": None, "output": None, "cache_read": None, "cache_write": None},
        ],
        "sources": [{"url": "https://...", "retrieved": "YYYY-MM-DD", "quote": "the line that says it"}],
    }}}, indent=2)


def fetch_brief(model, root=None, harness_price=None):
    """The task for an LLM that can search the web: what is known now, the form to fill, the rules, and the commands
    that check and save the result. Breadcrumbs does no search itself: the LLM does, and `cost set` checks the result."""
    name, prof = lookup(model, root)
    home = root or "<home>"
    lines = ["Cost profile for %r (today %s)" % (model, datetime.date.today().isoformat()), ""]
    if prof:
        where = "in %s" % os.path.join(home, "costs.json") if name in _saved_names(root) else "built in"
        lines.append("Known now: profile %r, %s." % (name, where))
        for upto, rates in prof["tiers"]:
            lines.append("  %s: input %g, output %g, cache read %g, cache write %g (USD per 1M)"
                         % ("up to %s tokens" % format(upto, ",") if upto else "above the tier before",
                            rates["input"], rates["output"], rates["cache_read"], rates["cache_write"]))
        if prof.get("context_window"):
            lines.append("  context window: %s tokens" % format(prof["context_window"], ","))
    else:
        lines.append("Known now: no profile.")
    if harness_price:
        lines.append("The harness's own table gives: input %g, cache read %g, cache write %g (USD per 1M)."
                     % tuple(harness_price))
    lines += ["",
              "Find the official price list of the provider and the model page with the context window. Use the",
              "official page first; a third-party page only when none is found, and say so.",
              "Quote the line that gives each rate. If a price steps up with the size of the prompt, give one tier for",
              "each step (the step is in tokens; say whether the page counts cache reads and writes in it).",
              "The context window is the prompt size the model accepts, from the model page.",
              "If a rate cannot be found, leave it null and say so. Do not guess, and do not copy another model's rate.",
              "",
              "Write the profile as JSON to a file, in this form:", "", template(model), "",
              "Check and save it:", "  python -m breadcrumbs --home %s cost set FILE" % home,
              "Then show it:", "  python -m breadcrumbs --home %s cost show %s" % (home, model)]
    return "\n".join(lines)


def _write(root, models):
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "costs.json")
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".costs.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"models": models}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    _cache.clear()
    return path


def _saved_objects(root):
    """The objects of <root>/costs.json as the file holds them (the ones that check; others are left out)."""
    try:
        with open(os.path.join(root, "costs.json"), encoding="utf-8") as f:
            data = json.load(f)
        models = data.get("models")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(models, dict):
        return {}
    return {k: v for k, v in models.items() if validate(v)[0] is not None}


def set_profile(root, model, raw):
    """Check raw (a profile object) and save it under model in <root>/costs.json.
    (True, summary lines) or (False, problems)."""
    prof, bad = validate(raw)
    if bad:
        return False, bad
    models = _saved_objects(root)
    models[model] = to_file(prof)
    _write(root, models)
    lines = ["saved %s in %s" % (model, os.path.join(root, "costs.json"))]
    for ptok in (50_000, 150_000):
        r = tier_for(prof, ptok)
        lines.append("  a prompt of %s tokens pays: input %g, output %g, cache read %g, cache write %g (USD per 1M)"
                     % (format(ptok, ","), r["input"], r["output"], r["cache_read"], r["cache_write"]))
    return True, lines


def remove(root, model):
    models = _saved_objects(root)
    if model not in models:
        return False
    del models[model]
    _write(root, models)
    return True


def show(root=None, model=None):
    """The lines of `cost show`: every profile (or the one of model), with its tiers, window and sources."""
    table, notes = profiles(root)
    out = []
    keys = [model] if model else list(table)
    for key in keys:
        name, prof = lookup(key, root) if model else (key, table.get(key))
        if prof is None:
            out.append("%s: no profile (the harness's own table gives the price)" % key)
            continue
        out.append("%s  (%s)" % (name, "in costs.json" if name in _saved_names(root) else "built in"))
        for upto, r in prof["tiers"]:
            out.append("  %-18s input %g  output %g  cache read %g  cache write %g  (USD per 1M)"
                       % ("up to %s" % format(upto, ",") if upto else "above", r["input"], r["output"],
                          r["cache_read"], r["cache_write"]))
        if prof.get("context_window"):
            out.append("  context window %s tokens" % format(prof["context_window"], ","))
        for s in prof["sources"]:
            out.append("  source %s (retrieved %s)" % (s["url"], s["retrieved"]))
    out += ["problem: %s" % n for n in notes]
    return out or ["no profiles"]


def _harness_price(model, provider):
    from . import ports
    price = ports.get("price")
    if price is None:
        return None
    try:
        r = price(model, provider)
    except Exception:  # a harness price that fails is no price for the brief
        return None
    return r if isinstance(r, tuple) and len(r) == 3 else None


def command(args, home):
    """`python -m breadcrumbs cost show|fetch|template|set|remove` (see the module doc)."""
    if args.action == "template":
        print(template(args.model or ""))
        return 0
    if args.action == "show":
        print("\n".join(show(home, args.model)))
        return 0
    if args.action == "fetch":
        if not args.model:
            print("cost fetch needs a model name", file=sys.stderr)
            return 2
        print(fetch_brief(args.model, home, _harness_price(args.model, getattr(args, "provider", None))))
        return 0
    if args.action == "remove":
        ok = bool(args.model) and remove(home, args.model)
        print("removed %s" % args.model if ok else "no saved profile for %s" % args.model)
        return 0 if ok else 1
    if args.action == "set":
        try:
            raw = json.load(sys.stdin if args.file == "-" else open(args.file, encoding="utf-8"))
        except (OSError, ValueError) as e:
            print("cost set: cannot read %s: %s" % (args.file, e), file=sys.stderr)
            return 2
        if isinstance(raw, dict) and isinstance(raw.get("models"), dict):
            models = raw["models"]
        else:
            if not args.model:
                print("cost set: a bare profile needs the model name (cost set FILE MODEL)", file=sys.stderr)
                return 2
            models = {args.model: raw}
        status = 0
        for name, body in models.items():
            ok, lines = set_profile(home, name, body)
            if ok:
                print("\n".join(lines))
            else:
                print("\n".join("refused %s: %s" % (name, b) for b in lines), file=sys.stderr)
            status = status or (0 if ok else 1)
        return status
    print("cost: unknown action %s" % args.action, file=sys.stderr)
    return 2
