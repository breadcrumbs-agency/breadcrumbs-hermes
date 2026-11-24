"""Imported by the tests that read a state.db: hermes_ingest reads it only through Hermes's agent.redact, so where Hermes
is not importable this puts a stand-in of agent.redact in its place. The stand-in masks one key shape (sk-proj-),
enough to show that a text read from state.db goes through the scrub. tests/hermes_integration.py runs Hermes's own."""
import importlib.util
import re
import sys
import types

KEY = re.compile(r"sk-proj-[A-Za-z0-9_-]{8,}")


def redact_for_egress(text, urls=False):
    return KEY.sub(lambda m: m.group()[:6] + "..." + m.group()[-4:], text)


try:
    HERMES = importlib.util.find_spec("agent.redact") is not None
except ImportError:  # an "agent" that is no package
    HERMES = False
if not HERMES:
    agent = sys.modules.setdefault("agent", types.ModuleType("agent"))
    agent.__path__ = getattr(agent, "__path__", [])
    redact = types.ModuleType("agent.redact")
    setattr(redact, "redact_for_egress", redact_for_egress)
    sys.modules["agent.redact"] = redact
    setattr(agent, "redact", redact)
