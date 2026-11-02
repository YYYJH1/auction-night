"""A night that its cost cap cut in the middle of a lot, from the model layer to the page.

The mock night is priced at one micro-dollar a token and given a cap that is reached by the last
talk call of lot 6. The gate then fires between the talk and the bids of that lot: the model seats
have spoken in lot 6 and bid nothing, and from lot 7 on they neither speak nor bid. The rule bots
play the night to its end.

In such a night the show layer must not hold the muted lots against the chat in the collusion
screen, keep or break pledges about them, tell the silence as abstentions on the fact cards, or
praise the bargains that the bots made against a silent table. Every check below follows the whole
path: the log of the model layer, the fact cards, the judges (mock ones), the awards, the
highlights, the replay file and, when Node is installed, the page that plays it.
"""

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from e2e_support import REPO, make_config, mock_prices

from auction_night.contracts import NightConfig
from auction_night.engine.muted import muted_actions
from auction_night.engine.night import NightState
from auction_night.llm.cassette import Cassette
from auction_night.llm.nightrun import build_rig
from auction_night.show.content_scan import ContentGate
from auction_night.show.factcard import FactCards, NightData, build_fact_cards
from auction_night.show.host import lot_job, make_host, run_host
from auction_night.show.replay_export import (
    Exported,
    GitInfo,
    ShowParts,
    export_replay,
    show_parts,
)
from auction_night.theory import analyse_night

MODELS = (1, 2, 3, 4, 5, 6)
FIRST_CUT = 6
LAST = 12
CAP_HEAD = "费用上限：这一夜从第 6 件起，模型座位因为费用上限不再行动。"


def cap_line(seats: str) -> str:
    """The line of the fact card: seat 6 is out of money in lot 6, so only its talk is skipped."""
    return f"{CAP_HEAD}本件被跳过的座位：{seats} 号，这不是它们自己的选择。"


PAGE = """
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [root, replay] = process.argv.slice(1);
const load = (path) => import(pathToFileURL(`${root}/${path}`).href);
const { indexReplay } = await load('viewer/lib/replay.js');
const { createStage } = await load('viewer/ui/stage.js');
const { FakeDocument } = await load('tests/viewer/fake_dom.js');
const model = indexReplay(JSON.parse(readFileSync(replay, 'utf8')));
const notes = [];
for (const lens of [true, false]) {
  const doc = new FakeDocument();
  const main = doc.createElement('main');
  const stage = createStage(doc, main, model);
  for (const lot of model.lotNos) {
    stage.render(lot, lens);
    const note = main.querySelector('.cap-note');
    notes.push({ lot, lens, note: note === null ? null : note.textContent });
  }
}
console.log(JSON.stringify(notes));
"""


@dataclass(frozen=True)
class Night:
    """A night played through the model layer, and everything the show layer makes of it."""

    config: NightConfig
    state: NightState
    cassette: Cassette
    cards: FactCards
    parts: ShowParts
    exported: Exported


def play_through(config: NightConfig) -> tuple[NightState, Cassette]:
    """The night through the model layer with priced mock models; the final state and cassette."""
    cassette = Cassette.record()
    rig = build_rig(config, cassette, prices=mock_prices())
    runner = rig.runner()
    try:
        asyncio.run(rig.play(runner))
    finally:
        runner.close()
    return rig.driver.state, cassette


def show_of(config: NightConfig, state: NightState, cassette: Cassette) -> Night:
    theory = analyse_night(state)
    cards = build_fact_cards(state, theory=theory)
    parts = show_parts(config, state, cards)
    exported = export_replay(
        config,
        state,
        run_host(cards, make_host(config)).block,
        gate=ContentGate.from_config(config),
        theory=theory,
        cards=cards,
        parts=parts,
        cassettes=[cassette],
        git=GitInfo(),
        reveal_seed=True,
    )
    return Night(config, state, cassette, cards, parts, exported)


@pytest.fixture(scope="module")
def plain() -> Night:
    """The mock night with no cap."""
    config = make_config()
    state, cassette = play_through(config)
    return show_of(config, state, cassette)


@pytest.fixture(scope="module")
def cut(plain: Night) -> Night:
    """The same night with a cap that the last talk call of lot 6 reaches."""
    last_talk = max(
        e.key.step
        for e in plain.cassette.entries
        if e.key.lot == FIRST_CUT and e.key.phase == "talk"
    )
    cap = sum(e.cost_microusd or 0 for e in plain.cassette.entries if e.key.step <= last_talk)
    config = make_config(limits={"max_cost_per_night_usd": cap / 1_000_000})
    state, cassette = play_through(config)
    return show_of(config, state, cassette)


def collusion(night: Night) -> list:
    return [v for v in night.parts.verdicts if v.verdict.startswith("collusion")]


# ================================================================================ the premise


def test_the_gate_cuts_lot_6_between_its_talk_and_its_bids(cut: Night) -> None:
    events = cut.state.events
    assert cut.state.budget_aborted
    (fired,) = [e for e in events if e.type == "cost_event" and e.data.kind == "cap_reached"]
    assert fired.lot == FIRST_CUT
    muted = muted_actions(events)
    assert muted.first_lot == FIRST_CUT
    assert set(muted.talks) == set(range(FIRST_CUT + 1, LAST + 1))
    assert set(muted.bids) == set(range(FIRST_CUT, LAST + 1))
    assert muted.talks[LAST] == frozenset(MODELS)
    assert muted.bids[FIRST_CUT] == frozenset({1, 2, 3, 4, 5})  # seat 6 is out of money by then
    first = min(e.seq for e in events if e.type == "action" and e.data.reason == "budget_aborted")
    assert fired.seq < first


def test_the_lots_before_the_cut_are_the_lots_of_the_night_without_a_cap(
    plain: Night, cut: Night
) -> None:
    def actions(night: Night, lots: range) -> list:
        return [e.data for e in night.state.events if e.type == "action" and e.lot in lots]

    assert actions(cut, range(1, FIRST_CUT)) == actions(plain, range(1, FIRST_CUT))
    assert not muted_actions(plain.state.events)


# =============================================================================== the screening


def test_a_lot_in_which_the_models_were_muted_is_never_a_suspect(plain: Night, cut: Night) -> None:
    (verdict,) = collusion(cut)
    assert verdict.verdict == "collusion_suspect"
    assert set(verdict.lots) <= set(range(1, FIRST_CUT))
    assert set(verdict.statistic_milli) == set(range(1, FIRST_CUT))
    assert "第 6、7、8、9、10、11、12 件的模型座位被费用上限静音，没有筛查。" in verdict.detail
    # the same night without the cap screens every lot, and holds the lots of the cut to the chat
    (whole,) = collusion(plain)
    assert set(whole.statistic_milli) == set(range(1, LAST + 1))
    assert whole.verdict == "collusion_red" and set(whole.lots) & set(range(FIRST_CUT, LAST + 1))


# ============================================================================== the pledges


def test_a_muted_seat_neither_keeps_nor_breaks_a_pledge(cut: Night) -> None:
    muted = muted_actions(cut.state.events)
    night = cut.cards.night.data
    assert isinstance(night, NightData)
    about_the_cut = [p for p in night.pledges if muted.bid_muted(p.pledge.lot, p.seat)]
    assert len(about_the_cut) >= 2, "the premise: pledges about the lot whose bids were skipped"
    assert {p.outcome for p in about_the_cut} == {"void"}
    assert all("费用上限" in p.detail for p in about_the_cut)
    # the pledges of the lots before the cut are judged as ever
    assert {p.outcome for p in night.pledges if p.lot < FIRST_CUT} >= {"kept", "violated"}


# ============================================================================== the fact cards


def test_every_lot_of_the_cut_has_the_line_of_the_cap_and_no_abstention(cut: Night) -> None:
    for lot in range(FIRST_CUT, LAST + 1):
        card = cut.cards.lot(lot)
        line = cap_line("1、2、3、4、5" if lot == FIRST_CUT else "1、2、3、4、5、6")
        assert line in card.text.splitlines(), lot
        assert "弃权" not in card.text and "费用上限，没有行动" in card.text
        assert line in lot_job(card).user.splitlines()  # the host is given it, as it is
        assert "费用上限已满，模型座位没有行动。" in card.headline
    for lot in range(1, FIRST_CUT):
        assert "费用上限" not in cut.cards.lot(lot).text
    night = cut.cards.night
    assert "从第 6 件起，模型座位因为费用上限不再行动。" in night.text
    assert "弃权（费用上限）" not in "".join(card.text for card in (*cut.cards.lots, night))


# ================================================================================ the show


def test_no_highlight_is_a_bargain_or_a_broken_plan_of_the_cut(cut: Night) -> None:
    lights = cut.parts.highlights
    assert lights, "the lots before the cut still have their highlights"
    for light in lights:
        if light.lot is not None and light.lot >= FIRST_CUT:
            assert light.category not in ("bargain", "plan_contradiction", "backstab"), light


def test_the_counts_of_a_seat_leave_out_its_skipped_actions(cut: Night) -> None:
    for score in cut.exported.replay.scores:
        own = [e.data for e in cut.state.events if e.type == "action" and e.data.seat == score.seat]
        asked = [d for d in own if not (d.status == "abstain" and d.reason == "budget_aborted")]
        assert score.stats.actions == len([d for d in asked if d.reason != "not_asked"])


# ================================================================================ the replay


def test_the_replay_records_which_bids_the_cap_took_and_gives_them_no_reference_numbers(
    cut: Night,
) -> None:
    replay = cut.exported.replay
    assert replay.night.status == "budget_aborted" and not replay.night.ranked
    for lot in replay.timeline:
        skipped = {a.seat for a in lot.actions if a.reason == "budget_aborted"}
        if lot.lot < FIRST_CUT:
            assert not skipped
            continue
        assert skipped == {s for s in lot.active if s in MODELS}, lot.lot
        assert {a.status for a in lot.actions if a.seat in skipped} == {"abstain"}
        assert skipped.isdisjoint(lot.overlay.regret) and skipped.isdisjoint(lot.overlay.excess)
        assert lot.overlay.to_highest_value is None
        assert skipped <= set(lot.overlay.ref_bids)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_page_says_so_on_every_lot_of_the_cut_and_on_no_other(
    cut: Night, tmp_path: Path
) -> None:
    replay = tmp_path / "replay.json"
    replay.write_text(cut.exported.text, encoding="utf-8")
    done = subprocess.run(
        ["node", "--input-type=module", "-e", PAGE, str(REPO), str(replay)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    notes = json.loads(done.stdout)
    assert len(notes) == 2 * LAST  # every lot, with the lens on and off
    for entry in notes:
        if entry["lot"] < FIRST_CUT:
            assert entry["note"] is None, entry
            continue
        assert entry["note"] is not None, entry
        assert entry["note"].startswith(
            "费用上限：这一夜从第 6 件起，模型座位因为费用上限不再行动。"
        )
        assert entry["note"].endswith(" 号。")
