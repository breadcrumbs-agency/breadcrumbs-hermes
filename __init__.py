"""breadcrumbs: a Hermes memory provider with exact provenance (plugin entry point).

The plugin catalog installs this repository as the plugin directory; Hermes loads this file as a
package, so the imports of its own package below are relative. The packages under it (breadcrumbs,
breadcrumbs_spacegraph) come from breadcrumbs-common and breadcrumbs-spacegraph, which pyproject.toml names. See README.md.
"""
from .breadcrumbs_hermes import adapter  # noqa: F401  first: the adapter registers once, under this package's name
from .breadcrumbs_hermes.ace import Ace, _home, wanted
from .breadcrumbs_hermes.hermes import BreadcrumbsProvider, register_plugin


def register(ctx):
    # Hermes finds a plugin's memory provider and context engine by these two calls in this file's text, so
    # they stay here; register_plugin adds the rest.
    provider = BreadcrumbsProvider()
    ctx.register_memory_provider(provider)
    register_plugin(ctx, provider)  # the handbook, the dials, /scope and /viz
    from .breadcrumbs_hermes.compaction import heal  # a profile that selects ACE without the provider is fixed here
    fixed = heal(_home(), name=provider.name)  # Hermes read its settings already: the fix is for the next chat
    if hasattr(ctx, "register_context_engine") and not fixed and wanted(name=provider.name):  # ACE: where context.engine names it
        ctx.register_context_engine(Ace(name=provider.name))
