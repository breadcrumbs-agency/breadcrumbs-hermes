"""The Hermes section of the explorer's settings widget: two switches that make breadcrumbs a part of Hermes.

  memory_provider   memory.provider = breadcrumbs    (the memory provider of the profile)
  context_engine    context.engine = breadcrumbs     (ACE shapes each request)

A switch is read with `hermes -p PROFILE config get KEY` and written with `hermes -p PROFILE config set KEY VALUE`.
Turning a switch on saves the value that it replaces in <base>/hermes-settings.json (base: the breadcrumbs folder of the
profile), so Revert restores that value. Hermes has no command that unsets a key: a switch whose earlier value was empty
is not reverted by the widget, and the message says which `hermes config set` to run by hand.

The profile is the one in the path of the store (.../profiles/NAME/breadcrumbs); a store outside a profile is `default`.
Changes take effect for a Hermes chat started after the change.
"""
import json
import os
import re

TARGET = "breadcrumbs"
FIELDS = (
    {"key": "memory_provider", "config": "memory.provider", "label": "Memory provider", "kind": "bool", "default": None,
     "doc": "On: breadcrumbs is the memory provider of this Hermes profile. Off: the provider that was set before. "
            "Applies to a Hermes chat started after the change."},
    {"key": "context_engine", "config": "context.engine", "label": "Context engine", "kind": "bool", "default": None,
     "doc": "On: breadcrumbs is the context engine (ACE shapes each request). Off: the engine that was set before. "
            "Applies to a Hermes chat started after the change."},
)
SAVED = "hermes-settings.json"


def profile_of(root):
    """The Hermes profile of a store path: the NAME of .../profiles/NAME/..., else default."""
    m = re.search(r"/profiles/([^/]+)/", (root or "") + "/")
    return m.group(1) if m else "default"


def base_of(root):
    """The breadcrumbs folder of the profile (the part of the path up to .../profiles/NAME/breadcrumbs), else the root."""
    m = re.search(r"^(.*?/profiles/[^/]+/breadcrumbs)(?:/|$)", root or "")
    return m.group(1) if m else (root or "")


def _config(profile, *args):
    from .adapter import hermes_config
    try:
        return hermes_config(profile, *args)
    except SystemExit as e:  # hermes_config exits on a failed command: the widget gets the reason as a 400
        raise ValueError(str(e))


def _load(root):
    try:
        with open(os.path.join(base_of(root), SAVED), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root, data):
    base = base_of(root)
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, SAVED)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _field(key):
    field = next((f for f in FIELDS if f["key"] == key), None)
    if field is None:
        raise ValueError("unknown Hermes setting: %s" % key)
    return field


def view(root):
    """The harness entry of the widget: {key, label, description, profile, enabled, fields}."""
    profile = profile_of(root)
    saved = _load(root)
    fields = []
    for f in FIELDS:
        item = {k: f[k] for k in ("key", "label", "kind", "default", "doc")}
        item["value"] = _config(profile, "get", f["config"]) == TARGET
        item["source"] = "hermes config (%s)" % profile
        item["overridden"] = f["key"] in saved
        fields.append(item)
    return {"key": "hermes", "label": "Hermes", "profile": profile, "enabled": True,
            "description": "Hermes settings: the memory provider and the context engine of this profile.",
            "fields": fields}


def _switch(root, field, on):
    profile = profile_of(root)
    saved = _load(root)
    if on:
        current = _config(profile, "get", field["config"])
        if current != TARGET and field["key"] not in saved:
            saved[field["key"]] = current
            _save(root, saved)
        _config(profile, "set", field["config"], TARGET)
        return
    if field["key"] not in saved:
        raise ValueError("%s was not turned on by breadcrumbs, so there is no earlier value to restore: run "
                         "hermes -p %s config set %s NAME" % (field["config"], profile, field["config"]))
    earlier = saved[field["key"]]
    if not earlier:
        raise ValueError("%s was empty before breadcrumbs set it, and Hermes has no command that unsets a key: run "
                         "hermes -p %s config set %s NAME" % (field["config"], profile, field["config"]))
    _config(profile, "set", field["config"], earlier)
    del saved[field["key"]]
    _save(root, saved)


def settings(root, action, payload=None):
    """The callback of the Hermes harness (ports.harness settings=): get, set, delete (revert one) or reset (all)."""
    payload = payload if isinstance(payload, dict) else {}
    if action == "get":
        return view(root)
    if action == "set":
        field = _field(payload.get("key"))
        if not isinstance(payload.get("value"), bool):
            raise ValueError("%s must be true or false" % field["key"])
        _switch(root, field, payload["value"])
    elif action == "delete":
        _switch(root, _field(payload.get("key")), False)
    elif action == "reset":
        keys = [payload["key"]] if payload.get("key") else list(_load(root))
        for key in keys:
            if key in _load(root):
                _switch(root, _field(key), False)
    else:
        raise ValueError("unknown Hermes settings action: %s" % action)
    return view(root)
