"""Grades the two runs test.sh made: the counter compiled from the collected D, run once against
the shipped block served on another address and port and once against a held-back block. Nothing
here counts anything: every reported line is compared with the exact counts the block's generator
(solution/generate_block.py) recorded, baked into this image and closed to the build user."""
import os
import re

import pytest

RUN = "/opt/run"
TRUTH = "/opt/truth"
BLOCKS = ["sample", "hidden"]
TIME_LIMIT = 90.0
COUNT = re.compile(r"0|[1-9][0-9]*")


def read_text(path):
    """Opens without following a symlink, so a link planted at the path is refused."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        return fh.read().decode("ascii", "replace")


def golden(name):
    want = {}
    for line in read_text(os.path.join(TRUTH, "golden", name + ".txt")).splitlines():
        rev, out, count = line.split()
        want[(rev, out)] = count
    return want


def report(name):
    """The run's stdout as {(revision, output): count}, after checking the run ended inside the
    time limit and every line has the three fields the README gives. A final newline is optional
    and blank lines are not allowed."""
    code, t0, t1 = read_text(os.path.join(RUN, f"status_{name}.txt")).split()
    elapsed = float(t1) - float(t0)
    assert elapsed < TIME_LIMIT, f"the {name} run was still going at the {TIME_LIMIT:.0f} s limit"
    text = read_text(os.path.join(RUN, f"report_{name}.txt"))
    assert text.strip(), f"the {name} run printed nothing (exit status {code})"
    got = {}
    for n, line in enumerate(text.removesuffix("\n").split("\n"), 1):
        f = line.split(" ")
        assert len(f) == 3 and all(f), f"{name} line {n} is not '<revision> <output> <count>': {line[:120]!r}"
        rev, out, count = f
        assert COUNT.fullmatch(count), f"{name} line {n}: {count[:60]!r} is not a plain decimal count"
        assert (rev, out) not in got, f"{name}: output {out} reported twice at revision {rev}"
        got[(rev, out)] = count
    return got


def test_counter_built():
    """The collected .d files compiled with ldc2 into /app/bdd/bddcount."""
    assert read_text(os.path.join(RUN, "build_check.txt")).strip() == "ok", \
        "the collected D did not compile (see the ldc2 output in the log)"


@pytest.mark.parametrize("name", BLOCKS)
def test_every_checkpoint_lists_its_outputs(name):
    """Within 90 s the run printed one line for every output present at every checkpoint and
    none for outputs that are absent there (dropped, or not yet added)."""
    got, want = report(name), golden(name)
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    assert not missing and not extra, \
        f"{name}: missing {missing[:8]} ({len(missing)}), not present at that revision {extra[:8]} ({len(extra)})"


@pytest.mark.parametrize("name", BLOCKS)
def test_every_count_is_exact(name):
    """Every reported count equals the exact number of input assignments that drive that
    output to 1 at that revision."""
    got, want = report(name), golden(name)
    wrong = [(k, got[k], v) for k, v in sorted(want.items()) if k in got and got[k] != v]
    absent = [k for k in want if k not in got]
    assert not absent, f"{name}: {len(absent)} (revision, output) pairs have no count, e.g. {absent[:4]}"
    assert not wrong, f"{name}: {len(wrong)} counts wrong, e.g. " + "; ".join(
        f"rev {k[0]} {k[1]}: got {g[:40]} want {w[:40]}" for k, g, w in wrong[:4])
