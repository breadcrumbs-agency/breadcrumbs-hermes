"""Groups of profiles: what profiles share, and what they do not.

Profiles inherit settings, not memory. Every profile reads the default profile's config.json under its own. A profile
that is in a group reads the group's config.json between the two. The crumbs (the memory: store, index, refs) of a
profile stay its own; only the members of one group may read each other's memory, and nothing else is shared.

  <hermes>/breadcrumbs/groups.json                     {"groups": {"<name>": {"profiles": ["a", "b"], "note": ""}}}
  <hermes>/breadcrumbs/groups/<name>/config.json       the settings that the members share

The chain of a chat (workdir.config_folders), the widest first:

  default   <hermes>/breadcrumbs/config.json           every profile but the default itself
  group     <hermes>/breadcrumbs/groups/<name>/        the profile's group, when it has one
  profile   <home>/breadcrumbs/config.json
  scope     <home>/breadcrumbs/scopes/<id>/config.json

A profile is in at most one group (so the chain is one line). The default profile is the base, never a member. The
file of a profile that is not in a group is not changed by any of this.

Mutations (`apply`, the "groups" port of the explorer): create, rename, delete, add, remove. Deleting a group moves
its folder to groups/<name>.deleted-<time> (its config is not lost); its members then read the default only.
"""
import json
import os
import re
import shutil
import tempfile
import time

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
FILE = "groups.json"
FOLDER = "groups"


def _base(hermes):
    return os.path.join(hermes, "breadcrumbs")


def folder(hermes, name):
    """The folder that holds config.json of a group."""
    return os.path.join(_base(hermes), FOLDER, name)


def load(hermes):
    """{name: {"profiles": [...], "note": ""}} (tolerant of a missing or bad file: {})."""
    try:
        with open(os.path.join(_base(hermes), FILE), encoding="utf-8") as f:
            raw = json.load(f).get("groups")
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for name, g in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(name, str) and NAME.match(name) and isinstance(g, dict):
            profiles = g.get("profiles")
            out[name] = {"profiles": [p for p in profiles if isinstance(p, str)] if isinstance(profiles, list) else [],
                         "note": str(g.get("note") or "")}
    return out


def _save(hermes, groups):
    os.makedirs(_base(hermes), exist_ok=True)
    path = os.path.join(_base(hermes), FILE)
    fd, tmp = tempfile.mkstemp(dir=_base(hermes), prefix=".groups.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"groups": groups}, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def group_of(hermes, profile):
    """The name of the group that a profile is in, or None."""
    for name, g in sorted(load(hermes).items()):
        if profile in g["profiles"]:
            return name
    return None


def members(hermes, name):
    return list(load(hermes).get(name, {}).get("profiles", ()))


def peers(hermes, profile):
    """The profiles whose memory this profile may read: the other members of its group ([] when it has none)."""
    name = group_of(hermes, profile)
    return [p for p in members(hermes, name) if p != profile] if name else []


def may_read(hermes, reader, owner):
    """True when the profile `reader` may read the memory of `owner`: its own, or a member of its group."""
    return reader == owner or owner in peers(hermes, reader)


def chain_folders(hermes, profile):
    """[(label, folder)] that a profile reads before its own config.json, the widest first."""
    if profile == "default":
        return []
    out = [("default", _base(hermes))]
    name = group_of(hermes, profile)
    if name:
        out.append(("group " + name, folder(hermes, name)))
    return out


def _profiles(hermes):
    from .workdir import profiles

    return [n for n, _ in profiles(hermes)]


def _check_name(name):
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError("a group name is letters, digits, dot, dash and underscore (up to 40): %r" % (name,))
    return name


def _need(groups, name):
    if name not in groups:
        raise KeyError("no group %s" % name)


def create(hermes, name, note=""):
    groups = load(hermes)
    _check_name(name)
    if name in groups:
        raise ValueError("group %s exists" % name)
    groups[name] = {"profiles": [], "note": str(note or "")}
    os.makedirs(folder(hermes, name), exist_ok=True)
    _save(hermes, groups)
    return groups[name]


def rename(hermes, old, new):
    groups = load(hermes)
    _need(groups, old)
    _check_name(new)
    if new in groups:
        raise ValueError("group %s exists" % new)
    groups[new] = groups.pop(old)
    if os.path.isdir(folder(hermes, old)):
        os.rename(folder(hermes, old), folder(hermes, new))
    _save(hermes, groups)
    return groups[new]


def delete(hermes, name):
    """Drop a group. Its folder is kept under another name; the members read the default only. Returns that folder."""
    groups = load(hermes)
    _need(groups, name)
    del groups[name]
    kept = ""
    if os.path.isdir(folder(hermes, name)):
        kept = folder(hermes, "%s.deleted-%s" % (name, time.strftime("%Y%m%dT%H%M%S")))
        shutil.move(folder(hermes, name), kept)
    _save(hermes, groups)
    return kept


def add(hermes, name, profile):
    """Put a profile in a group (it leaves the group that it was in: one group at most)."""
    groups = load(hermes)
    _need(groups, name)
    if profile == "default":
        raise ValueError("the default profile is the base that every profile reads: it is in no group")
    if profile not in _profiles(hermes):
        raise ValueError("no profile %s" % profile)
    for g in groups.values():
        if profile in g["profiles"]:
            g["profiles"].remove(profile)
    groups[name]["profiles"] = sorted(groups[name]["profiles"] + [profile])
    os.makedirs(folder(hermes, name), exist_ok=True)
    _save(hermes, groups)
    return groups[name]


def remove(hermes, name, profile):
    groups = load(hermes)
    _need(groups, name)
    if profile not in groups[name]["profiles"]:
        raise ValueError("%s is not in group %s" % (profile, name))
    groups[name]["profiles"].remove(profile)
    _save(hermes, groups)
    return groups[name]


def note(hermes, name, text):
    groups = load(hermes)
    _need(groups, name)
    groups[name]["note"] = str(text or "")
    _save(hermes, groups)
    return groups[name]


def rows(hermes):
    """What the widget shows: [{name, profiles, note, path, exists}] and the profiles that are in no group."""
    groups = load(hermes)
    out = [{"name": n, "profiles": g["profiles"], "note": g["note"],
            "path": os.path.join(folder(hermes, n), "config.json"),
            "exists": os.path.isfile(os.path.join(folder(hermes, n), "config.json"))} for n, g in sorted(groups.items())]
    grouped = {p for g in groups.values() for p in g["profiles"]}
    return {"groups": out, "ungrouped": [p for p in _profiles(hermes) if p != "default" and p not in grouped],
            "profiles": _profiles(hermes)}


def apply(hermes, action, payload=None):
    """The widget's call: one action, then the rows. Actions: get, create, rename, delete, add, remove, note."""
    p = dict(payload or {})
    name = p.get("name")
    if action == "create":
        create(hermes, name, p.get("note"))
    elif action == "rename":
        rename(hermes, name, p.get("to"))
    elif action == "delete":
        delete(hermes, name)
    elif action == "add":
        add(hermes, name, p.get("profile"))
    elif action == "remove":
        remove(hermes, name, p.get("profile"))
    elif action == "note":
        note(hermes, name, p.get("note"))
    elif action != "get":
        raise ValueError("unknown groups action: %s" % action)
    return rows(hermes)
