"""finder: the text of a search, which is the same for `python -m breadcrumbs find` and for the tool crumb_find of the
Hermes provider. The hits go under their session handles, each with its best matching line. This module is in the layer
common; the turn budget that cuts a long answer is a part of the harness (hermes.py).
"""
import re

from .crumbs import get_crumb
from .history import _ref_name
from .index import NEIGHBOR

NO_MATCH = "(no match)\n"


def hit_lines(store, index, refs, zoom, query, k=8, nb=NEIGHBOR, exclude_session=None):
    """The lines of the answer: [(ref, level, line)], the hits of each session after the handle of the session.
    level is 0.0 for a handle and 1.0 for a best line. nb: the weight of the neighbor smoothing of Index.search (the
    harness gives its dial: the port "neighbor"; with none, index.NEIGHBOR)."""
    words = set(re.findall(r"\w+", query.lower()))
    groups, hits, parts = {}, 0, {}
    for crumb_id, ref, _score in index.search(query, 3 * k, nb=nb):
        sid = get_crumb(store, crumb_id).meta.get("sess")
        if exclude_session and sid == exclude_session:
            continue
        sess = refs.get(_ref_name("sess-", sid)) if sid else None
        if sess not in groups:
            groups[sess] = [zoom.handle(sess)] if sess else []
            parts[sess] = [(sess, 0.0)] if sess else []
        out = groups[sess]
        out.append(zoom.handle(crumb_id, depth=1 if sess else 0))
        parts[sess].append((crumb_id, 0.0))
        doc, _, span = ref.partition(":")
        a, _, b = span.partition("-")
        lines = zoom._doc_lines(doc)[int(a or 0):int(b) if b else None]
        j = max(range(len(lines)), default=None,
                key=lambda i: len(words & set(re.findall(r"\w+", lines[i].decode("utf-8", "replace").lower()))))
        if j is not None and lines[j].strip():
            out.append(lines[j].decode("utf-8", "replace").rstrip("\n") + "\n")
            parts[sess].append(("%s:%d-%d" % (doc, int(a or 0) + j, int(a or 0) + j + 1), 1.0))
        hits += 1
        if hits >= k:
            break
    return [(r, lv, line) for sess, out in groups.items() for (r, lv), line in zip(parts[sess], out)]


def find(store, index, refs, zoom, query, k=8, nb=NEIGHBOR):
    """The text of the answer: the hits under their session handles, or "(no match)"."""
    lines = hit_lines(store, index, refs, zoom, query, k, nb)
    return "".join(line for _, _, line in lines) if lines else NO_MATCH
