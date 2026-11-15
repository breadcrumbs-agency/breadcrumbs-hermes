"""The port "config_layers": the config.json files that the explorer may show and change, and which chats read each.

Hermes runs a chat under a profile, and every surface of that profile reads the same files: the CLI, the TUI, the
desktop app, the gateway's bots (Telegram, Discord, Slack, ...), cron jobs and delegate_task children. The profile is
the one of the process (HERMES_HOME), not of the user or the bot, so one person's chats can land in several profiles, and
one bot token serves every chat of its profile. A chat reads (workdir.config_folders):

  profile   <home>/breadcrumbs/config.json                 every chat of the profile
  scope     <home>/breadcrumbs/scopes/<id>/config.json     the chats of the profile whose folder is in that git
                                                           repository or folder; its values win over the profile's

So the explorer lists the file of every profile of this Hermes root (the default profile and each folder of
<root>/profiles), and the file of each scope of each profile, with the chats that read it. A profile that opted out of
visualization (novis) is left out, with its scopes. The page chooses a file by its id; it never names a path.

A store that is no Hermes store (a demo, `--home DIR`) gives nothing: the explorer then shows its own config.json.
"""
import os

from breadcrumbs import scope

APPLIES_PROFILE = ("every chat of profile %s: the CLI, the TUI, the desktop app, its gateway bots, its cron jobs and "
                   "delegate_task children; in the folder of a scope, that scope's file wins over it")
APPLIES_SCOPE = ("the chats of profile %s that work in %s (%s); its values win over the profile's file, and a "
                 "dial that it does not set comes from there")
KINDS = {"git": "a git repository", "explicit": "a .breadcrumbs folder", "source": "a source tree", "dir": "a folder"}


def hermes_of(root):
    """The Hermes root of a store (the folder that holds config.yaml and profiles/), or None for a store outside one."""
    base = scope.base_of(root)
    if os.path.basename(base) != "breadcrumbs":
        return None
    home = os.path.dirname(base)
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    if any(os.path.exists(os.path.join(home, f)) for f in ("config.yaml", "state.db", "profiles")):
        return home
    return None


def layers(root, hermes=None):
    """[{id, kind, label, path, parent, applies, profile, here}] of the Hermes root of the store at root, or []."""
    from . import novis
    from .workdir import profiles

    hermes = hermes or hermes_of(root)
    if not hermes:
        return []
    here = os.path.realpath(root)
    out = []
    for name, home in profiles(hermes):
        if novis.Hide(home, hermes).tags()[0]:  # the profile opted out of visualization
            continue
        base = os.path.join(home, "breadcrumbs")
        pid = "profile:%s" % name
        out.append({"id": pid, "kind": "profile", "label": "profile %s" % name, "path": base, "parent": None,
                    "applies": APPLIES_PROFILE % name, "profile": name,
                    "here": os.path.realpath(base) == here})
        for s in scope.known(base):
            out.append({"id": "scope:%s:%s" % (name, s["id"]), "kind": "scope",
                        "label": "%s / %s" % (name, scope.short(s["root"]) or s["id"]), "path": s["path"],
                        "parent": pid, "profile": name, "here": os.path.realpath(s["path"]) == here,
                        "applies": APPLIES_SCOPE % (name, scope.short(s["root"]), KINDS.get(s["kind"], s["kind"]))})
    return out
