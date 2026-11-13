"""The request options of a model on Hermes's open wire: the keys of a "models" entry of config.json that are no dial.

The dials and the file are breadcrumbs/dials.py (common). A "models" entry also holds the request options of a model
(temperature, top_p, top_k, ...): any key that is no dial and does not start with "ace_". Hermes sends no temperature
of its own on the OpenAI chat-completions wire. The main agent passes none to the transport, and
agent/transports/chat_completions.py _base_kwargs reads one only from a provider profile's fixed value. register()
adds Hermes's public llm_request middleware (developer-guide/middleware), so the options go into each
chat-completions request of a chat after Hermes builds it: a key of the OpenAI spec at the top level, any other
sampler key (top_k, min_p, repetition_penalty, ...) in extra_body, and a null removes the key. Hermes runs that
middleware on the requests of a chat, not on its helper calls (titles, compression, vision). Only this open wire
changes, and only for a provider that you call with an API key (or none). The Anthropic wire, and each provider that
logs in with OAuth (oauth()), keep the request that Hermes builds: their terms can forbid a change. The file is read
at each request, so a change applies to the next request.
"""
import logging
import os

from breadcrumbs import dials

log = logging.getLogger(__name__)

# The top-level request keys of the OpenAI chat-completions spec that a model option can set.
SPEC = frozenset(("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed", "stop", "logit_bias",
                  "logprobs", "top_logprobs", "n", "max_tokens", "max_completion_tokens", "reasoning_effort",
                  "service_tier", "parallel_tool_calls", "verbosity", "user"))


def apply(kwargs, opts):
    """Put the request options of opts into chat-completions kwargs. The dials stay out."""
    extra = dict(kwargs.get("extra_body") or {})
    for key, value in opts.items():
        if key in dials.BY_KEY or key.startswith("ace_"):
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


def oauth(provider):
    """True when the request goes to a provider that logs in with OAuth (nous, qwen-oauth, solstice on this
    wire). Its terms can forbid a changed request, so it keeps the request that Hermes builds. Hermes's provider
    profile of that name (the public module providers) names its auth type."""
    name = str(provider or "").strip().lower()
    try:
        from providers import get_provider_profile
        prof = get_provider_profile(name) if name else None
    except Exception:  # not inside Hermes, or its lookup failed: no profile, so no OAuth
        prof = None
    kinds = [getattr(prof, "auth_type", ""), getattr(prof, "registry_auth_type", "")]
    return any(str(k or "").startswith("oauth") for k in kinds)


def middleware(home):
    """The llm_request middleware: the options of the model in "models" go into a chat-completions request.
    home() gives the HERMES_HOME at the time of the request. None keeps the request that Hermes built."""

    def llm_request(request=None, model=None, provider=None, api_mode=None, **_):
        if api_mode != "chat_completions" or not isinstance(request, dict):
            return None
        try:
            if oauth(provider):
                return None
            opts = dials.options(dials.config(os.path.join(home(), "breadcrumbs")), model or request.get("model"))
            if not opts:
                return None
            return {"request": apply(dict(request), opts), "source": "breadcrumbs",
                    "reason": "the request options of the model in config.json (options.py)"}
        except Exception:  # a bad option must not stop the request
            log.warning("breadcrumbs: the model options for %s were not applied", model, exc_info=True)
            return None

    return llm_request


def register(ctx, home):
    """Add the middleware where the Hermes context has register_middleware. True when it was added."""
    if not hasattr(ctx, "register_middleware"):
        return False
    try:
        ctx.register_middleware("llm_request", middleware(home))
    except Exception as e:  # a Hermes that refuses it still loads the plugin
        log.debug("breadcrumbs: no llm_request middleware (%s)", type(e).__name__)
        return False
    return True
