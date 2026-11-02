"""The sizes behind the estimate of the show layer are those of the mock night.

``EstimateAssumptions`` (``auction_night/llm/accounting.py``) holds the size of a call of the host,
of a judge and of the content classifier, and the number of public texts of a night. They were
measured on the mock night, with the rule of the mock model: one token for two characters of the
prompt. These tests measure them again, so that a change of a prompt template or of the show layer
that moves a size by more than a seventh is found, and the estimate is brought up to date.
"""

import json
import statistics
from decimal import Decimal
from fractions import Fraction
from functools import cache

from export_support import MOCK_CONFIG, NO_GIT, mock_night

from auction_night.contracts import load_config
from auction_night.llm.accounting import EstimateAssumptions, calls_breakdown
from auction_night.show.content_scan import _SYSTEM as SCAN_SYSTEM
from auction_night.show.content_scan import (
    MAX_BATCH_ITEMS,
    ContentGate,
    Probe,
    batch_message,
    batches_of,
    public_texts,
)
from auction_night.show.host import LOT_CHARS, RECAP_HIGH, load_host_templates, lot_job, recap_job
from auction_night.show.judge_input import build_judge_input
from auction_night.show.judges import MAX_ECHO_CHARS, MockJudge, judge_job, load_judge_templates
from auction_night.show.replay_export import export_replay, show_parts

TOLERANCE = Fraction(15, 100)


def counted(text: str) -> int:
    """Tokens as the mock model counts them: one for two characters."""
    return max(1, (len(text) + 1) // 2)


@cache
def measured() -> dict[str, float]:
    """The sizes of the show layer on the mock night, in the units of ``EstimateAssumptions``."""
    night = mock_night()
    parts = show_parts(night.config, night.state, night.cards)
    done = export_replay(
        night.config,
        night.state,
        night.host.block,
        gate=ContentGate.builtin(),
        theory=night.theory,
        cards=night.cards,
        parts=parts,
        cassettes=[night.cassette],
        git=NO_GIT,
    )
    texts = public_texts(done.replay)
    host = load_host_templates()
    lots = [lot_job(card, host) for card in night.cards.lots]
    recap = recap_job(night.cards.night, host)
    record = build_judge_input(night.state.events)
    job = judge_job(0, record, load_judge_templates())
    answer = json.dumps(MockJudge().answer(record), ensure_ascii=False)
    batches = [
        counted(SCAN_SYSTEM)
        + counted(batch_message([Probe(number, item.kind, item.text) for number, item in batch]))
        for batch in batches_of(texts)
    ]
    return {
        "host_lot_input_tokens": statistics.mean(counted(j.system + j.user) for j in lots),
        "host_recap_input_tokens": counted(recap.system + recap.user),
        "judge_input_tokens": counted(job.system + job.user),
        "judge_output_tokens": counted(answer),
        "scan_batch_input_tokens": statistics.mean(batches),
        "scan_texts_per_night": len(texts),
    }


def test_the_sizes_of_the_estimate_are_those_of_the_mock_night() -> None:
    assumptions = EstimateAssumptions()
    for name, size in measured().items():
        assumed = getattr(assumptions, name)
        assert abs(size - assumed) <= TOLERANCE * assumed, (
            f"{name}: the mock night gives {size:.0f} and the estimate assumes {assumed}. "
            "Update EstimateAssumptions in auction_night/llm/accounting.py."
        )


def test_the_limits_in_the_estimate_are_those_of_the_show_layer() -> None:
    a = EstimateAssumptions()
    assert a.scan_batch_items == MAX_BATCH_ITEMS
    assert a.host_lot_output_tokens == LOT_CHARS // 2  # the host is asked for 80 characters
    assert a.host_recap_output_tokens == RECAP_HIGH // 2  # and for 500 in the recap
    assert a.judge_retry_tokens == MAX_ECHO_CHARS // 2 + 100  # the first answer, cut, and a note


def test_the_reference_night_is_the_mock_night() -> None:
    config = load_config(MOCK_CONFIG)
    assert EstimateAssumptions().reference_model_seats == len(config.model_seats) == 6
    assert sum(calls_breakdown(config).values(), Fraction(0)) == Fraction(109, 4)
    assert EstimateAssumptions().reference_calls == Decimal("27.25")
