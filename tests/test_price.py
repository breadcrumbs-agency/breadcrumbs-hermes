"""The price port of the adapter of hermes: the rates of Hermes's own table (agent/usage_pricing.py), or None. The table is a
fake module here: the test needs no Hermes, and no price is copied into this repo."""
import sys
import types
import unittest

from breadcrumbs import ports
from breadcrumbs_hermes import adapter


class Entry:
    def __init__(self, inp, read, write):
        self.input_cost_per_million, self.cache_read_cost_per_million, self.cache_write_cost_per_million = inp, read, write


class PricePortTest(unittest.TestCase):

    def setUp(self):
        self.asked = []
        table = {"m1": Entry(3, 0.3, 3.75), "m2": Entry(1, None, 1.25)}  # made-up rates; m2 lacks a cache read rate

        def get_pricing_entry(model, provider=None):
            self.asked.append((model, provider))
            if model == "boom":
                raise RuntimeError("the table failed")
            return table.get(model)

        agent, pricing = types.ModuleType("agent"), types.ModuleType("agent.usage_pricing")
        pricing.get_pricing_entry, agent.usage_pricing = get_pricing_entry, pricing
        saved = {k: sys.modules.get(k) for k in ("agent", "agent.usage_pricing")}
        sys.modules.update({"agent": agent, "agent.usage_pricing": pricing})
        self.addCleanup(lambda: [sys.modules.pop(k, None) if v is None else sys.modules.__setitem__(k, v) for k, v in saved.items()])
        adapter._PRICES.clear()
        self.addCleanup(adapter._PRICES.clear)

    def test_the_adapter_gives_the_rates_of_hermes_and_none_without_them(self):
        price = ports.get("price")
        self.assertIs(price, adapter._price)
        self.assertEqual(price("m1", "anthropic"), (3.0, 0.3, 3.75))
        self.assertIsNone(price("m2", "openai"), "a missing rate: no price")
        self.assertIsNone(price("nope", "x"), "an unknown model")
        self.assertIsNone(price("boom", "x"), "a table that fails")
        price("m1", "anthropic")
        self.assertEqual(self.asked.count(("m1", "anthropic")), 1, "one ask for each model and provider")

    def test_no_hermes_on_the_path_gives_no_price(self):
        sys.modules["agent.usage_pricing"] = None  # an import of it fails
        self.assertIsNone(adapter._price("m1", "anthropic"))


if __name__ == "__main__":
    unittest.main()
