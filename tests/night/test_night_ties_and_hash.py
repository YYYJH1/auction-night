"""Ties are decided by keys derived with hashlib, and by nothing else.

A tie has to be decided the same way on every machine and in every process, whatever order the
interpreter happens to iterate a dict or a set in, and whatever salt it hashes strings with. The
engine promises that the only input of a tie is the key dealt for the lot, and that the key is a
function of the night seed and the lot number through SHA-256.

Three checks hold the promise together:

1. The keys of a deal equal a derivation written out here with ``hashlib`` and nothing from the
   engine, following the construction in :mod:`auction_night.engine.rng`.
2. In a night where every seat bids the same number in every lot, every lot is a tie of all the
   seats that were asked, and the winner is the one with the lowest key of that derivation.
3. Processes started with different ``PYTHONHASHSEED`` values play the same nights and write
   files that are equal byte for byte, and equal to the file written in this process.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from engine_support import SEED_ALPHABET
from hypothesis import given, settings
from hypothesis import strategies as st
from night_support import StandIn, make_config, play, wild_players

from auction_night.bots import TruthfulBot
from auction_night.contracts import dumps_events
from auction_night.engine.deal import deal
from auction_night.engine.night import Answer, Request

TESTS = Path(__file__).resolve().parents[1]
SEATS = tuple(range(1, 9))
TABLE = [
    {"seat": s, "codename": f"座位{s}", "kind": "mock", "mock_policy": "honest"} for s in SEATS
]

# ------------------------------------------------ 1. the keys, derived from the seed by hand


def reference_blocks(seed: str, purpose: str, index: int, blocks: int) -> bytes:
    """The documented byte stream: SHA-256 of the JSON text of the five coordinates."""
    out = b""
    for counter in range(blocks):
        text = json.dumps(
            ["auction-night/rng@1", seed, purpose, index, counter],
            separators=(",", ":"),
            ensure_ascii=True,
        )
        out += hashlib.sha256(text.encode("ascii")).digest()
    return out


def reference_shuffle(seed: str, purpose: str, index: int, items: range) -> list[int]:
    """Fisher-Yates from the end, each index drawn by rejection sampling on the byte stream."""
    data = reference_blocks(seed, purpose, index, 4)
    position = 0
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        n = i + 1
        width = ((n - 1).bit_length() + 7) // 8
        limit = 256**width - 256**width % n
        while True:
            value = int.from_bytes(data[position : position + width], "big")
            position += width
            if value < limit:
                break
        j = value % n
        out[i], out[j] = out[j], out[i]
    return out


def reference_tie_keys(seed: str, lot: int) -> dict[int, int]:
    """Rank of every seat in the tie order of one lot (0 wins)."""
    order = reference_shuffle(seed, "tie", lot, range(1, 9))
    return {seat: position for position, seat in enumerate(order)}


seeds = st.text(alphabet=SEED_ALPHABET, min_size=1, max_size=40)


@given(seeds)
@settings(max_examples=40, deadline=None)
def test_the_tie_keys_of_a_deal_are_sha256_of_the_seed_and_the_lot_number(seed: str) -> None:
    config = make_config(TABLE)
    dealt = deal(config, seed=seed)
    for lot in dealt.lots:
        assert lot.tie_key == reference_tie_keys(seed, lot.lot)
        assert sorted(lot.tie_key.values()) == list(range(8)), "a permutation of the ranks"


def test_the_keys_change_with_the_seed_and_with_the_lot() -> None:
    keys = {tuple(reference_tie_keys("tie-a", lot).items()) for lot in range(1, 13)}
    assert len(keys) > 8, "the lots of a night do not share one order"
    assert reference_tie_keys("tie-a", 1) != reference_tie_keys("tie-b", 1)


# ---------------------------------------------- 2. a night in which every lot is a tie

TIE_BID = 7


def tying_player(seat: int, levels: int):
    """Bids the same in every lot: 7 in a sealed or button lot, the lowest level in a Dutch lot."""
    stand_in = StandIn(seat, TruthfulBot(seat), pledges=False)

    def act(request: Request) -> Answer:
        if request.phase in ("plan", "talk"):
            return stand_in.answer(request)
        if request.phase == "dutch":
            assert request.tier is not None
            return Answer(seat=seat, value=request.tier // levels)
        if request.phase == "english_revise":
            return Answer(seat=seat, value=request.facts_of(seat).exit_price)
        return Answer(seat=seat, value=TIE_BID)

    return act


def tie_night(seed: str):
    config = make_config(TABLE)
    levels = config.night.dutch.levels
    players = {seat: tying_player(seat, levels) for seat in SEATS}
    return play(config, deal(config, seed=seed), players)


@given(seeds)
@settings(max_examples=12, deadline=None)
def test_when_everybody_ties_the_lowest_key_of_the_hashlib_derivation_wins(seed: str) -> None:
    transcript = tie_night(seed)
    state = transcript.state
    assert len(state.records) == 12
    for record in state.records:
        hammer = record.outcome.hammer
        asked = list(record.outcome.asked)
        assert len(asked) == 8, "nobody runs out of budget on bids of 7 and the lowest level"
        assert not hammer.unsold and hammer.tie_broken
        assert hammer.tied == asked, "every seat is in the tie"
        keys = reference_tie_keys(seed, record.lot)
        assert hammer.winner == min(asked, key=lambda s: keys[s])


# ------------------------------------------------- 3. other processes, other hash salts


def produce(path: str) -> None:
    """Play fixed nights and write everything they produce into one file."""
    config = make_config()
    parts = [deal(config, seed=seed).digest() for seed in ("salt-a", "salt-b", "salt-c")]
    wild = play(config, deal(config, seed="salt-wild"), wild_players(config, 31337))
    parts.append(dumps_events(wild.events))
    parts.append(dumps_events(tie_night("salt-tie").events))
    Path(path).write_text("\n".join(parts), encoding="utf-8")


HASH_SEEDS = ("0", "1", "40961")
SCRIPT = (
    "import sys\n"
    f"sys.path[:0] = [{str(TESTS / 'engine')!r}, {str(TESTS / 'night')!r}]\n"
    "import test_night_ties_and_hash as module\n"
    "module.produce(sys.argv[1])\n"
    "print(hash('auction'))\n"
)


def test_other_hash_salts_write_the_same_bytes(tmp_path: Path) -> None:
    here = tmp_path / "here.txt"
    produce(str(here))
    jobs = []
    for salt in HASH_SEEDS:
        out = tmp_path / f"salt-{salt}.txt"
        env = {**os.environ, "PYTHONHASHSEED": salt, "CUDA_VISIBLE_DEVICES": ""}
        process = subprocess.Popen(
            [sys.executable, "-c", SCRIPT, str(out)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        jobs.append((salt, out, process))
    hashed = set()
    for salt, out, process in jobs:
        stdout, err = process.communicate(timeout=300)
        assert process.returncode == 0, f"PYTHONHASHSEED={salt}: {err}"
        assert out.read_bytes() == here.read_bytes(), f"PYTHONHASHSEED={salt} wrote other bytes"
        hashed.add(stdout.strip())
    assert len(hashed) == len(HASH_SEEDS), "the processes really did hash strings differently"
    assert len(here.read_bytes()) > 100_000, "the file holds whole nights, not a stub"
