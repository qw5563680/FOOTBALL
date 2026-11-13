"""Compare numeric summaries without weakening archived-byte or structural checks."""

import math

import pytest


def assert_same_summary(actual, expected, path="summary"):
    """Allow only last-bit float aggregation drift; preserve types and structure.

    Reloading sorted JSON changes h/d/a iteration order. Python versions with
    different float summation algorithms can then differ in the last few bits.
    An absolute 1e-14 tolerance covers that drift, with no relative tolerance.
    This helper is only for in-memory summaries, never persisted-file bytes.
    """
    assert type(actual) is type(expected), f"{path}: types differ"
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), f"{path}: keys differ"
        for key in expected:
            assert_same_summary(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), f"{path}: lengths differ"
        for index, (value, original) in enumerate(zip(actual, expected, strict=True)):
            assert_same_summary(value, original, f"{path}[{index}]")
    elif isinstance(expected, float):
        assert math.isfinite(actual) and math.isfinite(expected), f"{path}: nonfinite"
        assert actual == pytest.approx(expected, rel=0, abs=1e-14), path
    else:
        assert actual == expected, path
