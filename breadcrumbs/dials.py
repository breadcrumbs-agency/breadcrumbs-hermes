"""The knobs and dials of breadcrumbs: one table, DIALS, and the code that reads it.

A dial is set in <HERMES_HOME>/breadcrumbs/config.json (each profile has its own file):

  {"ace_tokens": 200000,
   "models": {"*nemotron*": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "ace_tokens": 290000,
                             "ace_fold_to": 0.9},
              "*":          {"ace_ratchet": 1.0}}}

A value comes from, in this order (the last one that sets the dial wins): the default, the top level of the
file, each glob of "models" that matches the model name (in file order), and the environment variable of the
dial. A value that is not valid (a wrong type, or out of its range) gives the default, and an environment
variable that is not valid is ignored; `python -m breadcrumbs dials` says so, as it does for a key that is no
dial (a typing mistake). `python -m breadcrumbs dials` shows every
dial with its value and where the value came from. README "Knobs and dials" holds the same table; a test keeps
the two equal.

A "models" entry may also hold the request options of a model (temperature, top_p, top_k, ...): any key that is no
dial and does not start with "ace_". This release does not send them. It changes no request that Hermes builds
and replaces no code of Hermes. The file keeps such keys, and `python -m breadcrumbs dials` names them as options
that are not sent (unsent()).
"""
import fnmatch
import json
import os
from collections import namedtuple

# The top-level request keys of the OpenAI chat-completions spec that a model option can set.
SPEC = frozenset(("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed", "stop", "logit_bias",
                  "logprobs", "top_logprobs", "n", "max_tokens", "max_completion_tokens", "reasoning_effort",
                  "service_tier", "parallel_tool_calls", "verbosity", "user"))

# key, default, kind (int, float, bool), lo, hi (None: no bound), group, environment variable, meaning
Dial = namedtuple("Dial", "key default kind lo hi group env doc")

DIALS = (
    Dial("ace_tokens", 200_000, "int", 1, None, "ACE (per model)", "BREADCRUMBS_ACE_TOKENS",
         "The normal size in tokens at which ACE folds a chat. Reading may grow the target above it; work tapers "
         "it back to it."),
    Dial("ace_fold_to", 0.9, "float", 0.1, 1.0, "ACE (per model)", None,
         "A fold leaves the request at this share of the target: 0.9 cuts 10%, 20k at a target of 200k. The "
         "same 10% tapers a raised target back to ace_tokens, one step in each interval of work."),
    Dial("ace_fold_cap", 1.35, "float", 0.1, 2.0, "ACE (per model)", None,
         "A fold aims at most at this share of the target, by the estimate (the estimate reads high)."),
    Dial("ace_ratchet", 1.45, "float", 1.0, 4.0, "ACE (per model)", None,
         "A fold interval in which the tools only read raises the target by this factor, so that a model that "
         "reads a lot of data keeps it in one place (fewer folds, fewer layers). There is no ceiling but Hermes's "
         "threshold. 1.0: never."),
    Dial("ace_diverse", 4, "int", 0, None, "ACE (per model)", None,
         "An interval in which the tool calls write, or use this many different tools, is work and not "
         "reading: the raised target tapers back. 0: only a write counts."),
    Dial("prefetch_tokens", 300, "int", 1, None, "Recall", "BREADCRUMBS_PREFETCH_TOKENS",
         "The first budget of the recall text in each turn, in tokens. The controller then adapts it, between "
         "budget_min and budget_max (it is saved in horizon.json)."),
    Dial("recall_share", 0.15, "float", 0.0, 1.0, "Recall", "BREADCRUMBS_RECALL_SHARE",
         "A turn overflows when the recall text is more than this share of the context."),
    Dial("neighbor", 0.3, "float", 0.0, 1.0, "Recall", "BREADCRUMBS_NEIGHBOR",
         "The weight of dialogue-neighbor smoothing in search: a turn next to a strong hit gets this share of "
         "the hit's score. 0: off."),
    Dial("turn_factor", 0.0, "float", 0.0, 64.0, "Recall", "BREADCRUMBS_TURN_FACTOR",
         "One memory budget for each user turn: recall, find and open share this many recall budgets. "
         "0: no turn budget."),
    Dial("age_half_days", 30.0, "float", 0.1, None, "Recall", "BREADCRUMBS_AGE_HALF_DAYS",
         "The priority of a crumb in recall halves in this many days of age."),
    Dial("idle_half_days", 7.0, "float", 0.1, None, "Recall", "BREADCRUMBS_IDLE_HALF_DAYS",
         "And it halves in this many days since the model last opened it."),
    Dial("budget_min", 40, "int", 1, None, "Controller", None,
         "The smallest recall budget that the controller sets, in tokens."),
    Dial("budget_max", 2000, "int", 1, None, "Controller", None,
         "The largest recall budget that the controller sets, in tokens."),
    Dial("overflow_low", 0.02, "float", 0.0, 1.0, "Controller", None,
         "The controller raises the budget when fewer turns than this share overflow."),
    Dial("overflow_high", 0.05, "float", 0.0, 1.0, "Controller", None,
         "The controller lowers the budget when more turns than this share overflow."),
)
BY_KEY = {d.key: d for d in DIALS}

# Keys of config.json that are no single dial: "models" (dials per model), and two structured settings that
# other modules read.
OTHER_KEYS = {
    "models": "dials per model: a glob of the model name -> any dial above",
    "hive": "other hosts to search: {\"ms\": 150, \"peers\": {...}} (breadcrumbs/hive.py)",
    "slot": "the name of another memory provider that runs through breadcrumbs (breadcrumbs/slot.py)",
}

# Environment variables that are no dial: they are read by the process, not by a profile.
KNOBS = (
    ("BREADCRUMBS_HOME", "<HERMES_HOME>/breadcrumbs", "The directory of the store, for the command line."),
    ("BREADCRUMBS_TOOL_PREFIX", "mcp__", "The prefix of the tool names (`mcp__crumb`). Hermes on the Anthropic OAuth wire needs it."),
    ("BREADCRUMBS_SLOT", "(none)", "The name of another memory provider to run through breadcrumbs. Wins over config.json \"slot\"."),
    ("BREADCRUMBS_SUMMARIZE", "auto", "`0`: never run the small summary model. `auto`: run it when the model and the binary exist."),
    ("BREADCRUMBS_SUM_MODEL", "~/models/Qwen_Qwen3.5-0.8B-Q4_K_M.gguf", "The GGUF file of the summary model."),
    ("BREADCRUMBS_LLAMA", "llama-completion on the PATH", "The llama.cpp binary that runs the summary model."),
    ("BREADCRUMBS_SUM_TEMPLATE", "qwen", "The prompt template of the summary model."),
    ("BREADCRUMBS_CRC32C", "(auto)", "`pure`: use the pure-Python CRC32C even when google-crc32c is installed."),
    ("BREADCRUMBS_GRAALPY", "(auto)", "The GraalPy binary for the GraalPy runs."),
    ("BREADCRUMBS_GRAAL_HEAP", "4g", "The heap of a GraalPy run."),
    ("BREADCRUMBS_IPFS", "ipfs on the PATH", "The kubo binary, for `push`, `pull` and the hive."),
    ("BREADCRUMBS_HERMES_REPO", "~/.hermes/hermes-agent", "The Hermes checkout that `seat` commands read."),
)


def config(root):
    """<root>/config.json as a dict ({} when it is missing or bad)."""
    try:
        with open(os.path.join(root, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def options(cfg, model):
    """The dials of model: each glob of cfg["models"] that matches its name, merged in file order."""
    out, name, models = {}, str(model or "").lower(), cfg.get("models")
    for glob, opts in (models.items() if isinstance(models, dict) else ()):
        if isinstance(opts, dict) and fnmatch.fnmatchcase(name, str(glob).lower()):
            out.update(opts)
    return out


def valid(d, v):
    """The value of dial d, or None when v is no valid value of it (a wrong type, or out of its range)."""
    if d.kind == "bool":
        return v if isinstance(v, bool) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if d.kind == "int" and not isinstance(v, int):
        return None
    if (d.lo is not None and v < d.lo) or (d.hi is not None and v > d.hi):
        return None
    return float(v) if d.kind == "float" else v


def _want(d):
    kind = {"int": "a whole number", "float": "a number", "bool": "true or false"}[d.kind]
    if d.lo is not None and d.hi is not None:
        return "%s from %g to %g" % (kind, d.lo, d.hi)
    if d.lo is not None:
        return "%s of %g or more" % (kind, d.lo)
    return kind


def from_env(d, environ):
    """(value, why) of the environment variable of dial d: (None, "") when it is not set, (None, why) when it
    holds no valid value."""
    raw = environ.get(d.env) if d.env else None
    if raw in (None, ""):
        return None, ""
    try:
        v = valid(d, int(raw) if d.kind == "int" else float(raw))
    except ValueError:
        v = None
    return (v, "") if v is not None else (None, "%s=%r is not %s; ignored" % (d.env, raw, _want(d)))


Row = namedtuple("Row", "dial value source problems")


def resolve(cfg, model=None, environ=None):
    """One Row for each dial: the value that counts, where it came from, and what was ignored on the way."""
    environ = os.environ if environ is None else environ
    cfg = cfg if isinstance(cfg, dict) else {}
    models = cfg.get("models")
    rows = []
    for d in DIALS:
        value, source, bad = d.default, "default", []
        layers = [("config", cfg)]
        for glob, opts in (models.items() if isinstance(models, dict) else ()):
            if isinstance(opts, dict) and fnmatch.fnmatchcase(str(model or "").lower(), str(glob).lower()):
                layers.append(('models["%s"]' % glob, opts))
        for name, layer in layers:
            if d.key in layer:
                v = valid(d, layer[d.key])
                if v is None:  # the layer sets the dial, and wrongly: the default, not the layer before
                    bad.append("%s %s: %r is not %s; the default is used" % (name, d.key, layer[d.key], _want(d)))
                    value, source = d.default, "default"
                else:
                    value, source = v, name
        v, why = from_env(d, environ)
        if v is not None:
            value, source = v, "env " + d.env
        if why:
            bad.append(why)
        rows.append(Row(d, value, source, bad))
    return rows


def value(cfg, model, key, environ=None):
    """The value of one dial (see resolve). environ={} leaves the environment out."""
    return next(r.value for r in resolve(cfg, model, environ) if r.dial.key == key)


def env_value(key):
    """The value of a dial from the environment alone (the default when it is not set or not valid)."""
    return value({}, None, key)


def unknown(cfg):
    """The keys of config.json (and of its "models" entries) that are no dial and no other setting: a typing
    mistake keeps the default without a word otherwise. In a "models" entry a key that does not start with
    "ace_" is a request option (see unsent), not a typing mistake."""
    out = ["config %s: no such dial" % k for k in cfg if k not in BY_KEY and k not in OTHER_KEYS]
    models = cfg.get("models")
    for glob, opts in (models.items() if isinstance(models, dict) else ()):
        if not isinstance(opts, dict):
            out.append('models["%s"]: not an object; ignored' % glob)
            continue
        out += ['models["%s"] %s: no such dial' % (glob, k) for k in opts if k not in BY_KEY and k.startswith("ace_")]
    return out


def unsent(cfg):
    """The request options that "models" entries hold (temperature, top_p, top_k, ...: a key that is no dial and
    does not start with "ace_"). This release does not send them; the file keeps them."""
    models = cfg.get("models")
    return ['models["%s"] %s: a request option; this release does not send it' % (glob, k)
            for glob, opts in (models.items() if isinstance(models, dict) else ()) if isinstance(opts, dict)
            for k in opts if k not in BY_KEY and not k.startswith("ace_")]


def _fmt(v):
    if isinstance(v, bool):
        return str(v).lower()
    return "{:,}".format(v) if isinstance(v, int) else "%g" % v


def show(root, model=None, environ=None):
    """The lines of `python -m breadcrumbs dials`: every dial with its value and source, what was ignored, then
    the environment knobs."""
    environ = os.environ if environ is None else environ
    cfg = config(root)
    rows = resolve(cfg, model, environ)
    lines = ["Dials of %s%s" % (os.path.join(root, "config.json"), "  (model %s)" % model if model else "")]
    group = None
    for r in rows:
        d = r.dial
        if d.group != group:
            group = d.group
            lines += ["", group]
        mark = "" if r.source == "default" else "   <- " + r.source
        lines.append("  %-16s %-10s default %-10s%s" % (d.key, _fmt(r.value), _fmt(d.default), mark))
    notes = [p for r in rows for p in r.problems] + unknown(cfg)
    if notes:
        lines += ["", "Ignored"] + ["  " + p for p in notes]
    options_ = unsent(cfg)
    if options_:
        lines += ["", "Request options (kept in the file, not sent)"] + ["  " + p for p in options_]
    lines += ["", "Environment knobs (read by the process, not by a profile)"]
    for name, default, _doc in KNOBS:
        now = environ.get(name)
        lines.append("  %-26s %s" % (name, "= %s" % now if now else "not set (default %s)" % default))
    return lines


def markdown():
    """The tables of README "Knobs and dials": one row for each dial, then one for each environment knob."""
    out = ["| Dial | Default | Range | Env var | What it does |", "|---|---|---|---|---|"]
    for d in DIALS:
        if d.kind == "bool":
            rng = "true or false"
        elif d.hi is None:
            rng = "%g or more" % d.lo
        else:
            rng = "%g to %g" % (d.lo, d.hi)
        out.append("| `%s` | %s | %s | %s | %s |" % (d.key, _fmt(d.default), rng, "`%s`" % d.env if d.env else "", d.doc))
    out += ["", "| Environment knob | Default | What it does |", "|---|---|---|"]
    out += ["| `%s` | %s | %s |" % (n, dflt, doc) for n, dflt, doc in KNOBS]
    return "\n".join(out)


def apply(kwargs, opts):
    """Put the request options of opts into chat-completions kwargs. The dials stay out. Nothing in this release
    calls it: the options are not sent (unsent)."""
    extra = dict(kwargs.get("extra_body") or {})
    for key, value in opts.items():
        if key in BY_KEY or key.startswith("ace_"):
            continue
        if key == "extra_body" and isinstance(value, dict):
            extra.update(value)
            continue
        where = kwargs if key in SPEC else extra
        if value is None:
            where.pop(key, None)
        else:
            where[key] = value
    if extra:
        kwargs["extra_body"] = extra
    else:
        kwargs.pop("extra_body", None)
    return kwargs
