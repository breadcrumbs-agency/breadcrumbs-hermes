"""ACE (adaptive context engineering): a Hermes context engine over breadcrumbs.

Hermes sends every request through select_context(). While a request is under the target, ACE leaves it
alone. When it passes the target (200k tokens by default, or Hermes's own threshold or the model's ace_ceiling
when one is lower), ACE folds the older turns into one layer of crumbs and keeps the newest turns exact:

    system prompt | the layer + the first kept user message | the kept turns, byte for byte

A turn can pass the target alone (an agent that runs a hundred tools). Then its own first steps fold
too: its user message stays, with the layer, and its newest steps stay exact back to a step start (an
assistant message, so no tool result loses its call). The size counts the text, the tool calls and the
thinking that goes back with each message; the model's own count of the request before (prompt_tokens)
sets how many tokens a byte is. A fold makes the request FOLD_TO of the target, so the layer then holds
while the chat grows. Each request writes one line to <root>/ace.log. A new engine object has no count yet:
it reads the last counts of its session back from that log (logged_counts).
The layer costs fewer tokens per byte than the messages that it folds, and a fold fills a little less than
its aim, so the model counts a folded request under its aim (60 folds in a row on Nov 9 2026: a median of
0.709). The gain (that count over the aim) sets the next fold's aim: with it the median was 1.004. Each fold
measures the gain again with the calibration of its own time, because the calibration learns the cheap layer
too (_fold). When the tool calls since the last fold did no work
(is_work: they only read; read, fold, read again is a loop), the target grows by RATCHET in place of a new
fold, up to Hermes's threshold. The log keeps the raised target and the gain for a new object.
TARGET_TOKENS, FOLD_TO, FOLD_CAP and RATCHET are defaults: each model has its own dials in <root>/config.json
"models" (breadcrumbs/dials.py), next to its temperature (Engine._dial). Reading grows the target, with no ceiling but
Hermes's threshold and the model's ace_ceiling (Claude Haiku 5.5: 94,000 built in, from the 100,000 prompt
tokens where its price steps up); work tapers it back to the band by the fold share (_taper).

The layer is a tree: groups of GROUP_TURNS turns, then one crumb per message. Each message is stored
exactly (put_text: the same bytes give the same id, so a text that comes back is one crumb, marked x2).
The layer shows each crumb at a level from 0 (its handle) to 1 (its exact text), with the zoom's distinct
and meaningful words in between: the meat of it. The layer has a finite size: LAYER_SHARE of a
fold, and at a target of SMALL_TARGET tokens or less only SMALL_SHARES[1], because there exact text
recalls more (bench/ace_paired.py on LongMemEval-S). Weights set the levels: recency, role, the words of the new message (an old crumb that the new
question needs is relevant but too thin, so it gets more room and a \u25c6 mark; the words count by how rare
they are in the layer), and later mentions of its names and
numbers. A crumb that the model opens with crumb(ref, level) keeps at least that level in later layers,
and its own group pays for it first: growing one crumb shrinks the crumbs around it. When the handles
alone do not fit, the oldest groups fold into one crumb each. ACE folds again only when the request
passes the target again, so between folds the layer bytes stay the same and the prompt cache holds.
compress() makes the same fold on the stored history, with no model call.

Turn it on for a profile: hermes -p PROFILE config set context.engine breadcrumbs
(memory.provider breadcrumbs gives the crumb tool that opens the handles).
"""

import os
import re

try:  # inside Hermes
    from agent.context_engine import ContextEngine
except ImportError:  # tests and the CLI
    ContextEngine = object

from breadcrumbs.ace_core import (  # the engine, and the names that the tests and the other modules of Hermes read
    DIAL_DEFAULTS, Engine, FOLD_CAP, FOLD_TO, GAP, HANDLE, LAYER_HEAD, LEVELS, LOG_BYTES, LOG_RE, LOG_SESS, RATCHET,
    REF_RE, SMALL_TARGET, TARGET_TOKENS, TPB, allocate, cost, crumb_text, is_work, join, layer_cost, logged_counts,
    logged_ratchet, message_text, ref_of, request_bytes, split, status_lines)


def context_window(model, context_length):
    """The context window for a model: the one of its cost profile (breadcrumbs/costs.py) when Hermes gives none, or a
    smaller one (its metadata of a model it does not know: 256K, say). A larger one that Hermes gives stands, so an
    override of the config is not lowered. Hermes's value otherwise."""
    try:
        from breadcrumbs import costs
        from breadcrumbs.cli import default_home
        window = costs.context_window(model, default_home())
    except Exception:  # no profile to read: Hermes's value
        window = None
    if window and (not context_length or int(context_length) < window):
        return window
    return context_length

# The public names of this module: its own, and the names of breadcrumbs.ace_core that it re-exports (the tests and the
# other modules of Hermes read them here).
__all__ = ["Ace", "BLOCK_NAMES_CRUMB", "CHILD_PLATFORM", "DIAL_DEFAULTS", "Engine", "FOLD_CAP", "FOLD_TO", "GAP", "HANDLE",
           "LAYER_HEAD", "LEVELS", "LOG_BYTES", "LOG_RE", "LOG_SESS", "RATCHET", "REF_RE", "SMALL_TARGET", "TARGET_TOKENS",
           "TPB", "allocate", "cost", "crumb_text", "is_work", "join", "layer_cost", "logged_counts", "logged_ratchet",
           "message_text", "ref_of", "request_bytes", "split", "status_lines", "setting", "wanted"]


# A delegate_task child runs on this platform and with no memory provider, so with no crumb tool (a live run, Nov 10
# 2026: it folded at request 4 of 10). The line is the one of the provider's prompt block (hermes.SYSTEM_BLOCK) that
# names the tool; the provider adds the block and its tools under one gate, so a prompt without the line means a
# model without the tool.
CHILD_PLATFORM = "subagent"
BLOCK_NAMES_CRUMB = "- crumb(id, level): show a breadcrumb"


def setting(home, block, key):
    """A two-level key of <home>/config.yaml (block: key, as Hermes writes it), or None."""
    try:
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    # A column-0 comment does not end a YAML block: Hermes's template puts banners inside `memory:`.
    m = re.search(r"^%s:[ \t]*\n((?:[ \t]+.*\n?|[ \t]*\n|#.*\n)*)" % block, text, re.M)
    v = m and re.search(r"^[ \t]+%s:[ \t]*['\"]?([^'\"\s#]+)" % key, m.group(1), re.M)
    return v.group(1) if v else None


def wanted(home=None, name="breadcrumbs"):
    """True when the profile's config.yaml selects this engine (context.engine: the name of the plugin
    that holds it). Hermes runs one context-engine plugin per process, so an unselected ACE must not take
    the seat."""
    return setting(home or _home(), "context", "engine") == name


def _home():
    try:
        from hermes_constants import get_hermes_home  # profile-scoped inside Hermes
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


class Ace(Engine, ContextEngine):
    """The breadcrumb layer as a Hermes context engine (see the module doc): the engine of breadcrumbs.ace_core, with
    Hermes's session and model callbacks."""
    protect_first_n, protect_last_n = 3, 6

    def update_from_response(self, usage):
        usage = usage or {}
        self.counted(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
                     int(usage.get("total_tokens") or 0))

    def update_model(self, model, context_length, base_url="", api_key="", provider="", api_mode=""):
        self.use_model(model, context_window(str(model or ""), context_length))
        if ContextEngine is not object:  # Hermes's own fields of the window (its thresholds per model)
            super().update_model(model, self.context_length, base_url, api_key, provider, api_mode)

    def on_session_start(self, session_id, **kwargs):
        # Hermes's init call names agent.platform; its session-transition call names the session source (run_agent.py,
        # session_source_for), which can differ from it. A child never becomes a primary session, so it stays a child.
        self.child = self.child or kwargs.get("platform") == CHILD_PLATFORM
        self.start_session(session_id, kwargs.get("hermes_home"))

    def on_session_reset(self):
        if ContextEngine is not object:
            super().on_session_reset()
        self.reset_session()

    def clone_for_agent(self):  # a fresh instance: the store opens per agent
        return Ace(self.target_tokens if self._given else None, self._name)

    def home_folder(self):
        return str(self.home or _home())

    def _cannot_open(self, head):
        """True for a delegate_task child whose system prompt does not name the crumb tool. Hermes gives
        select_context the messages and no tool list, so this reads the two facts that it does give: the platform
        (on_session_start) and the system prompt (the system messages before the first user message). A child that
        folded in a live run got a layer that tells it to open handles with crumb(ref, level) and had no such tool.
        Any other agent folds as before (its prompt can predate its provider, and its tools may come from elsewhere,
        as an MCP server). compress() is not guarded: it answers Hermes's own threshold, the last defense against a
        full window."""
        if not self.child:
            return False
        return not any(BLOCK_NAMES_CRUMB in message_text(m) for m in head if m.get("role") == "system")
