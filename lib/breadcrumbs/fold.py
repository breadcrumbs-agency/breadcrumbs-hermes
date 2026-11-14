"""fold: the policy that keeps a session under the 100,000-token step of its price, for any harness.

Anthropic bills every prompt token above the step at 5 times the price (Claude Haiku 5.5), so a session keeps its prompt at
or under CEILING. A harness reads its own transcript (the prompt tokens and the model of the last request: the input of
that request) and asks this module what to do:

    post_result(tokens, model, response, env)  the result to send in place of a tool's, thinned to the room that is left;
                                               None when the result goes as it is
    block_reason(tokens, model, env)           why the next prompt is not sent (the prompt is at the ceiling); None: it goes

What the policy cannot do: take out the history that is already in the context. A harness that cannot change its own
request (Claude Code gives hooks no such change) keeps the history only by /compact, and the notes below say so.
"""
import re

CEILING = 94_000  # Haiku 5.5: the step is 100,000 prompt tokens; the 5.9% under-read of the estimate keeps a request under it
RESERVE = 4_000  # room kept for the model's own output in the next request
BYTES_PER_TOKEN = 4  # the estimate of zoom.py
HAIKU = re.compile(r"haiku-5[.-]5", re.I)  # claude-haiku-5-5 (and claude-haiku-5.5)


def ceiling(model, env):
    """The most prompt tokens a request may hold on this model, or None when the model has no step to keep under.
    BREADCRUMBS_FOLD_CEILING (a whole number) sets it for any model; the default applies to Haiku 5.5 only."""
    raw = str(env.get("BREADCRUMBS_FOLD_CEILING", "")).strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return CEILING if model and HAIKU.search(str(model)) else None


def thin(text, room):
    """text cut to `room` tokens, at a line end when there is one, with a note of what was cut. A room of 0 keeps none of
    the text, only the note."""
    data = text.encode("utf-8")
    limit = max(0, room) * BYTES_PER_TOKEN
    if len(data) <= limit:
        return text
    kept = data[:limit].decode("utf-8", "ignore")
    if "\n" in kept:
        kept = kept[:kept.rfind("\n")]
    return ("%s\n[breadcrumbs: this result was thinned to the room of the 100,000-token step of Haiku (%d of %d bytes kept). "
            "/compact folds the history and frees room.]" % (kept, len(kept.encode("utf-8")), len(data)))


def shrink(response, room):
    """The result with its text thinned to `room` tokens, in the shape it came in: a string, a content list, or a dict with
    a content list of text parts. None when nothing is cut or the shape is none of these."""
    if isinstance(response, str):
        new = thin(response, room)
        return new if new != response else None
    items = response.get("content") if isinstance(response, dict) else response
    if not isinstance(items, list):
        return None
    done, out = False, []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            new = thin(item["text"], room)
            done = done or new != item["text"]
            out.append(dict(item, text=new))
        else:
            out.append(item)
    if not done:
        return None
    return dict(response, content=out) if isinstance(response, dict) else out


def post_result(tokens, model, response, env):
    """The result to send in place of a tool's, when the prompt is at the ceiling: thinned to the room that is left. None
    when the model has no step, the prompt is under the ceiling less the reserve, or nothing needs cutting."""
    cap = ceiling(model, env)
    if cap is None or tokens + RESERVE < cap:
        return None
    return shrink(response, cap - tokens - RESERVE)


def block_reason(tokens, model, env):
    """Why the next prompt is not sent: the prompt is at the ceiling. None when it may go."""
    cap = ceiling(model, env)
    if cap is None or tokens < cap:
        return None
    return ("This Haiku session holds %s prompt tokens, at the 100,000-token step of the price, so the prompt was not sent. "
            "Run /compact to fold the history, or switch the model with /model, then send it again." % format(tokens, ","))
