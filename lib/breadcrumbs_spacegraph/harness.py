"""harness: which program a view is of, for the icon that sits beside the brand.

Each harness gives its icon to the harness table of common (ports.harness, icon=): Hermes and Claude Code give their own
mark, and a harness with none gets its first letter on a tile (monogram). A view says which harness it shows, by these
steps, and never guesses past them:

  1. the harness that the opener names, when the table has it: the `h` of the address (viz.run(harness=...)), else the
     environment variable BREADCRUMBS_HARNESS. A name is a key of the table ("claude-code"), its words ("claude"), or
     its label, in any case;
  2. else, when this process loaded exactly one harness (the Hermes plugin loads Hermes, the Claude plugin loads Claude
     Code), that one;
  3. else none. A name that the table does not have is none too: the harness of the opener is not loaded here, and
     another one's mark would be wrong.

The page asks `/api/harness?h=` and draws {here} beside the brand. The answer holds the whole table, so that a later
view can mark each session with the icon of its own harness.
"""
import base64
import colorsys
import hashlib
import html
import os
from urllib.parse import quote

from breadcrumbs import ports

ENV = "BREADCRUMBS_HARNESS"


def monogram(key, label):
    """The icon of a harness that gave none: the first letter of its label, white on a tile whose hue comes from its key.
    A data: URI of an SVG (the same hue on every run and every machine)."""
    hue = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 360 / 360.0
    r, g, b = (round(255 * c) for c in colorsys.hls_to_rgb(hue, 0.36, 0.45))
    letter = html.escape((label or key or "?").strip()[:1].upper() or "?")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="5.5" '
           'fill="#%02x%02x%02x"/><text x="12" y="17" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
           'font-size="14" font-weight="700" fill="#fff" text-anchor="middle">%s</text></svg>') % (r, g, b, letter)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def table():
    """[{key, label, icon, repository?, pages?}] for each loaded harness, in table order.

    Every entry has an icon: the one that the harness gave, else its monogram. Optional repository and Pages URLs let
    the explorer put the harness's project links beside the top-left mark.
    """
    return [{"key": key, "label": entry["label"], "icon": entry.get("icon") or monogram(key, entry["label"]),
             **({k: entry[k] for k in ("repository", "pages") if entry.get(k)})}
            for key, entry in ports.harnesses().items()]


def resolve(name):
    """The key of the harness that `name` names, or None. A key of the table, one of its words (`match`), or its label,
    in any case."""
    name = (name or "").strip().lower()
    if not name:
        return None
    for key, entry in ports.harnesses().items():
        if name in (key.lower(), entry["label"].lower()) or name in entry["match"]:
            return key
    return None


def hint(harness=None, env=os.environ):
    """The harness that opens a view: the one given, else BREADCRUMBS_HARNESS, else None."""
    return (harness or env.get(ENV) or "").strip() or None


def fragment(harness=None, env=os.environ):
    """What an opener adds to the address of the page so that it names its harness: "&h=<name>", or "" with none."""
    named = hint(harness, env)
    return "&h=" + quote(named, safe="") if named else ""


def info(want=None, env=os.environ):
    """The answer of /api/harness: {"here": the key of the harness that this view shows, or None, "harnesses": table()}.
    `want` is the `h` of the address, else BREADCRUMBS_HARNESS of this process (see the steps above)."""
    rows = table()
    named = (want or "").strip() or (env.get(ENV) or "").strip()
    if named:
        here = resolve(named)
    else:
        here = rows[0]["key"] if len(rows) == 1 else None
    return {"here": here, "harnesses": rows}
