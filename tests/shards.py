#!/usr/bin/env python3
"""
Which test files each CI shard runs

Whole files, as a file's tests can share a server and a port, and by weight, as
a test that starts Firefox takes seconds and most others take milliseconds.

Licensed under the MIT License - see LICENSE file for details
"""

from typing import Dict, List


def assign_files(weights: Dict[str, int], shards: int) -> List[List[str]]:
    """Heaviest first, each to the lightest shard so far. Sorted by name on ties,
    so every shard works out the same answer without talking to the others."""
    assigned: List[List[str]] = [[] for _ in range(shards)]
    totals = [0] * shards
    for name in sorted(weights, key=lambda file: (-weights[file], file)):
        lightest = totals.index(min(totals))
        assigned[lightest].append(name)
        totals[lightest] += weights[name]
    return assigned
