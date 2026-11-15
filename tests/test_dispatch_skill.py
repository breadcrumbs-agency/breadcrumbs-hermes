"""The dispatch skill (breadcrumbs_hermes/skills/dispatch/SKILL.md: the briefs, then a lane table for each model family
and the submodel a profile of that family gets): what it promises is checked against the code. The file is generated
from the umbrella repository (breadcrumbs/skills/dispatch, tools/skills.py), so it is not edited here."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from breadcrumbs import dials
from breadcrumbs_hermes import hermes

# What Hermes's skill server warns about on every load of a plugin skill (tools/skills_tool_plugin.py, the list of
# injection patterns): a text that trips it is a defect, so this one keeps clear of them.
SCANNER = ("ignore previous instructions", "ignore all previous", "you are now", "disregard your",
           "forget your instructions", "new instructions:", "system prompt:", "<system>", "]]>")
VERBS = ("orchestrate", "pinpoint", "scaffold", "dispatch", "fan-out", "inspect", "validate", "trim")
FAMILIES = ("Claude", "GPT", "NVIDIA")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def skill():
    with open(hermes.HANDBOOK, encoding="utf-8") as f:
        return f.read()


def block(kind, holding=""):
    """The first fenced block of this kind whose text holds `holding`."""
    for text in re.findall(r"```%s\n(.*?)\n```" % kind, skill(), re.S):
        if holding in text:
            return text
    raise AssertionError("no %s block holding %r" % (kind, holding))


def submodels():
    """{family title: {key: value}} from the `hermes config set delegation.*` block of each family."""
    out = {}
    for title, text in re.findall(r"(?m)^(\w+) profile:\n\n```sh\n(.*?)\n```", skill(), re.S):
        out[title] = dict(re.findall(r"hermes config set delegation\.(\w+) (\S+)", text))
    return out


def rows():
    """The cells of each row of the lane tables."""
    return [[c.strip() for c in line.strip("|").split("|")] for line in skill().splitlines()
            if line.startswith("| ") and not line.startswith("| lane ")]


class DispatchSkillTest(unittest.TestCase):
    def test_it_opens_with_the_briefs_and_its_description_fits(self):
        self.assertTrue(skill().startswith("1. Align the least manufactured intent"))
        self.assertTrue(0 < len(hermes.HANDBOOK_ABOUT) <= 60)

    def test_every_verb_is_taught(self):
        body = skill().lower()
        for verb in VERBS:
            self.assertIn(verb, body, verb)

    def test_each_family_gives_a_profile_its_cheap_lane_as_submodel(self):
        found = submodels()
        self.assertEqual(sorted(found), sorted(FAMILIES))
        cheap = [r for r in rows() if r[0] == "cheap"]
        self.assertEqual(len(cheap), len(FAMILIES))
        for title, keys in found.items():
            self.assertEqual(sorted(keys), ["model", "provider", "reasoning_effort"], title)
            self.assertIn(keys["reasoning_effort"], EFFORTS)
            self.assertTrue(any(r[2].split(",")[0] == keys["model"] and r[3] == keys["reasoning_effort"] for r in cheap),
                            (title, keys))

    def test_it_holds_no_price_step_and_no_ceiling(self):
        """A step and a ceiling are read from the dials when the skill is used, so the text cannot go stale on them.
        The numbers come from the dials here, not from this test."""
        numbers = set(dials.PRICE_STEPS.values()) | {o["ace_ceiling"] for o in dials.MODELS.values()}
        self.assertTrue(numbers)
        body = skill()
        for n in numbers:
            for form in (str(n), "{:,}".format(n)):
                self.assertNotIn(form, body)

    def test_it_keeps_clear_of_the_patterns_that_hermes_warns_about(self):
        body = skill().lower()
        for pattern in SCANNER:
            self.assertNotIn(pattern, body)

    def test_the_dial_example_is_a_valid_config_and_gears_the_top_lanes(self):
        cfg = json.loads(block("json"))
        self.assertEqual(dials.unknown(cfg), [])
        for r in rows():
            if r[0].startswith("|---") or set(r[0]) <= {"-"}:
                continue
            model = r[2].split(",")[0]
            self.assertEqual(bool(dials.value(cfg, model, "ace_lockup", {})), "(top)" in r[0], r)
        # the cheap lane needs nothing set: its ceiling is built in
        self.assertEqual(dials.value(cfg, "claude-haiku-5-5", "ace_ceiling", {}),
                         next(iter(dials.MODELS.values()))["ace_ceiling"])

    def test_each_command_it_teaches_exists(self):
        body = skill()
        subs = set(re.findall(r"`breadcrumbs ([a-z][a-z-]*)", body)) | set(re.findall(r"(?m)^breadcrumbs ([a-z][a-z-]*)", body))
        self.assertTrue({"dials", "cost", "project", "push", "pull", "hive"} <= subs, subs)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
        with tempfile.TemporaryDirectory() as home:
            for sub in sorted(subs):
                done = subprocess.run([sys.executable, "-W", "ignore", "-m", "breadcrumbs", "--home", home, sub, "--help"],
                                      capture_output=True, text=True, env=env, timeout=60)
                self.assertEqual(done.returncode, 0, (sub, done.stderr[-300:]))

    def test_the_usage_snippet_counts_the_requests_over_a_ceiling(self):
        found = re.search(r"<<'EOF'\n(.*?)\nEOF", block("sh", holding="usage.log"), re.S)
        assert found is not None
        code = found.group(1)
        lines = ["2026-01-01T00:00:00 sess=a in=100 cr=80000 cw=1000 out=5 model=cheap task=main",
                 "2026-01-01T00:00:01 sess=a in=100 cr=120000 cw=0 out=5 model=cheap task=main",
                 "2026-01-01T00:00:02 sess=b in=10 cr=0 cw=0 out=5 model=lead task=main",
                 "not a usage line"]
        with tempfile.TemporaryDirectory() as home:
            log = os.path.join(home, "usage.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            done = subprocess.run([sys.executable, "-", log, "100000"], input=code, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = {line.split()[0]: line.split() for line in done.stdout.splitlines()}
        self.assertEqual(out["cheap"][2], "2")    # requests
        self.assertEqual(out["cheap"][-1], "1")   # one of them over the ceiling
        self.assertEqual(out["lead"][-1], "0")


if __name__ == "__main__":
    unittest.main()
