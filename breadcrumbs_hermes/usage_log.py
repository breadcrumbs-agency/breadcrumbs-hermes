"""usage.log: one line of numbers for every model request, in every chat.

Hermes keeps no history of the tokens of each request: state.db holds totals per chat. It gives plugins two observer
hooks instead. post_api_request fires after each raw provider request of a chat. post_auxiliary_call fires after
each helper call (titles, compression, approval, vision). Each hook appends one line to <profile home>/breadcrumbs/
usage.log, the folder of ace.log, so a chat that runs no ACE has its record too. One line (here cut in two):

    2026-11-10T15:40:12 sess=<id> turn=<id> req=<id> n=<call> task=<main|aux task> model=<name> resp=<name> prov=<name>
    in=<n> cr=<n> cw=<n> out=<n> rsn=<n> prompt=<n> est=<n> msgs=<n> tools=<n> dur=<seconds> fin=<reason>

The stamp is local time, as in ace.log. sess is the full session id (it joins state.db). in, cr, cw, out and rsn are
the tokens that the provider counted: input, cache read, cache write, output, reasoning. prompt is the size of the
context that it counted (in + cr + cw). est is Hermes's own estimate before the request, msgs and tools are the
messages and the tools sent, dur is the seconds that the call took, fin is the finish reason (error: a helper call
that raised). Hermes sends est and tools to the pre_* hooks and not to the post_* hooks (checked in agent/
turn_response_intake.py and agent/auxiliary_hooks.py): they read 0 until a Hermes passes them. A value never holds a
space. A missing id or name is "-" and a missing number is 0.

Ids, names and numbers only. The request, the response, the error text and the base_url are never read into a line:
format_line() takes the named fields and nothing else. BREADCRUMBS_USAGE_LOG=0 writes nothing. The file turns over
to usage.log.1 at LOG_BYTES, as ace.log does. A hook never raises.

The format of a line is common's (breadcrumbs/usage.py line()): `ingest` of another harness adds the requests of its
sessions to the usage.log of the store, each line with src=<harness> at its end.
"""
import logging
import os
import threading
import time
from collections.abc import Mapping

from breadcrumbs import usage
from breadcrumbs.usage import LOG_NAME, WORD_MAX, count as _count  # noqa: F401  (the format of a line is common's)

LOG_BYTES = 1 << 20              # usage.log turns over to usage.log.1 at this size, as ace.log does
OFF = "BREADCRUMBS_USAGE_LOG"    # "0" writes nothing
HOOKS = ("post_api_request", "post_auxiliary_call")
USAGE_KEYS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")

_LOCK = threading.Lock()         # Hermes runs a hook in a worker thread: one append at a time in a process


def enabled():
    return os.environ.get(OFF, "").strip() != "0"


def log_path():
    """<profile home>/breadcrumbs/usage.log: the folder of ace.log, in the home of the profile that runs."""
    from .ace import _home  # imported here: a failure stays inside the guarded hook, never in the plugin load
    return os.path.join(_home(), "breadcrumbs", LOG_NAME)


def _stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _field(obj, name):
    if isinstance(obj, Mapping):
        return obj.get(name)
    return None if obj is None else getattr(obj, name, None)


def format_line(kw, task, failed=False):
    """The line (no newline) for one call: the named fields of kw and nothing else, in the format of breadcrumbs/usage.py.
    task is "main" or the aux task. failed: a helper call that raised (fin=error, whatever else kw says)."""
    use = kw.get("usage")  # a dict, or None for a call with no usage
    inp, cr, cw, out, rsn = (_count(_field(use, k)) for k in USAGE_KEYS)
    given = _field(use, "prompt_tokens")
    return usage.line({
        "sess": kw.get("session_id"), "turn": kw.get("turn_id"), "req": kw.get("api_request_id"),
        "n": kw.get("api_call_count"), "task": task, "model": kw.get("model"), "resp": kw.get("response_model"),
        "prov": kw.get("provider"), "in": inp, "cr": cr, "cw": cw, "out": out, "rsn": rsn,
        "prompt": _count(given) if given is not None else inp + cr + cw, "est": kw.get("approx_input_tokens"),
        "msgs": kw.get("message_count"), "tools": kw.get("tool_count"), "dur": kw.get("api_duration"),
        "fin": "error" if failed else kw.get("finish_reason")}, _stamp())


def _append(path, data):
    """One write call of the whole line. The folder is made at the first line (a profile without ACE has none)."""
    try:
        f = open(path, "ab", buffering=0)
    except FileNotFoundError:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "ab", buffering=0)
    with f:
        f.write(data)


def record(line):
    """Append one line to usage.log. At LOG_BYTES the file turns over to usage.log.1 first, as ace.log does."""
    path = log_path()
    data = (line + "\n").encode("utf-8")
    with _LOCK:
        try:
            if os.path.getsize(path) > LOG_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        _append(path, data)


def api_line(kw):
    """The line of a post_api_request payload."""
    return format_line(kw, "main")


def aux_line(kw):
    """The line of a post_auxiliary_call payload. error and error_type only say that the call failed: their text
    is never read into the line."""
    return format_line(kw, kw.get("aux_task"), failed=bool(kw.get("error") or kw.get("error_type")))


def post_api_request(**kw):
    """After each raw provider request of a chat: one line, task=main."""
    if enabled():
        record(api_line(kw))


def post_auxiliary_call(**kw):
    """After each helper call: one line, task=<aux_task>. A call that raised writes its line too (zeros, fin=error)."""
    if enabled():
        record(aux_line(kw))


def _guarded(fn):
    def hook(**kw):
        try:
            return fn(**kw)
        except Exception as e:  # the record never breaks a model call; the type only: a message may hold text
            logging.getLogger(__name__).debug("breadcrumbs usage log: %s failed (%s)", fn.__name__, type(e).__name__)
            return None
    hook.__name__ = fn.__name__
    return hook


def register(ctx):
    """The two observer hooks, where the Hermes context has register_hook. Returns the names registered."""
    done = []
    if hasattr(ctx, "register_hook"):
        for name, fn in zip(HOOKS, (post_api_request, post_auxiliary_call)):
            try:
                ctx.register_hook(name, _guarded(fn))
                done.append(name)
            except Exception as e:  # a Hermes that refuses a hook name still loads the plugin
                logging.getLogger(__name__).debug("breadcrumbs usage log: no %s (%s)", name, type(e).__name__)
    return done
