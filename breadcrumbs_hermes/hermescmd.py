"""`hermes -p PROFILE config ...`: the one call of the Hermes command line that the plugin makes (adapter.py, for
`slot`, and hermes_settings.py, for the explorer's switches). A module of its own, so that no module imports the adapter.
"""
import subprocess


def hermes_config(profile, *args):
    """`hermes -p PROFILE config ARGS...`: the last line of its output (Hermes's own notices dropped)."""
    p = subprocess.run(["hermes", "-p", profile or "default", "config", *args], capture_output=True, text=True,
                       timeout=120)
    if p.returncode:
        if args[:1] == ("get",) and "not set" in (p.stderr or "") + (p.stdout or ""):
            return ""  # Hermes exits 1 on a key that is not set: that is no value, not a failure
        raise SystemExit("breadcrumbs: hermes config %s failed: %s" % (" ".join(args), (p.stderr or p.stdout)[-300:]))
    lines = [line.strip() for line in p.stdout.splitlines() if line.strip() and not line.startswith("hermes:")]
    return lines[-1] if lines else ""
