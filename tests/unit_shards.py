"""Run a long unit test module in shards (t-078). Not a test module.

A module that does `from unit_shards import load_tests` runs, when PA_UNIT_SHARD=<k>/<n> is set, only every n-th of
its tests in loader order (classes, then methods, by name), from the k-th: the n shards together run each test
exactly once, and a test added later lands in exactly one shard without an edit (the panel harness's --shard rule,
t-031). With no value (or an empty one) it runs all its tests, so `python3 -m unittest tests.test_x` is unchanged.
A malformed value raises, so the module errors and its part fails rather than running nothing.
tests/run-all.sh sets the value for one part only, from its unit_shards table. The shard keeps the tests'
TestCase objects in one suite, so unittest still sets up a class's (and the module's) fixtures in each shard that
holds one of their tests.
"""
import os
import re
import unittest

ENV = "PA_UNIT_SHARD"


def parse(value):
    """(k, n) from "<k>/<n>" with 1 <= k <= n; else ValueError."""
    found = re.fullmatch(r"([0-9]+)/([0-9]+)", value)
    if not found or not 1 <= int(found.group(1)) <= int(found.group(2)):
        raise ValueError("{}={!r}: expected <k>/<n> with 1 <= k <= n".format(ENV, value))
    return int(found.group(1)), int(found.group(2))


def flat(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flat(test)
        else:
            yield test


def load_tests(loader, tests, pattern):
    value = os.environ.get(ENV)
    if not value:
        return tests
    k, n = parse(value)
    return unittest.TestSuite([t for i, t in enumerate(flat(tests)) if i % n == k - 1])
