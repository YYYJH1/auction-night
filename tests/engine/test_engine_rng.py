"""Independent integer streams: construction, known answers, uniformity and static guards."""

import ast
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import pytest
from engine_support import REPO, SEED_ALPHABET
from hypothesis import given
from hypothesis import strategies as st

from auction_night.engine.rng import RNG_VERSION, Stream, stream

ENGINE_DIR = REPO / "src" / "auction_night" / "engine"


# --------------------------------------------------- an independent reading of the spec


def reference_bytes(seed: str, purpose: str, index: int, count: int) -> bytes:
    """The byte stream written out from the documented construction, without the engine."""
    out = b""
    counter = 0
    while len(out) < count:
        text = f'["auction-night/rng@1",{json.dumps(seed)},{json.dumps(purpose)},{index},{counter}]'
        out += hashlib.sha256(text.encode("ascii")).digest()
        counter += 1
    return out


def reference_randbelow(seed: str, purpose: str, index: int, n: int, draws: int) -> list[int]:
    """``draws`` values of ``randbelow(n)`` by the documented rejection rule."""
    width = ((n - 1).bit_length() + 7) // 8
    space = 256**width
    limit = space - space % n
    buf = reference_bytes(seed, purpose, index, 4096)
    pos, out = 0, []
    while len(out) < draws:
        value = int.from_bytes(buf[pos : pos + width], "big")
        pos += width
        if value < limit:
            out.append(value % n)
    return out


def chi_square(counts: Counter[int], n: int, total: int) -> float:
    expected = total / n
    return sum((counts.get(k, 0) - expected) ** 2 / expected for k in range(n))


def chi_square_bound(df: int) -> float:
    """A bound that a correct generator exceeds with probability around 1e-5."""
    return df + 5 * math.sqrt(2 * df) + 10


# ------------------------------------------------------------------------------ the spec


def test_version_label_is_part_of_the_construction() -> None:
    assert RNG_VERSION == "rng@1"
    first_block = hashlib.sha256(b'["auction-night/rng@1","seed-a","demo",0,0]').hexdigest()
    assert first_block == "0afa3e122393e97dff064cdfcae5fdaae48335efd9bfe360e936a9dc153cd0af"
    taken = Stream("seed-a", "demo", 0)._take(32)
    assert taken.to_bytes(32, "big").hex() == first_block


@pytest.mark.parametrize("n", [2, 3, 7, 10, 100, 129, 255, 256, 257, 1000, 65537, 3_000_000])
def test_randbelow_matches_the_documented_rejection_rule(n: int) -> None:
    got = Stream("seed-a", "demo", 3)
    expected = reference_randbelow("seed-a", "demo", 3, n, 200)
    assert [got.randbelow(n) for _ in range(200)] == expected


def test_rejection_really_happens_for_awkward_sizes() -> None:
    # n = 129 accepts a single byte only below 129, so about half of the bytes are dropped.
    draws = reference_randbelow("seed-a", "demo", 0, 129, 200)
    raw = reference_bytes("seed-a", "demo", 0, 200)
    assert sum(1 for byte in raw[:200] if byte >= 129) > 50
    assert max(draws) < 129
    s = Stream("seed-a", "demo", 0)
    assert [s.randbelow(129) for _ in range(200)] == draws


def test_known_answers() -> None:
    s = Stream("seed-a", "demo", 0)
    assert [s.randbelow(100) for _ in range(12)] == [10, 62, 18, 35, 47, 25, 6, 76, 70, 31, 53, 91]
    s = Stream("seed-a", "demo", 0)
    assert [s.randbelow(129) for _ in range(12)] == [10, 62, 18, 35, 125, 6, 76, 53, 96, 54, 21, 60]
    assert Stream("seed-a", "demo", 1).shuffle(range(1, 9)) == [1, 5, 8, 4, 6, 2, 3, 7]
    s = Stream("seed-a", "demo", 2)
    assert (s.randint(-20, 20), s.randint(0, 400), s.token("ABC", 6)) == (8, 387, "BABCCA")
    s = Stream("seed-a", "big", 0)
    assert [s.randbelow(3_000_000) for _ in range(4)] == [1472305, 1439393, 1924080, 2497051]


def test_randbelow_of_one_reads_nothing() -> None:
    a, b = Stream("s", "p", 0), Stream("s", "p", 0)
    assert a.randbelow(1) == 0
    assert [a.randbelow(50) for _ in range(5)] == [b.randbelow(50) for _ in range(5)]


def test_stream_helper_is_the_class() -> None:
    assert stream("s", "p", 4).randbelow(1000) == Stream("s", "p", 4).randbelow(1000)


# --------------------------------------------------------------------------- independence


def test_streams_differ_in_every_coordinate() -> None:
    base = [Stream("seed", "purpose", 5).randbelow(10**9) for _ in range(1)]
    for other in (
        Stream("seed2", "purpose", 5),
        Stream("seed", "purpose2", 5),
        Stream("seed", "purpose", 6),
    ):
        assert [other.randbelow(10**9)] != base


def test_the_encoding_cannot_confuse_neighbouring_fields() -> None:
    a = Stream("ab", "c", 1).randbelow(2**60)
    b = Stream("a", "bc", 1).randbelow(2**60)
    c = Stream("a", "b", 11).randbelow(2**60)
    assert len({a, b, c}) == 3


def test_drawing_from_one_stream_never_moves_another() -> None:
    lone = [Stream("seed", "values", 4).randbelow(1000) for _ in range(1)]
    busy = Stream("seed", "budget", 0)
    for _ in range(500):
        busy.randbelow(1000)
    assert [Stream("seed", "values", 4).randbelow(1000)] == lone


@given(st.text(alphabet=SEED_ALPHABET, min_size=1, max_size=30), st.integers(0, 40))
def test_a_stream_is_a_pure_function_of_its_coordinates(seed: str, index: int) -> None:
    first = Stream(seed, "p", index)
    second = Stream(seed, "p", index)
    assert [first.randbelow(37) for _ in range(20)] == [second.randbelow(37) for _ in range(20)]


# -------------------------------------------------------------------------- distribution


@pytest.mark.parametrize("n", [2, 3, 7, 10, 101, 129])
def test_randbelow_is_uniform(n: int) -> None:
    s = Stream("uniformity", f"n{n}", 0)
    total = 60_000
    counts = Counter(s.randbelow(n) for _ in range(total))
    assert set(counts) == set(range(n))
    assert chi_square(counts, n, total) < chi_square_bound(n - 1)


def test_randint_covers_both_ends_and_is_uniform() -> None:
    s = Stream("uniformity", "randint", 0)
    total = 30_000
    counts = Counter(s.randint(-3, 3) + 3 for _ in range(total))
    assert set(counts) == set(range(7))
    assert chi_square(counts, 7, total) < chi_square_bound(6)


def test_shuffle_is_a_uniform_permutation() -> None:
    total = 12_000
    counts: Counter[tuple[int, ...]] = Counter()
    for index in range(total):
        counts[tuple(Stream("uniformity", "shuffle", index).shuffle([0, 1, 2, 3]))] += 1
    assert len(counts) == 24
    expected = total / 24
    chi = sum((count - expected) ** 2 / expected for count in counts.values())
    assert chi < chi_square_bound(23)


def test_shuffle_and_sample_return_new_lists() -> None:
    items = [1, 2, 3, 4, 5]
    s = Stream("s", "p", 0)
    shuffled = s.shuffle(items)
    assert items == [1, 2, 3, 4, 5]
    assert sorted(shuffled) == items
    picked = s.sample(items, 3)
    assert len(set(picked)) == 3
    assert set(picked) <= set(items)
    assert items == [1, 2, 3, 4, 5]


def test_sample_chooses_every_subset_with_equal_chance() -> None:
    total = 12_000
    counts: Counter[frozenset[int]] = Counter()
    for index in range(total):
        counts[frozenset(Stream("uniformity", "sample", index).sample(range(5), 2))] += 1
    assert len(counts) == 10
    expected = total / 10
    chi = sum((count - expected) ** 2 / expected for count in counts.values())
    assert chi < chi_square_bound(9)


def test_choice_and_token() -> None:
    s = Stream("s", "choice", 0)
    assert {s.choice("abc") for _ in range(60)} == {"a", "b", "c"}
    token = Stream("s", "token", 0).token("XY", 12)
    assert len(token) == 12
    assert set(token) <= {"X", "Y"}
    assert Stream("s", "token", 0).token("XY", 0) == ""


# --------------------------------------------------------------------------- bad input


@pytest.mark.parametrize(
    "call",
    [
        lambda: Stream("", "p", 0),
        lambda: Stream("s", "", 0),
        lambda: Stream("s", "p", -1),
        lambda: Stream("s", "p", True),
        lambda: Stream(5, "p", 0),  # type: ignore[arg-type]
        lambda: Stream("s", "p", 0).randbelow(0),
        lambda: Stream("s", "p", 0).randbelow(True),
        lambda: Stream("s", "p", 0).randbelow(2.5),  # type: ignore[arg-type]
        lambda: Stream("s", "p", 0).randint(3, 2),
        lambda: Stream("s", "p", 0).sample([1, 2], 3),
        lambda: Stream("s", "p", 0).sample([1, 2], -1),
        lambda: Stream("s", "p", 0).choice([]),
        lambda: Stream("s", "p", 0).token("", 3),
    ],
)
def test_bad_arguments_are_refused(call) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        call()


# ------------------------------------------------------------------------- static guards

FORBIDDEN_MODULES = {
    "random",
    "secrets",
    "time",
    "datetime",
    "os",
    "sys",
    "socket",
    "subprocess",
    "pathlib",
    "uuid",
}
FORBIDDEN_CALLS = {"hash", "open", "print", "input", "eval", "exec"}


def engine_sources() -> list[Path]:
    return sorted(ENGINE_DIR.rglob("*.py"))


def test_engine_sources_exist() -> None:
    names = {path.name for path in engine_sources()}
    assert {"rng.py", "deal.py", "rules_text.py", "sealed.py", "english.py", "dutch.py"} <= names


@pytest.mark.parametrize("path", engine_sources(), ids=lambda p: str(p.relative_to(ENGINE_DIR)))
def test_engine_is_pure_integer_code(path: Path) -> None:
    """No random module, no salted hash(), no clock, no I/O and no floating point."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_MODULES, (path.name, alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in FORBIDDEN_MODULES, (
                path.name,
                node.module,
            )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in FORBIDDEN_CALLS, (path.name, node.func.id, node.lineno)
        elif isinstance(node, ast.Constant):
            assert not isinstance(node.value, float), (path.name, node.value, node.lineno)
        elif isinstance(node, ast.BinOp):
            assert not isinstance(node.op, ast.Div), (path.name, node.lineno)
