"""Imported by the tests that open a provider: the store is then the profile's default memory, whatever folder the tests
run in (a folder inside a git repository would otherwise give the provider the memory of that repository).
test_scope_provider.py turns the scopes on where it tests them."""
import os

os.environ["BREADCRUMBS_SCOPE"] = "off"
