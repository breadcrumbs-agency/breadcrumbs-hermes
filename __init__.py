"""breadcrumbs: a Hermes memory provider with exact provenance (plugin entry point).

The plugin catalog installs this repository as the plugin directory; Hermes loads this file as a
package, so the imports of its own package below are relative. The packages under it (breadcrumbs,
breadcrumbs_spacegraph) are copied under lib/ from breadcrumbs-common and breadcrumbs-spacegraph (tools/bundle.py). See README.md.
"""

import os as _os
import sys as _sys

_LIB = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "lib")  # the layers under this plugin (tools/bundle.py)
if _os.path.isdir(_LIB) and _LIB not in _sys.path:
    _sys.path.insert(0, _LIB)
from .breadcrumbs_hermes import adapter  # noqa: F401  first: the adapter registers once, under this package's name
from .breadcrumbs_hermes.ace import Ace, wanted
from .breadcrumbs_hermes.hermes import BreadcrumbsProvider, register_plugin


def register(ctx):
    # Hermes finds a plugin's memory provider and context engine by these two calls in this file's text, so
    # they stay here; register_plugin adds the rest.
    provider = BreadcrumbsProvider()
    ctx.register_memory_provider(provider)
    register_plugin(ctx, provider)  # the handbook, the dials and the islands
    if hasattr(ctx, "register_context_engine") and wanted(name=provider.name):  # ACE: where context.engine names it
        ctx.register_context_engine(Ace(name=provider.name))
