"""night.memory: the observation with the seat's own opening plan and the pledges in force.

``off`` is the default and adds nothing. The first half of this file pins that: the digests below
were taken from the observations that release 0.1.1 produced, before the setting existed, for the
fixture night and for two real nights of season s1, and the digests of the configurations and of
the templates stay what the published replays carry. The second half is the section itself: whose
plan it shows, which pledges it lists and which it leaves out.
"""

import copy
import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from llm_night import LLM_SEATS, Night, build_night, make_settings, stub_rules

from auction_night.contracts import AnyEvent, NightConfig, Replay, config_hash, load_config
from auction_night.contracts.act import PHASE_TABLE
from auction_night.contracts.config import MEMORY_MODES
from auction_night.contracts.events import ActionEvent, GuessEvent, loads_events, parse_event
from auction_night.contracts.mechanism import PHASES
from auction_night.engine.rules_text import common_rule_text, rule_with_example
from auction_night.llm.render import (
    RenderError,
    RenderSettings,
    render_observation,
    render_prompt,
    render_system,
)
from auction_night.prompts import (
    OPTIONAL_GROUPS,
    TemplateError,
    TemplateSet,
    load_templates,
    templates_digest,
)

REPO = Path(__file__).resolve().parents[2]
SEASON = REPO / "seasons" / "s1"
HEAD = "【你的开场计划与仍然有效的誓约】"
PLEDGES = "仍然有效的誓约（公开，涉及本件和之后的拍品）："
NO_PLAN = "你没有交开场计划。"

# Taken at release 0.1.1, before the setting existed, with the helpers of this file.
FIXTURE_DIGEST = "sha256:e3e16ed9419f197423d04af79544a387e1634c0405d24f0bffa9349486e27168"
FIXTURE_RENDERED = 312
REAL_DIGESTS = {
    "s1-d01-r0": ("sha256:65c4b881c50af0ad22eb647a6ccd477d632ee77f66550dc3963325b3b36b5b3f", 160),
    "s1-d01-r3": ("sha256:9695b3579525318e11b159859db009ef144080080b6b42437dc5c37051866420", 165),
}
# The digest of the seat templates that every published replay of season s1 carries.
SEAT_TEMPLATES_DIGEST = "sha256:efd9d9f3c050dbb50bd699e74279aaa031f2df71699550ef39bde306b93fb3fc"


def digest_of(chunks: list[str]) -> str:
    return "sha256:" + hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()


def fixture_digest(settings: RenderSettings) -> tuple[str, int]:
    """Every observation that the fixture night can ask of its six model seats: each seat, each
    moment of the night, each phase (a pair that cannot be asked gives the error it raises)."""
    night = build_night()
    events = night.events()
    chunks, rendered = [], 0
    for seat in LLM_SEATS:
        for mark, seq in sorted(night.marks.items(), key=lambda item: (item[1], item[0])):
            for phase in PHASES:
                try:
                    text = render_observation(
                        events, seat, phase, settings, guess_target=seat % 8 + 1, as_of_seq=seq
                    )
                    rendered += 1
                except RenderError as exc:
                    text = f"RenderError: {exc}"
                chunks.append(f"== {seat} {mark} {phase}\n{text}\n")
    return digest_of(chunks), rendered


def real_night(name: str) -> tuple[NightConfig, list[AnyEvent]]:
    config = load_config(SEASON / name / "config.yaml")
    events = loads_events((SEASON / name / "events.jsonl").read_text(encoding="utf-8"))
    return config, list(events)


def real_digest(config: NightConfig, events: list[AnyEvent]) -> tuple[str, int]:
    """The system prompt of every model seat of a played night, and the observation at each of
    its actions, as the log stood just before the action."""
    settings = RenderSettings.from_config(config, rule_with_example, common_rules=common_rule_text)
    targets = {(e.data.seat, e.lot): e.data.target for e in events if isinstance(e, GuessEvent)}
    chunks, rendered = [], 0
    for seat in [s.seat for s in config.seats if s.kind == "llm"]:
        chunks.append(f"== system {seat}\n{render_system(events, seat, settings)}\n")
        for event in events:
            if not isinstance(event, ActionEvent) or event.data.seat != seat:
                continue
            phase = event.data.phase
            target = targets.get((seat, event.lot))
            these = settings
            if phase == "talk" and target is None:
                these = dataclasses.replace(settings, mindreading=False)
            try:
                text = render_observation(
                    events, seat, phase, these, guess_target=target, as_of_seq=event.seq - 1
                )
                rendered += 1
            except RenderError as exc:
                text = f"RenderError: {exc}"
            chunks.append(f"== {seat} {event.seq} {phase}\n{text}\n")
    return digest_of(chunks), rendered


# ======================================================================== off changes nothing


def test_memory_is_off_unless_it_is_asked_for(mock_config: NightConfig) -> None:
    assert MEMORY_MODES == ("off", "plan_pledges")
    assert make_settings().memory == "off"
    assert mock_config.night.memory == "off"
    assert RenderSettings.from_config(mock_config, stub_rules).memory == "off"


def test_the_observations_of_the_fixture_night_are_the_bytes_of_release_0_1_1() -> None:
    assert fixture_digest(make_settings()) == (FIXTURE_DIGEST, FIXTURE_RENDERED)
    assert fixture_digest(make_settings(memory="off")) == (FIXTURE_DIGEST, FIXTURE_RENDERED)


@pytest.mark.parametrize("name", sorted(REAL_DIGESTS))
def test_the_prompts_of_a_real_night_are_the_bytes_of_release_0_1_1(name: str) -> None:
    config, events = real_night(name)
    assert config.night.memory == "off"
    assert real_digest(config, events) == REAL_DIGESTS[name]


def test_the_digest_of_the_seat_templates_is_the_one_the_published_replays_carry() -> None:
    assert templates_digest() == templates_digest("zh") == SEAT_TEMPLATES_DIGEST
    assert load_templates().digest == SEAT_TEMPLATES_DIGEST
    nights = sorted(SEASON.glob("s1-d01-r*"))
    assert len(nights) == 6
    for night in nights:
        manifest = json.loads((night / "replay.json").read_text(encoding="utf-8"))["manifest"]
        assert manifest["prompt_hash"] == SEAT_TEMPLATES_DIGEST, night.name


def test_the_config_hash_of_a_published_night_is_the_one_its_replay_carries() -> None:
    for night in sorted(SEASON.glob("s1-d01-r*")):
        config = load_config(night / "config.yaml")
        replay = Replay.model_validate_json((night / "replay.json").read_text(encoding="utf-8"))
        assert config_hash(config) == replay.manifest.config_hash, night.name


def test_a_setting_that_is_on_is_part_of_the_config_hash(mock_config: NightConfig) -> None:
    on = mock_config.model_copy(
        update={"night": mock_config.night.model_copy(update={"memory": "plan_pledges"})}
    )
    assert config_hash(on) != config_hash(mock_config)
    off = on.model_copy(update={"night": on.night.model_copy(update={"memory": "off"})})
    assert config_hash(off) == config_hash(mock_config)


# ================================================================== the texts and their digest


def test_the_texts_of_the_section_are_in_the_digest_only_when_the_night_uses_them() -> None:
    base = load_templates()
    assert OPTIONAL_GROUPS == {"memory": "observe.memory_"}
    assert sorted(key for key in base.texts if key.startswith("observe.memory_")) == [
        "observe.memory_head",
        "observe.memory_plan",
        "observe.memory_plan_left",
        "observe.memory_plan_none",
        "observe.memory_pledge",
        "observe.memory_pledges_head",
    ]
    assert templates_digest("zh", "memory") != templates_digest()
    texts = dict(base.texts)
    texts["observe.memory_head"] += "。"
    edited = TemplateSet(base.lang, base.version, texts)
    assert edited.digest == base.digest, "a text that the night does not use does not count"
    assert edited.digest_with("memory") != base.digest_with("memory")
    texts["observe.none"] += "。"
    assert TemplateSet(base.lang, base.version, texts).digest != base.digest


def test_an_optional_group_that_does_not_exist_is_an_error() -> None:
    with pytest.raises(TemplateError, match="no optional group 'recall'"):
        templates_digest("zh", "recall")


def test_every_text_of_the_section_takes_the_values_that_the_renderer_gives_it() -> None:
    texts = load_templates()
    assert texts.format("observe.memory_plan", lot=3, priority=2, budget=90) == (
        "你的开场计划，第 3 件：重视程度 2（0 到 3），打算最多花 90。"
    )
    assert texts.format("observe.memory_plan_left", total=500).endswith("合计：500。")
    assert texts.format("observe.memory_pledge", seat=4, codename="拾光", text="x") == (
        "4 号「拾光」立誓：x"
    )
    for key in ("observe.memory_head", "observe.memory_plan_none", "observe.memory_pledges_head"):
        assert texts.get(key) == texts.format(key)


# ============================================================================ the section


def planned(seat: int, lot: int) -> int:
    """A planned amount that belongs to one seat and one lot and looks like nothing else."""
    return 7000 + 100 * seat + lot


# (lot where it is said, seat) -> pledge. Seat 2 pledges about the lot in progress, seats 1, 5 and 6
# about lots to come, seat 3 (in lot 3) and seat 6 (in lot 4) about lot 7, outside the chat window
# by lot 6. Seat 4 names lot 5, which is decided by then: the engine would not take that pledge,
# and the section must leave it out all the same.
PLEDGES_MADE = {
    (3, 3): {"kind": "no_bid", "lot": 7},
    (4, 6): {"kind": "no_bid", "lot": 7},
    (6, 6): {"kind": "no_bid", "lot": 8},
    (6, 2): {"kind": "no_bid", "lot": 6},
    (6, 1): {"kind": "let_win", "lot": 9, "to": 3},
    (6, 5): {"kind": "max_bid", "lot": 8, "amount": 77},
    (6, 4): {"kind": "no_bid", "lot": 5},
}
# The pledges in force once lot 6 has been talked through, in the order the section lists them:
# by the lot they are about, and within a lot by the moment they were made.
IN_FORCE = [
    "2 号「阿铁」立誓：对第 6 件不出价",
    "3 号「小满」立誓：对第 7 件不出价",
    "6 号「麦芽」立誓：对第 7 件不出价",
    "6 号「麦芽」立誓：对第 8 件不出价",
    "5 号「青柠」立誓：对第 8 件出价不超过 77",
    "1 号「鲸落」立誓：让 3 号拿下第 9 件",
]


def pledge_night() -> tuple[Night, list[AnyEvent]]:
    """The fixture night with plans that tell the seats apart and the pledges of PLEDGES_MADE."""
    night = build_night()
    for raw in night.raw:
        data = raw["data"]
        if raw["type"] == "plan":
            seat = data["seat"]
            data["priorities"] = [(seat + lot) % 4 for lot in range(1, 13)]
            data["budget_plan"] = [planned(seat, lot) for lot in range(1, 13)]
        elif raw["type"] == "talk" and (raw["lot"], data["seat"]) in PLEDGES_MADE:
            data["pledge"] = PLEDGES_MADE[(raw["lot"], data["seat"])]
    return night, night.events()


def memory_settings(**overrides: Any) -> RenderSettings:
    return make_settings(memory="plan_pledges", **overrides)


def observe(
    night: Night, events: list[AnyEvent], seat: int, phase: Any, mark: str, **options: Any
) -> str:
    """The observation of ``seat`` at a named moment, with the section on."""
    return render_observation(
        events,
        seat,
        phase,
        memory_settings(),
        guess_target=options.pop("guess_target", seat % 8 + 1),
        as_of_seq=night.marks[mark],
    )


def section_of(text: str) -> list[str]:
    """The lines of the memory section of an observation."""
    (block,) = [b for b in text.split("\n\n") if b.startswith(HEAD)]
    return block.split("\n")


def numbers_in(text: str) -> set[int]:
    return {int(found) for found in re.findall(r"\d+", text)}


def test_the_section_shows_the_plan_for_the_lot_in_progress_and_the_pledges_in_force() -> None:
    night, events = pledge_night()
    lines = section_of(observe(night, events, 3, "sealed", "lot6_talk_done"))
    left = sum(planned(3, lot) for lot in range(6, 13))
    assert lines == [
        HEAD,
        f"你的开场计划，第 6 件：重视程度 {(3 + 6) % 4}（0 到 3），打算最多花 {planned(3, 6)}。",
        f"计划里本件和之后各件打算最多花的合计：{left}。",
        PLEDGES,
        *IN_FORCE,
    ]


def test_the_section_sits_after_the_monologues_and_before_the_instruction() -> None:
    night, events = pledge_night()
    text = observe(night, events, 3, "sealed", "lot6_talk_done")
    order = ["【往期结果】", "【公开频道", "【你最近两次的内心独白】", HEAD, "【现在要做】"]
    positions = [text.index(part) for part in order]
    assert positions == sorted(positions) and text.count(HEAD) == 1


def test_in_an_english_revision_the_section_comes_before_the_state_of_the_clock() -> None:
    night, events = pledge_night()
    text = observe(night, events, 3, "english_revise", "lot2_before_revise")
    assert text.index(HEAD) < text.index("【本件进行中】") < text.index("【现在要做】")


def test_the_plan_line_and_the_amount_left_follow_the_lot_in_progress() -> None:
    night, events = pledge_night()
    for lot in range(1, 7):
        lines = section_of(observe(night, events, 5, "talk", f"lot{lot}_open"))
        assert lines[1].startswith(f"你的开场计划，第 {lot} 件：")
        assert lines[1].endswith(f"打算最多花 {planned(5, lot)}。")
        left = sum(planned(5, n) for n in range(lot, 13))
        assert lines[2] == f"计划里本件和之后各件打算最多花的合计：{left}。"


def test_the_plan_is_the_seats_own_and_no_other_plan_or_private_text_is_shown() -> None:
    night, events = pledge_night()
    asked = 0
    for mark in ("lot2_open", "lot2_before_revise", "lot4_done", "lot5_open", "lot6_talk_done"):
        for seat in LLM_SEATS:
            for phase in ("talk", "english_revise", "dutch", "sealed"):
                try:
                    prompt = render_prompt(
                        events,
                        seat,
                        phase,
                        memory_settings(),
                        guess_target=seat % 8 + 1,
                        as_of_seq=night.marks[mark],
                    )
                except RenderError:
                    continue  # the phase is not played in that lot
                asked += 1
                assert HEAD in prompt.observation
                text = prompt.system + "\n" + prompt.observation
                found = numbers_in(text)
                for other in range(1, 9):
                    theirs = {planned(other, lot) for lot in range(1, 13)}
                    assert (found & theirs == set()) == (other != seat), (mark, seat, phase, other)
                    assert f"THOUGHT-S{other}-" not in text or other == seat
                    assert f"PERSONA-S{other}" not in text
    assert asked >= 40


def test_a_pledge_is_listed_in_whatever_lot_it_was_made_and_by_whoever_made_it() -> None:
    night, events = pledge_night()
    text = observe(night, events, 6, "sealed", "lot6_talk_done")
    lines = section_of(text)
    assert lines[4:] == IN_FORCE
    assert "[第 3 件]" not in text and "[第 4 件]" not in text, "lots 3 and 4 left the chat window"


def test_a_pledge_of_a_lot_that_is_decided_or_past_is_left_out() -> None:
    night, events = pledge_night()
    lines = section_of(observe(night, events, 3, "sealed", "lot6_talk_done"))
    assert not any("第 5 件" in line for line in lines), "seat 4 names the decided lot 5"
    # by the open of lot 5 the pledges of lots 1 to 3 in the fixture night are over as well
    early = section_of(observe(night, events, 3, "talk", "lot5_open"))
    assert early[3:] == [
        PLEDGES,
        "3 号「小满」立誓：对第 7 件不出价",
        "6 号「麦芽」立誓：对第 7 件不出价",
    ]


def test_a_pledge_of_the_lot_in_progress_ends_with_the_hammer() -> None:
    night, events = pledge_night()
    during = section_of(observe(night, events, 3, "english_revise", "lot2_before_revise"))
    assert "2 号「阿铁」立誓：对第 2 件出价不超过 100" in during
    after = section_of(observe(night, events, 3, "talk", "lot3_open"))
    assert not any("第 2 件" in line for line in after[3:])


def test_what_has_not_been_said_yet_is_not_in_the_section() -> None:
    night, events = pledge_night()
    lines = section_of(observe(night, events, 3, "talk", "lot6_talk_of_seat_3"))
    assert lines[4:] == [
        "2 号「阿铁」立誓：对第 6 件不出价",
        "3 号「小满」立誓：对第 7 件不出价",
        "6 号「麦芽」立誓：对第 7 件不出价",
        "6 号「麦芽」立誓：对第 8 件不出价",
        "1 号「鲸落」立誓：让 3 号拿下第 9 件",
    ]  # seats 3, 4 and 5 have not spoken in lot 6


def without_plan_of_seat_3(*, asked: bool) -> tuple[Night, list[AnyEvent]]:
    """The pledge night in which seat 3 handed in no plan. Asked, it has an action in the plan
    phase and no plan, as after an abstention. Not asked, it has neither, as in a night without
    the plan phase."""
    night, _ = pledge_night()
    night.raw = [
        raw
        for raw in night.raw
        if not (raw["type"] == "plan" and raw["data"]["seat"] == 3)
        and (
            asked
            or not (
                raw["type"] == "action"
                and raw["data"]["phase"] == "plan"
                and raw["data"]["seat"] == 3
            )
        )
    ]
    return night, night.events()


def test_a_seat_that_was_asked_for_a_plan_and_gave_none_is_told_so() -> None:
    night, events = without_plan_of_seat_3(asked=True)
    lines = section_of(observe(night, events, 3, "sealed", "lot6_talk_done"))
    assert lines == [HEAD, NO_PLAN, PLEDGES, *IN_FORCE]
    other = section_of(observe(night, events, 4, "sealed", "lot6_talk_done"))
    assert other[1].startswith("你的开场计划，第 6 件：") and NO_PLAN not in other


def test_a_night_without_a_plan_phase_says_nothing_about_a_plan() -> None:
    night, events = without_plan_of_seat_3(asked=False)
    lines = section_of(observe(night, events, 3, "sealed", "lot6_talk_done"))
    assert lines == [HEAD, PLEDGES, *IN_FORCE]


def test_a_rule_bot_is_asked_for_no_plan_and_still_sees_the_pledges() -> None:
    night, events = pledge_night()
    lines = section_of(observe(night, events, 7, "sealed", "lot6_talk_done"))
    assert lines == [HEAD, PLEDGES, *IN_FORCE]


def test_without_any_pledge_in_force_the_section_says_none() -> None:
    night = build_night()
    lines = section_of(observe(night, night.events(), 3, "sealed", "lot6_talk_done"))
    assert lines[-2:] == [PLEDGES, "（暂无）"]


def test_the_opening_plan_phase_is_the_same_with_the_section_on_or_off() -> None:
    night, events = pledge_night()
    mark = night.marks["opening_done"]
    on = render_observation(events, 3, "plan", memory_settings(), as_of_seq=mark)
    off = render_observation(events, 3, "plan", make_settings(), as_of_seq=mark)
    assert on == off and HEAD not in on


def test_every_other_phase_gets_the_section_once() -> None:
    night, events = pledge_night()
    seen = set()
    for mark in night.marks:
        for phase in PHASE_TABLE:
            if phase == "plan":
                continue
            try:
                text = observe(night, events, 3, phase, mark)
            except RenderError:
                continue
            assert text.count(HEAD) == 1, (mark, phase)
            seen.add(phase)
    assert seen == set(PHASE_TABLE) - {"plan"}


def test_the_section_changes_nothing_but_itself() -> None:
    night, events = pledge_night()
    mark = night.marks["lot6_talk_done"]
    for seat in LLM_SEATS:
        on = render_prompt(events, seat, "sealed", memory_settings(), as_of_seq=mark)
        off = render_prompt(events, seat, "sealed", make_settings(), as_of_seq=mark)
        assert on.system == off.system
        blocks = on.observation.split("\n\n")
        assert [b for b in blocks if not b.startswith(HEAD)] == off.observation.split("\n\n")


def test_the_codename_of_a_pledge_maker_is_cleaned_like_every_other() -> None:
    night, _ = pledge_night()
    for raw in night.raw:
        if raw["type"] == "night_start":
            for entry in raw["data"]["seats"]:
                if entry["seat"] == 2:
                    entry["codename"] = "阿​铁"
    cleaned = [parse_event(copy.deepcopy(raw)) for raw in night.raw]
    lines = section_of(observe(night, cleaned, 3, "sealed", "lot6_talk_done"))
    assert lines[4] == "2 号「阿铁」立誓：对第 6 件不出价"
