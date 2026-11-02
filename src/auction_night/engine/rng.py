"""Independent integer streams derived with hashlib.

Every random draw of the environment (lot schedule, private cards, budgets, missions, tie
keys) comes from a :class:`Stream`. A stream is fixed by three things: the night seed text,
a purpose name and an index. The index is the lot number for per-lot purposes, the seat
number for per-seat purposes and 0 for night-level purposes. Streams that differ in any of
the three share nothing, so adding a draw to one purpose can never shift the numbers of
another purpose.

The module uses neither the standard ``random`` module nor Python's ``hash()``, which is
salted per process. Construction (version ``rng@1``):

* ``block(c)`` is the SHA-256 digest of the compact JSON text of
  ``["auction-night/rng@1", seed, purpose, index, c]`` with ASCII escapes, ``c = 0, 1, ...``.
* The byte stream is ``block(0) + block(1) + ...``, read from the front.
* ``randbelow(n)`` takes the smallest whole number of bytes ``b`` with ``256**b >= n``, reads
  ``b`` bytes as a big-endian integer ``x`` and accepts it when ``x < (256**b // n) * n``.
  The result is ``x % n``. A rejected ``x`` is dropped and the next ``b`` bytes are read.
  ``randbelow(1)`` is 0 and reads nothing.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Final

RNG_VERSION: Final = "rng@1"
_DOMAIN: Final = f"auction-night/{RNG_VERSION}"


def _block(seed: str, purpose: str, index: int, counter: int) -> bytes:
    text = json.dumps(
        [_DOMAIN, seed, purpose, index, counter], separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(text.encode("ascii")).digest()


class Stream:
    """A deterministic stream of integers for one (seed, purpose, index).

    Streams are cheap to create, so draw each purpose from a fresh stream and keep the draw
    order inside a purpose fixed. The object is stateful and not meant to be shared.
    """

    __slots__ = ("_buffer", "_counter", "_index", "_pos", "_purpose", "_seed")

    def __init__(self, seed: str, purpose: str, index: int = 0) -> None:
        if not isinstance(seed, str) or not seed:
            raise ValueError("the seed is a non-empty text")
        if not isinstance(purpose, str) or not purpose:
            raise ValueError("the purpose is a non-empty text")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("the index is a non-negative integer")
        self._seed = seed
        self._purpose = purpose
        self._index = index
        self._counter = 0
        self._buffer = b""
        self._pos = 0

    def _take(self, count: int) -> int:
        """The next ``count`` bytes of the stream as a big-endian integer."""
        while len(self._buffer) - self._pos < count:
            fresh = _block(self._seed, self._purpose, self._index, self._counter)
            self._counter += 1
            self._buffer = self._buffer[self._pos :] + fresh
            self._pos = 0
        start = self._pos
        self._pos += count
        return int.from_bytes(self._buffer[start : self._pos], "big")

    def randbelow(self, n: int) -> int:
        """A uniform integer in ``0 .. n - 1``, by rejection sampling on the byte stream."""
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError("randbelow needs an integer n >= 1")
        if n == 1:
            return 0
        width = ((n - 1).bit_length() + 7) // 8
        space = 256**width
        limit = space - space % n
        while True:
            value = self._take(width)
            if value < limit:
                return value % n

    def randint(self, low: int, high: int) -> int:
        """A uniform integer in ``low .. high``, both ends included."""
        if low > high:
            raise ValueError("randint needs low <= high")
        return low + self.randbelow(high - low + 1)

    def shuffle[T](self, items: Iterable[T]) -> list[T]:
        """A new list with the items in uniformly random order (Fisher-Yates from the end)."""
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.randbelow(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def sample[T](self, items: Sequence[T], k: int) -> list[T]:
        """``k`` distinct items in the order they were drawn (partial Fisher-Yates)."""
        pool = list(items)
        if not 0 <= k <= len(pool):
            raise ValueError("sample size out of range")
        for i in range(k):
            j = i + self.randbelow(len(pool) - i)
            pool[i], pool[j] = pool[j], pool[i]
        return pool[:k]

    def choice[T](self, items: Sequence[T]) -> T:
        """One item, uniformly."""
        if not items:
            raise ValueError("choice from an empty sequence")
        return items[self.randbelow(len(items))]

    def token(self, alphabet: str, length: int) -> str:
        """A string of ``length`` characters drawn uniformly from ``alphabet``."""
        if not alphabet or length < 0:
            raise ValueError("token needs an alphabet and a length >= 0")
        return "".join(alphabet[self.randbelow(len(alphabet))] for _ in range(length))


def stream(seed: str, purpose: str, index: int = 0) -> Stream:
    """The stream of one (seed, purpose, index)."""
    return Stream(seed, purpose, index)
