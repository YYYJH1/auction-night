"""The findings of a night, from nights played by the mock policies and by scripted seats.

Four nights carry most of the weight, all made by the scripted policies of the model layer:

* **honest**: every model seat bids by its value, nobody pledges or arranges anything;
* **collude**: seats 2 and 4 arrange to take lots in turn in the chat and sit out each other's
  turn in the bids; seat 5 pledges not to bid and bids all the same (``backstab``); seat 6 pledges
  at random;
* **chat only**: the same arrangement in words, with honest bids;
* **bids only**: the same sitting out, in silence.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from judge_support import (
    ScriptedJudge,
    answer,
    claim,
    hybrid_night,
    mock_night,
    same_for_both,
)
from show_support import Script, play_script, scripted_config

from auction_night.contracts import Replay
from auction_night.contracts.events import (
    HammerEvent,
    Pledge,
    TalkEvent,
    canonical_json,
    dumps_events,
    loads_events,
    validate_log,
)
from auction_night.engine.deal import deal
from auction_night.engine.night import NightState, append_events
from auction_night.show.host import BANNED_WORDS
from auction_night.show.judge_input import JudgeInput, build_judge_input
from auction_night.show.judges import MockJudge, run_judges
from auction_night.show.pledges import judge_pledges
from auction_night.show.screening import NULL_THRESHOLD_MILLI, LotScreen
from auction_night.show.truth import Truth
from auction_night.show.verdicts import (
    MIN_RED_LOTS,
    Judging,
    collusion_report,
    collusion_verdict,
    run_judging,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "replays" / "sealed_four_lots.json"
HONEST = {2: "honest", 4: "honest", 5: "honest", 6: "honest"}
COLLUDERS = (2, 4)


def mock_judges() -> list[MockJudge]:
    return [MockJudge(0), MockJudge(1)]


def verdicts_of(judging: Judging, kind: str) -> list[Any]:
    return [v for v in judging.verdicts if v.verdict == kind]


def collusion_of(judging: Judging) -> list[Any]:
    return [v for v in judging.verdicts if v.verdict.startswith("collusion")]


# ======================================================================== the honest night


@pytest.fixture(scope="module")
def honest() -> Judging:
    return run_judging(mock_night("verdicts-honest", HONEST), mock_judges())


def test_an_honest_night_has_nothing_to_report(honest: Judging) -> None:
    assert honest.run.both and honest.verdicts == () and honest.pledges == ()
    assert honest.collusion.ring == () and honest.false_claims == ()
    assert not any(f.chat for f in honest.collusion.lots)
    # the judges still read the night and guessed missions
    assert all(d.mission_guesses is not None for d in honest.judges)


def test_the_events_of_a_quiet_night_are_the_judges_alone(honest: Judging) -> None:
    assert [e.type for e in honest.events] == ["judge_extract", "judge_extract"]


# ======================================================================== pledges


@pytest.fixture(scope="module")
def collude_state() -> NightState:
    return mock_night("verdicts-collude")


@pytest.fixture(scope="module")
def collude(collude_state: NightState) -> Judging:
    return run_judging(collude_state, mock_judges())


def test_a_pledge_that_was_broken_is_a_verdict_decided_by_program(
    collude_state: NightState, collude: Judging
) -> None:
    records = judge_pledges(collude_state.events)
    broken = [r for r in records if r.outcome == "violated"]
    assert broken and collude.pledges == tuple(records)
    found = verdicts_of(collude, "pledge_violation")
    assert len(found) == len(broken)
    by_seq = {e.seq: e for e in collude_state.events}
    for verdict, record in zip(found, sorted(broken, key=lambda r: r.seq), strict=True):
        assert verdict.seats == [record.seat]
        assert verdict.lots == sorted({record.lot, record.pledge.lot})
        assert verdict.intent == "unknown"  # the log holds what was bid, not why
        assert verdict.detail == record.detail
        assert (verdict.topic, verdict.gap_milli, verdict.threshold_milli) == (None, None, None)
        assert verdict.statistic_milli == {}
        talk, hammer = (by_seq[s] for s in verdict.evidence_seqs)
        assert isinstance(talk, TalkEvent) and talk.seq == record.seq
        assert talk.data.pledge == record.pledge and verdict.quotes == [talk.data.text]
        assert isinstance(hammer, HammerEvent) and hammer.lot == record.pledge.lot


def test_the_backstabber_breaks_its_word_every_lot_and_a_kept_pledge_is_no_finding(
    collude_state: NightState, collude: Judging
) -> None:
    backstab = [v for v in verdicts_of(collude, "pledge_violation") if v.seats == [5]]
    assert len(backstab) >= 10  # seat 5 pledges not to bid in every lot and bids in nearly all
    kept = [r for r in collude.pledges if r.outcome != "violated"]
    assert kept  # the night has pledges that were kept or could not be tested
    cited = {seq for v in verdicts_of(collude, "pledge_violation") for seq in v.evidence_seqs}
    assert not cited & {r.seq for r in kept}


def test_pledge_verdicts_do_not_need_the_judges() -> None:
    state = mock_night("verdicts-collude")
    judges = [ScriptedJudge(0, "a", [None]), ScriptedJudge(1, "b", ["没有 JSON"])]
    judging = run_judging(state, judges)
    assert not judging.run.both and len(judging.run.absent) == 2
    assert {e.type for e in judging.events} == {"judge_verdict"}  # no judge_extract at all
    assert {v.verdict for v in judging.verdicts} == {"pledge_violation"}  # no claim, no collusion
    full = run_judging(state, mock_judges())
    assert list(judging.verdicts) == verdicts_of(full, "pledge_violation")
    assert judging.accuracy == ()


def test_a_pledge_about_a_later_lot_names_both_lots() -> None:
    script = Script(pledges={(1, 2): Pledge(kind="no_bid", lot=4)}, bids={(4, 2): 30})
    _, _, transcript = play_script(script, seed="verdicts-later")
    judging = run_judging(transcript.state, mock_judges())
    (verdict,) = verdicts_of(judging, "pledge_violation")
    assert verdict.seats == [2] and verdict.lots == [1, 4]
    by_seq = {e.seq: e for e in transcript.events}
    talk, hammer = (by_seq[s] for s in verdict.evidence_seqs)
    assert talk.lot == 1 and hammer.lot == 4 and verdict.quotes == ["第1件"]
    assert verdict.detail.startswith("承诺第 4 件不出价，实际")


def test_a_pledge_with_no_words_has_no_quote() -> None:
    script = Script(
        say={(1, 3): ""},
        pledges={(1, 3): Pledge(kind="max_bid", lot=1, amount=10)},
        bids={(1, 3): 25},
    )
    _, _, transcript = play_script(script, seed="verdicts-silent")
    judging = run_judging(transcript.state, mock_judges())
    (verdict,) = verdicts_of(judging, "pledge_violation")
    assert verdict.seats == [3] and verdict.quotes == []


# ======================================================================== claims


def false_budget_claim(seat: int, lot: int, said: int = 50) -> dict[str, Any]:
    return claim(seat, lot, f"我的预算只有 {said}", "budget", number=said)


CLAIM_SEED = "verdicts-claims"


def test_a_claim_both_judges_found_and_the_deal_contradicts_is_a_false_claim() -> None:
    the_deal = deal(scripted_config(), seed=CLAIM_SEED)
    holds = 4 in the_deal.missions  # a seat with a mission cannot truthfully say it has none
    line = "我没有任务，真的" if holds else "我是收藏家，信我"
    wrong = (
        claim(4, 2, "我没有任务", "mission_kind", text_value="none")
        if holds
        else claim(4, 2, "我是收藏家", "mission_kind", text_value="collector")
    )
    script = Script(say={(2, 3): "我的预算只有 50", (2, 4): line})
    _, _, transcript = play_script(script, seed=CLAIM_SEED)
    judging = run_judging(
        transcript.state, same_for_both(answer([false_budget_claim(3, 2), wrong]))
    )
    found = verdicts_of(judging, "false_claim")
    assert [(v.seats, v.lots) for v in found] == [([3], [2]), ([4], [2])]
    budget, kind = found
    truth = Truth.of_events(transcript.events)
    assert truth.budgets[3] > 100  # the claim was far off
    assert budget.quotes == ["我的预算只有 50"] and budget.intent == "unknown"
    assert budget.detail.startswith("自称预算 50，当时实际还剩 ")
    left, opening = truth.remaining(3, 2), truth.budgets[3]
    assert budget.topic == "budget"
    assert budget.gap_milli == min(abs(50 - n) * 1000 // n for n in (left, opening))
    assert budget.gap_milli >= 100 and budget.statistic_milli == {}
    assert budget.threshold_milli is None
    assert kind.topic == "mission_kind" and kind.gap_milli is None, "a mission has no gap"
    by_seq = {e.seq: e for e in transcript.events}
    talk, hammer = (by_seq[s] for s in budget.evidence_seqs)
    assert talk.data.seat == 3 and talk.data.text == "我的预算只有 50" and hammer.lot == 2
    assert kind.detail.startswith("自称") and kind.quotes == [wrong["quote"]]


def test_a_false_valuation_names_its_topic_and_how_far_it_was_in_thousandths_of_the_tier() -> None:
    the_deal = deal(scripted_config(), seed=CLAIM_SEED)
    lot = next(entry for entry in the_deal.lots if not entry.is_blind and entry.tier == 100)
    said = 300
    line = f"我的估值是 {said}"
    script = Script(say={(lot.lot, 3): line})
    _, _, transcript = play_script(script, seed=CLAIM_SEED)
    wrong = claim(3, lot.lot, line, "valuation", number=said)
    judging = run_judging(transcript.state, same_for_both(answer([wrong])))
    (verdict,) = verdicts_of(judging, "false_claim")
    value = lot.values[3]
    assert value != said
    assert verdict.topic == "valuation"
    assert verdict.gap_milli == abs(said - value) * 1000 // lot.tier
    assert verdict.gap_milli >= 150 and verdict.seats == [3] and verdict.lots == [lot.lot]
    assert f"自称估值 {said}，实际是 {value}" in verdict.detail


def test_a_claim_only_one_judge_found_is_not_a_finding() -> None:
    _, _, transcript = play_script(Script(say={(2, 3): "我的预算只有 50"}), seed=CLAIM_SEED)
    one = ScriptedJudge(0, "a", [answer([false_budget_claim(3, 2)])])
    other = ScriptedJudge(1, "b", [answer()])
    assert verdicts_of(run_judging(transcript.state, [one, other]), "false_claim") == []


def test_a_claim_one_judge_was_unsure_about_is_not_a_finding() -> None:
    _, _, transcript = play_script(Script(say={(2, 3): "我的预算只有 50"}), seed=CLAIM_SEED)
    sure = false_budget_claim(3, 2)
    judges = [
        ScriptedJudge(0, "a", [answer([sure])]),
        ScriptedJudge(1, "b", [answer([{**sure, "ambiguous": True}])]),
    ]
    assert verdicts_of(run_judging(transcript.state, judges), "false_claim") == []


def test_a_true_claim_is_not_a_finding() -> None:
    _, _, first = play_script(Script(), seed=CLAIM_SEED)
    near = Truth.of_events(first.events).remaining(3, 2)
    line = f"我的预算还有 {near}"
    _, _, transcript = play_script(Script(say={(2, 3): line}), seed=CLAIM_SEED)
    said = answer([claim(3, 2, line, "budget", number=near)])
    assert verdicts_of(run_judging(transcript.state, same_for_both(said)), "false_claim") == []


def test_a_missing_judge_leaves_no_false_claim() -> None:
    _, _, transcript = play_script(Script(say={(2, 3): "我的预算只有 50"}), seed=CLAIM_SEED)
    said = answer([false_budget_claim(3, 2)])
    judges = [ScriptedJudge(0, "a", [said]), ScriptedJudge(1, "b", [None])]
    judging = run_judging(transcript.state, judges)
    assert verdicts_of(judging, "false_claim") == [] and judging.false_claims == ()


# ======================================================================== collusion, built by hand


@pytest.fixture(scope="module")
def ring_record() -> JudgeInput:
    return build_judge_input(mock_night("verdicts-collude").events)


def collusion_items(record: JudgeInput, lots: list[int]) -> list[dict[str, Any]]:
    """What a judge would extract as arrangements in some lots (the lines of the mock policies)."""
    items = MockJudge().answer(record)["extractions"]
    return [e for e in items if e["promise_class"] == "collusion" and e["lot"] in lots]


def screens(over: list[int], n_lots: int = 12, **overrides: Any) -> list[LotScreen]:
    """Lots in which the statistic of the ring (2, 4) is 850 (over) or 0 (not over)."""
    found = []
    for lot in range(1, n_lots + 1):
        loud = lot in over
        fields: dict[str, Any] = {
            "lot": lot,
            "tier": 100,
            "winner": 1,
            "counterfactual": 80 if loud else 50,
            "observed": 50,
            "ref": {1: 70, 2: 60, 4: 50},
            "bids": {1: 70, 2: 0, 4: 0} if loud else {1: 70, 2: 60, 4: 50},
        }
        found.append(LotScreen(**{**fields, **overrides}))
    return found


def report(record: JudgeInput, lots: list[int], over: list[int], **options: Any) -> Any:
    run = run_judges(record, same_for_both(answer(collusion_items(record, lots))))
    return collusion_report(run, screens(over), players=record.model_seats, **options), run


def test_both_channels_in_enough_lots_is_a_red_card(ring_record: JudgeInput) -> None:
    rep, _ = report(ring_record, [3, 4, 8], over=[4, 9, 12])
    assert rep.ring == (2, 4) and rep.judges_agree
    assert {f.lot: f.status for f in rep.lots if f.status} == {
        3: "suspect",  # the chat only
        4: "red",
        5: "suspect",  # the chat only: the arrangement of lot 4 also covers lot 5
        8: "suspect",
        9: "red",  # the arrangement of lot 8 covers lot 9
        12: "suspect",  # the bids only: nobody arranged anything near lot 12
    }
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.verdict == "collusion_red"
    assert verdict.seats == [2, 4] and verdict.lots == [4, 9]
    assert verdict.intent is None and "两侧都满足" in verdict.detail
    assert "另有 4 件只满足一侧" in verdict.detail
    assert f"阈值 {NULL_THRESHOLD_MILLI}" in verdict.detail
    # the verdict holds the statistic of every screened lot and the threshold it was held against
    assert verdict.statistic_milli == {n: 850 if n in (4, 9, 12) else 0 for n in range(1, 13)}
    assert verdict.threshold_milli == NULL_THRESHOLD_MILLI
    assert verdict.topic is None and verdict.gap_milli is None


def test_the_chat_covers_the_lot_of_the_promise_and_the_next_one(ring_record: JudgeInput) -> None:
    rep, _ = report(ring_record, [5], over=[])
    assert [f.lot for f in rep.lots if f.chat] == [5, 6]
    assert rep.quotes[0].lot == 5 and {q.seat for q in rep.quotes} == {2, 4}


def test_one_lot_with_both_channels_is_not_enough_for_a_red_card(ring_record: JudgeInput) -> None:
    rep, _ = report(ring_record, [3, 4], over=[4])
    assert [f.lot for f in rep.of_status("red")] == [4]  # the lot itself is red
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.verdict == "collusion_suspect"
    assert verdict.lots == [3, 4, 5] and verdict.seats == [2, 4]
    assert f"红牌要求至少 {MIN_RED_LOTS} 件两侧都满足，这里只有 1 件，记为疑似" in verdict.detail
    assert collusion_verdict(rep, ring_record, min_red_lots=1).verdict == "collusion_red"  # type: ignore[union-attr]


def test_the_chat_alone_is_a_suspect(ring_record: JudgeInput) -> None:
    rep, _ = report(ring_record, [3, 7], over=[])
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.verdict == "collusion_suspect"
    assert verdict.seats == [2, 4] and verdict.lots == [3, 4, 7, 8]
    assert "只有聊天一侧成立" in verdict.detail and "没有超过阈值" in verdict.detail
    assert verdict.quotes and all("让" in q for q in verdict.quotes)


def test_the_bids_alone_are_a_suspect_that_names_nobody(ring_record: JudgeInput) -> None:
    run = run_judges(ring_record, same_for_both(answer()))  # nobody found any arrangement
    rep = collusion_report(run, screens([6, 7]), players=ring_record.model_seats)
    assert rep.ring == () and [f.lot for f in rep.of_status("suspect")] == [6, 7]
    # with no ring the statistic is the discount alone: 300 coins of the tier, over the threshold
    assert {f.statistic_milli for f in rep.lots if f.over} == {300}
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.verdict == "collusion_suspect"
    assert verdict.seats == [] and verdict.lots == [6, 7] and verdict.quotes == []
    assert "只有出价一侧成立" in verdict.detail and "没有两位评委都认定的约定" in verdict.detail
    assert verdict.statistic_milli == {n: 300 if n in (6, 7) else 0 for n in range(1, 13)}


def test_bids_far_from_the_chat_name_the_ring_that_spoke_elsewhere(ring_record: JudgeInput) -> None:
    rep, _ = report(ring_record, [1], over=[9, 10])
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.verdict == "collusion_suspect"
    assert verdict.seats == [2, 4] and 9 in verdict.lots and 10 in verdict.lots


def test_neither_channel_is_no_verdict(ring_record: JudgeInput) -> None:
    run = run_judges(ring_record, same_for_both(answer()))
    rep = collusion_report(run, screens([]), players=ring_record.model_seats)
    assert collusion_verdict(rep, ring_record) is None
    assert all(f.status is None for f in rep.lots)


def test_the_threshold_is_strict() -> None:
    record = build_judge_input(mock_night("verdicts-collude").events)
    run = run_judges(record, same_for_both(answer()))
    at = [LotScreen(1, 1000, 1, 765, 500, {1: 1}, {1: 1})]  # discount 265 exactly
    over = [LotScreen(1, 1000, 1, 766, 500, {1: 1}, {1: 1})]
    assert not collusion_report(run, at, players=(1,), threshold_milli=265).lots[0].over
    assert collusion_report(run, over, players=(1,), threshold_milli=265).lots[0].over


def test_the_verdict_carries_the_threshold_it_was_given(ring_record: JudgeInput) -> None:
    run = run_judges(ring_record, same_for_both(answer()))
    rep = collusion_report(
        run, screens([6, 7]), players=ring_record.model_seats, threshold_milli=299
    )
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.threshold_milli == 299
    assert verdict.lots == [6, 7], "300 is strictly above 299"
    assert "阈值 299" in verdict.detail


def test_one_judge_alone_cannot_close_the_chat_channel(ring_record: JudgeInput) -> None:
    items = answer(collusion_items(ring_record, [3, 4, 5, 6]))
    judges = [ScriptedJudge(0, "a", [items]), ScriptedJudge(1, "b", [answer()])]
    run = run_judges(ring_record, judges)
    rep = collusion_report(run, screens([3, 4, 5, 6]), players=ring_record.model_seats)
    assert rep.ring == () and not any(f.chat for f in rep.lots)
    assert rep.of_status("red") == ()  # the bids alone are never a red card


def test_a_missing_judge_means_no_red_card_and_the_text_says_so(ring_record: JudgeInput) -> None:
    items = answer(collusion_items(ring_record, [3, 4, 5, 6]))
    judges = [ScriptedJudge(0, "a", [items]), ScriptedJudge(1, "b", [None])]
    run = run_judges(ring_record, judges)
    rep = collusion_report(run, screens([3, 4, 5, 6]), players=ring_record.model_seats)
    assert not rep.judges_agree and rep.ring == ()
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.verdict == "collusion_suspect"
    assert "有评委缺席，聊天一侧无法成立" in verdict.detail


def test_only_what_both_judges_are_sure_of_counts_as_an_arrangement(
    ring_record: JudgeInput,
) -> None:
    sure = collusion_items(ring_record, [3, 4])
    unsure = [{**item, "ambiguous": True} for item in sure]
    judges = [ScriptedJudge(0, "a", [answer(sure)]), ScriptedJudge(1, "b", [answer(unsure)])]
    run = run_judges(ring_record, judges)
    assert collusion_report(run, screens([]), players=ring_record.model_seats).ring == ()


def test_a_promise_about_ones_own_bid_is_no_arrangement(ring_record: JudgeInput) -> None:
    own = [{**item, "promise_class": "bid"} for item in collusion_items(ring_record, [3, 4])]
    run = run_judges(ring_record, same_for_both(answer(own)))
    assert collusion_report(run, screens([]), players=ring_record.model_seats).ring == ()


def test_a_bot_that_the_judges_quote_is_never_in_the_ring(ring_record: JudgeInput) -> None:
    items = collusion_items(ring_record, [3, 4])
    run = run_judges(ring_record, same_for_both(answer(items)))
    only_bots = collusion_report(run, screens([]), players=(7, 8))
    assert only_bots.ring == () and only_bots.quotes == ()


def test_the_quotes_of_a_verdict_are_the_words_of_the_room_without_repeats(
    ring_record: JudgeInput,
) -> None:
    rep, _ = report(ring_record, list(range(1, 13)), over=list(range(1, 13)))
    verdict = collusion_verdict(rep, ring_record)
    assert verdict is not None and verdict.verdict == "collusion_red"
    said = {line.text for lot in ring_record.lots for line in lot.talk}
    assert len(verdict.quotes) == len(set(verdict.quotes)) <= 6
    assert all(any(q in text for text in said) for q in verdict.quotes)
    assert verdict.evidence_seqs == sorted(set(verdict.evidence_seqs))


# ======================================================================== collusion, played


def test_seats_that_arrange_in_chat_and_sit_out_in_the_bids_get_the_red_card(
    collude_state: NightState, collude: Judging
) -> None:
    (verdict,) = collusion_of(collude)
    assert verdict.verdict == "collusion_red" and verdict.seats == [2, 4]
    assert len(verdict.lots) >= MIN_RED_LOTS
    assert collude.collusion.ring == COLLUDERS
    over = [f.lot for f in collude.collusion.lots if f.over]
    assert set(verdict.lots) == {f.lot for f in collude.collusion.lots if f.over and f.chat}
    assert set(verdict.lots) <= set(over)
    # every lot of the night is screened, and the verdict shows them all next to the threshold
    assert verdict.statistic_milli == {f.lot: f.statistic_milli for f in collude.collusion.lots}
    assert sorted(verdict.statistic_milli) == list(range(1, 13))
    assert verdict.threshold_milli == NULL_THRESHOLD_MILLI
    assert all(verdict.statistic_milli[n] > NULL_THRESHOLD_MILLI for n in verdict.lots)
    by_seq = {e.seq: e for e in collude_state.events}
    kinds = {by_seq[s].type for s in verdict.evidence_seqs}
    assert kinds == {"talk", "hammer"}
    assert verdict.quotes and set(verdict.quotes) <= {
        "这件让4号拿，下件轮到我",
        "这件我来，下件让2号",
        "这件让2号拿，下件轮到我",
        "这件我来，下件让4号",
    }


@pytest.mark.parametrize("seed", ["verdicts-collude", "verdicts-2", "verdicts-3", "verdicts-4"])
def test_the_red_card_does_not_depend_on_the_luck_of_one_seed(seed: str) -> None:
    judging = run_judging(mock_night(seed), mock_judges())
    verdicts = collusion_of(judging)
    assert [v.verdict for v in verdicts] == ["collusion_red"]
    assert verdicts[0].seats == [2, 4] and len(verdicts[0].lots) >= MIN_RED_LOTS


def test_an_arrangement_in_words_alone_is_a_suspect() -> None:
    state = hybrid_night("verdicts-2", bids={2: "honest", 4: "honest"})
    judging = run_judging(state, mock_judges())
    (verdict,) = collusion_of(judging)
    assert verdict.verdict == "collusion_suspect" and verdict.seats == [2, 4]
    assert judging.collusion.of_status("red") == ()
    assert "没有超过阈值" in verdict.detail


def test_sitting_out_in_silence_is_a_suspect_with_no_seat_to_name() -> None:
    state = hybrid_night("verdicts-3", talk={2: "honest", 4: "honest"})
    judging = run_judging(state, mock_judges())
    assert judging.collusion.ring == ()
    (verdict,) = collusion_of(judging)
    assert verdict.verdict == "collusion_suspect" and verdict.seats == []
    assert verdict.lots and "只有出价一侧成立" in verdict.detail


def test_the_honest_night_has_no_collusion_and_no_chat_channel(honest: Judging) -> None:
    assert collusion_of(honest) == []
    assert all(not f.chat for f in honest.collusion.lots)


# ======================================================================== the stage as a whole


def test_the_events_of_the_stage_join_the_log(collude_state: NightState, collude: Judging) -> None:
    kinds = [e.type for e in collude.events]
    assert kinds == ["judge_extract"] * 2 + ["judge_verdict"] * len(collude.verdicts)
    assert [e.seq for e in collude.events] == list(range(1, len(collude.events) + 1))
    assert all(e.vis == "audience" for e in collude.events)
    after, added = append_events(collude_state, collude.events)
    assert validate_log(after.events) == []
    assert [e.seq for e in added] == list(
        range(len(collude_state.events) + 1, len(collude_state.events) + len(added) + 1)
    )
    assert loads_events(dumps_events(after.events)) == list(after.events)


def test_a_verdict_about_one_lot_carries_that_lot() -> None:
    judging = run_judging(mock_night("verdicts-collude"), mock_judges())
    for event in judging.events:
        if event.type == "judge_verdict":
            expected = event.data.lots[0] if len(event.data.lots) == 1 else None
            assert event.lot == expected


def test_the_stage_is_deterministic(collude_state: NightState, collude: Judging) -> None:
    again = run_judging(collude_state, mock_judges())
    assert [canonical_json(e) for e in again.events] == [canonical_json(e) for e in collude.events]
    assert again.accuracy == collude.accuracy


def test_the_stage_reads_a_finished_night_only() -> None:
    state = mock_night("verdicts-collude")
    with pytest.raises(ValueError, match="over"):
        run_judging(replace(state, stage="plan"), mock_judges())  # type: ignore[arg-type]


def test_the_texts_of_the_findings_are_plain_facts(collude: Judging) -> None:
    banned = [w.lower() for w in BANNED_WORDS]
    for verdict in collude.verdicts:
        text = json.dumps(verdict.model_dump(mode="json"), ensure_ascii=False).lower()
        assert not any(word in text for word in banned)
        for word in ("相似", "学了", "一路的", "动机"):
            assert word not in text
        assert verdict.intent in (None, "unknown")  # no motive is ever stated


def test_the_parts_of_the_stage_fit_the_replay_contract(collude: Judging) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for name, items in (
        ("pledges", collude.pledges),
        ("judges", collude.judges),
        ("verdicts", collude.verdicts),
        ("judge_accuracy", collude.accuracy),
    ):
        raw[name] = [json.loads(canonical_json(item)) for item in items]
    replay = Replay.model_validate(raw)
    assert replay.verdicts == list(collude.verdicts) and replay.judges == list(collude.judges)
    assert replay.judge_accuracy == list(collude.accuracy) and replay.pledges == list(
        collude.pledges
    )
