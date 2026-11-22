"""Import this first in a test of what the viewer shows in full (level 4, the private text).

The viewer shows level 2 (handles and titles, no body text) by default and level 4 only after a launch flag and a
drag of the bar (breadcrumbs_spacegraph.disclosure); these tests read the whole text of a crumb and do not test the gate
(its own tests are in breadcrumbs-spacegraph/tests/test_disclosure.py), so they take the default up to level 4.
"""
import os

os.environ["BREADCRUMBS_DISCLOSE"] = "4"
try:
    from breadcrumbs_spacegraph import disclosure
    disclosure.DEFAULT = disclosure.PRIVATE
except ImportError:  # no viewer here: nothing to open
    pass
