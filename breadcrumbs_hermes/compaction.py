"""Context compaction control for every Hermes profile: `python -m breadcrumbs compaction [status|on|off] [PROFILE ...]`.

A profile compacts its context in one of two ways:

  ace     context.engine and memory.provider are both breadcrumbs (ace.wanted): ACE folds the older turns into crumbs.
  hermes  anything else: Hermes's own compaction (its compressor) runs. This is the default; the plugin never
          compacts a profile that did not ask for it.

status  lists every profile (the default one and each ~/.hermes/profiles/NAME) with its plugin, memory provider,
        context engine and the way it compacts. It reads config.yaml and changes nothing.
on      memory.provider breadcrumbs first (the provider gives the crumb tool that opens the handles), then
        context.engine breadcrumbs. A profile without the plugin is skipped and named.
off     context.engine compressor (Hermes's own) where breadcrumbs holds the engine seat. memory.provider stays.

With no PROFILE, status and off go to every profile; `on` needs a PROFILE or --all, because it is the opt-in.
Settings are written with `hermes -p PROFILE config set` (never by editing config.yaml); a Hermes chat started after
the change uses them.
"""
import logging
import os
import time

from .ace import setting
from .hermescmd import hermes_config

NAME = "breadcrumbs"
HERMES_ENGINE = "compressor"
NO_PROVIDER = {"", "none", "null", "builtin", "(not set)"}
HEAL_LOG = "heal.log"
log = logging.getLogger(__name__)


def _root():
    return os.path.expanduser("~/.hermes")


def home_of(profile):
    return _root() if profile in ("", "default") else os.path.join(_root(), "profiles", profile)


def profiles():
    """The default profile (when it has a config.yaml) and each folder of ~/.hermes/profiles, by name."""
    found = ["default"] if os.path.isfile(os.path.join(_root(), "config.yaml")) else []
    folder = os.path.join(_root(), "profiles")
    try:
        names = sorted(n for n in os.listdir(folder) if os.path.isdir(os.path.join(folder, n)))
    except OSError:
        names = []
    return found + names


def state(profile, name=NAME):
    """{profile, plugin, memory, engine, mode} from the profile's config.yaml and plugins folder."""
    home = home_of(profile)
    memory, engine = setting(home, "memory", "provider"), setting(home, "context", "engine")
    return {"profile": profile,
            "plugin": os.path.exists(os.path.join(home, "plugins", name)),
            "memory": memory, "engine": engine,
            "mode": "ace" if memory == name and engine == name else "hermes"}


def turn_on(profile, name=NAME):
    """ACE for a profile. Returns the state, or None when the profile has no plugin (nothing is written then)."""
    before = state(profile, name)
    if not before["plugin"]:
        return None
    if before["memory"] != name:
        hermes_config(profile, "set", "memory.provider", name)
    if before["engine"] != name:
        hermes_config(profile, "set", "context.engine", name)
    return state(profile, name)


def turn_off(profile, name=NAME):
    """Hermes's own compaction for a profile: the engine seat goes back to the compressor when breadcrumbs holds it."""
    before = state(profile, name)
    if before["engine"] == name:
        hermes_config(profile, "set", "context.engine", HERMES_ENGINE)
    return state(profile, name)


def _hermes_set(key, value):
    """Hermes's own writer of config.yaml, in this process (the one `hermes config set` calls), for the profile that
    HERMES_HOME names. No subprocess: the plugin runs inside Hermes at load, where a second hermes would be slow."""
    from hermes_cli.config import set_config_value
    set_config_value(key, value)


def heal(home, name=NAME, setter=None):
    """Fix a profile that selects the engine without a way to open its folds, when the plugin loads. Returns what it
    did ("filled" or "reverted"), or None when nothing was wrong (or BREADCRUMBS_NO_HEAL is set).

      context.engine breadcrumbs, memory.provider unset   -> memory.provider breadcrumbs (the ACE the profile asked for
                                                             gets its crumb tool; nothing is displaced)
      context.engine breadcrumbs, memory.provider other   -> context.engine compressor (breadcrumbs cannot take the
                                                             seat of another provider; the default, no ACE, holds)

    Hermes read its settings before the plugin loaded, so the fix applies to the next chat; the caller does not
    register ACE in this process. Each fix is logged to the logger and to <home>/breadcrumbs/heal.log."""
    setter = setter or _hermes_set
    if os.environ.get("BREADCRUMBS_NO_HEAL") or setting(home, "context", "engine") != name:
        return None
    memory = setting(home, "memory", "provider")
    if memory == name:
        return None
    try:
        if (memory or "").lower() in NO_PROVIDER:
            setter("memory.provider", name)
            did, why = "filled", "context.engine is %s and memory.provider was empty: set memory.provider %s" % (name, name)
        else:
            setter("context.engine", HERMES_ENGINE)
            did, why = "reverted", ("context.engine was %s but memory.provider is %s: set context.engine %s"
                                    % (name, memory, HERMES_ENGINE))
    except Exception as e:  # a profile that cannot be fixed still loads; ACE stays off (wanted)
        log.warning("breadcrumbs: could not fix the settings of %s: %s", home, e)
        return None
    log.warning("breadcrumbs: %s (takes effect for the next chat)", why)
    try:
        folder = os.path.join(home, "breadcrumbs")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, HEAL_LOG), "a", encoding="utf-8") as f:
            f.write("%s %s: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), did, why))
    except OSError:
        pass
    return did


def line(s):
    return "%-27s %-10s memory=%-12s engine=%-12s compaction=%s" % (
        s["profile"], "plugin" if s["plugin"] else "no plugin", s["memory"] or "-", s["engine"] or "-", s["mode"])


def command(action, names, every):
    """The CLI: returns the exit code. The `on` of a profile with no plugin is a failure, so a script sees it."""
    action = action or "status"
    known = profiles()
    if action == "on" and not (every or names):
        print("breadcrumbs: `on` needs a PROFILE or --all (ACE is off unless a profile asks for it)")
        return 2
    chosen = list(names) if names and not every else known
    missing = [n for n in chosen if n not in known]
    if missing:
        print("breadcrumbs: no such profile: %s" % ", ".join(missing))
        return 1
    failed = 0
    for p in chosen:
        if action == "status":
            print(line(state(p)))
            continue
        try:
            s = turn_on(p) if action == "on" else turn_off(p)
        except SystemExit as e:  # hermes_config exits on a failed command
            print("%-27s failed: %s" % (p, e))
            failed += 1
            continue
        if s is None:
            print("%-27s skipped: no plugin in %s" % (p, os.path.join(home_of(p), "plugins", NAME)))
            failed += 1
            continue
        print(line(s))
    return 1 if failed else 0
