#!/usr/bin/env python3
"""
tests/shards.py: every test file runs in exactly one shard

Licensed under the MIT License - see LICENSE file for details
"""

import test_imports  # Automatic path setup
from shards import assign_files


WEIGHTS = {"a.py": 40, "b.py": 30, "c.py": 30, "d.py": 20, "e.py": 1, "f.py": 1}


def test_every_file_runs_exactly_once():
    assigned = assign_files(WEIGHTS, 3)
    assert sorted(name for shard in assigned for name in shard) == sorted(WEIGHTS)


def test_the_weight_is_shared_out():
    totals = [sum(WEIGHTS[name] for name in shard) for shard in assign_files(WEIGHTS, 3)]
    assert max(totals) - min(totals) <= 20


def test_every_shard_works_out_the_same_answer_whatever_order_it_collected():
    backwards = dict(reversed(list(WEIGHTS.items())))
    assert assign_files(WEIGHTS, 3) == assign_files(backwards, 3)


def test_more_shards_than_files_leaves_some_empty():
    assert sum(1 for shard in assign_files({"a.py": 1}, 3) if not shard) == 2
