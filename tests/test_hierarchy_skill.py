"""The hierarchy skill (breadcrumbs_hermes/skills/hierarchy/SKILL.md): what it promises is checked against the code."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from breadcrumbs_hermes import dials, hermes

# What Hermes's skill server warns about on every load of a plugin skill (tools/skills_tool_plugin.py, the list of
# injection patterns): a text that trips it is a defect, so this one keeps clear of them.
SCANNER = ("ignore previous instructions", "ignore all previous", "you are now", "disregard your",
           "forget your instructions", "new instructions:", "system prompt:", "<system>", "]]>")
VERBS = ("orchestrate", "pinpoint", "scaffold", "dispatch", "fan-out", "inspect", "validate", "trim")


def skill():
    with open(hermes.HIERARCHY, encoding="utf-8") as f:
        return f.read()


def block(kind, holding=""):
    """The first fenced block of this kind whose text holds `holding`."""
    for text in re.findall(r"```%s\n(.*?)\n```" % kind, skill(), re.S):
        if holding in text:
            return text
    raise AssertionError("no %s block holding %r" % (kind, holding))


class HierarchySkillTest(unittest.TestCase):
    def test_the_frontmatter_names_the_skill_and_holds_the_registered_description(self):
        head = re.match(r"---\n(.*?)\n---\n", skill(), re.S)
        assert head is not None
        fields = dict(line.split(": ", 1) for line in head.group(1).splitlines())
        self.assertEqual(fields["name"], "hierarchy")
        self.assertEqual(fields["description"], hermes.HIERARCHY_ABOUT)
        self.assertTrue(0 < len(hermes.HIERARCHY_ABOUT) <= 60)

    def test_every_verb_of_the_lanes_is_taught(self):
        body = skill().lower()
        for verb in VERBS:
            self.assertIn(verb, body)

    def test_it_holds_no_price_step_and_no_ceiling(self):
        """A step and a ceiling are read from the dials when the skill is used, so the text cannot go stale on them.
        The numbers come from the dials here, not from this test."""
        body = skill()
        numbers = set(dials.PRICE_STEPS.values()) | {o["ace_ceiling"] for o in dials.MODELS.values()}
        self.assertTrue(numbers)
        for n in numbers:
            for form in (str(n), "{:,}".format(n)):
                self.assertNotIn(form, body)

    def test_it_keeps_clear_of_the_patterns_that_hermes_warns_about(self):
        body = skill().lower()
        for pattern in SCANNER:
            self.assertNotIn(pattern, body)

    def test_the_dial_example_is_a_valid_config_and_gears_the_lanes_it_names(self):
        cfg = json.loads(block("json"))
        self.assertEqual(dials.unknown(cfg), [])
        gear = lambda model: dials.value(cfg, model, "ace_lockup", {})
        self.assertTrue(gear("claude-fable-5-2"))
        self.assertTrue(gear("claude-opus-5-5"))
        self.assertFalse(gear("claude-sonnet-5-5"))
        self.assertFalse(gear("claude-haiku-5-5"))
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
        rows = {line.split()[0]: line.split() for line in done.stdout.splitlines()}
        self.assertEqual(rows["cheap"][2], "2")    # requests
        self.assertEqual(rows["cheap"][-1], "1")   # one of them over the ceiling
        self.assertEqual(rows["lead"][-1], "0")


if __name__ == "__main__":
    unittest.main()
