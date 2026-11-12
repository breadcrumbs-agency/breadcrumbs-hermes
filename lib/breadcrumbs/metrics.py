"""Metrics: a named number, as common as a scalar. One entry type, one fold and one table, for every layer.

    metric(key, label, unit, agg, ...)  the entry of a metric (a dict, so that a view sends it as it is)
    fold(agg, a, b), value(agg, v)      how two folded values make one, and the number of a folded value
    Cell, put(), roll()                 a cell of an answer (a value and the records that it folds), and the folds of many
    add(*metrics), table(), get(key)    the table: a layer adds its metrics, a reader lists them

agg says how values fold:
    sum    the values add up (a count, tokens, bytes, dollars)
    max    the largest value stays (a level, such as a target)
    ratio  a share of two sums. A ratio has no value of its own to add: each record gives its parts, the pair
           (numerator, denominator), and the pairs add up. The number of a cell, of a row and of a total is always
           sum(numerators) / sum(denominators) of the pairs folded there, never a mean of ratios. A denominator of 0 gives
           no number ("no data", never a guess). `of` names the parts: (numerator field, (denominator fields...)), the
           fields of the record that part() reads.

The fields of an entry are the ones that the viewer reads: key, label, unit, agg, group (where the number is recorded),
src (what it is read from), units (the context units in which it has a position), keyed (by model and task), walks (read
from the crumb tree of every chat), and `of` for a ratio only. Common adds the cache metrics (breadcrumbs/usage.py);
the viewer adds its own measures. The table keeps the order in which the metrics were added.
"""
AGGS = ("sum", "max", "ratio")

_table = {}  # key -> entry, in the order added


def metric(key, label, unit, agg="sum", group="", src="", units=(), keyed=False, walks=False, of=None):
    """The entry of one metric. A ratio needs of=(numerator field, (denominator fields...)); a sum or a max has no `of`."""
    if agg not in AGGS:
        raise ValueError("metric %s: agg is one of %s" % (key, ", ".join(AGGS)))
    if (agg == "ratio") != (of is not None):
        raise ValueError("metric %s: a ratio, and only a ratio, names its parts (of=)" % key)
    m = {"key": key, "label": label, "unit": unit, "agg": agg, "group": group, "src": src, "units": list(units),
         "keyed": keyed, "walks": walks}
    if of is not None:
        num, dens = of
        m["of"] = [num, list(dens)]
    return m


def part(m, record):
    """What one record gives to the ratio m: the pair (numerator, denominator) of its fields."""
    num, dens = m["of"]
    return record[num], sum(record[d] for d in dens)


def fold(agg, a, b):
    """Two folded values of one metric as one."""
    if agg == "ratio":
        return a[0] + b[0], a[1] + b[1]
    return max(a, b) if agg == "max" else a + b


def value(agg, v):
    """The number of a folded value: a ratio's share of its sums, None when its denominator is 0 (or v is None)."""
    if v is None or agg != "ratio":
        return v
    return v[0] / v[1] if v[1] else None


class Cell:
    """One number of an answer and where it came from: value, how many records it folds, their first and last time."""
    __slots__ = ("v", "n", "t0", "t1", "src")

    def __init__(self, v, ts, src):
        self.v, self.n, self.t0, self.t1, self.src = v, 1, ts, ts, src

    def add(self, v, ts, agg):
        self.v = fold(agg, self.v, v)
        self.n += 1
        if ts is not None:
            self.t0 = ts if self.t0 is None else min(self.t0, ts)
            self.t1 = ts if self.t1 is None else max(self.t1, ts)


def put(acc, key, v, ts, src, agg="sum"):
    """acc[key] takes v: a new Cell, or the fold of v into the one there."""
    cell = acc.get(key)
    if cell is None:
        acc[key] = Cell(v, ts, src)
    else:
        cell.add(v, ts, agg)


def roll(into, other, agg_of):
    """into[key] takes the plain folded values of other ({key: value}). agg_of(key) is the agg of the metric of a key."""
    for key, v in other.items():
        have = into.get(key)
        into[key] = v if have is None else fold(agg_of(key), have, v)


def add(*metrics):
    """Add metrics to the table. A key that is there already must come with the same entry."""
    for m in metrics:
        have = _table.setdefault(m["key"], m)
        if have != m:
            raise ValueError("metric %s: another entry has this key" % m["key"])
    return list(metrics)


def table():
    """Every metric, in the order added."""
    return list(_table.values())


def get(key):
    return _table.get(key)
