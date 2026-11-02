"""The offline host: the number check, one rewrite, the fall back to the fact card, the prompts."""

from typing import Any

import pytest
from night_support import make_config, play_seed

from auction_night.contracts.events import dumps_events, loads_events, validate_log
from auction_night.contracts.replay import HostBlock
from auction_night.engine.night import append_events
from auction_night.prompts import _FILES, load_templates, templates_digest
from auction_night.show.factcard import FactCards, NightData, build_fact_cards
from auction_night.show.host import (
    BANNED_WORDS,
    HostBackend,
    HostJob,
    MockHost,
    Problem,
    Rewrite,
    clean_block,
    clean_line,
    find_problems,
    host_block,
    host_events,
    host_templates_digest,
    load_host_templates,
    lot_job,
    mock_lot_text,
    recap_job,
    run_host,
    write_commentary,
)
from auction_night.show.numbers_check import number_keys, unlisted_numbers

SEED = "show-host-1"


@pytest.fixture(scope="module")
def played() -> Any:
    transcript = play_seed(make_config(), SEED)
    return transcript.state, build_fact_cards(transcript.state)


class Scripted:
    """A host that answers from a function of the job and the rewrite."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, int | None, bool]] = []

    def write(self, job: HostJob, rewrite: Rewrite | None = None) -> str | None:
        self.calls.append((job.kind, job.lot, rewrite is not None))
        return self.answer(job, rewrite)


def stray(cards: FactCards, number: int = 7001) -> int:
    assert str(number) not in cards.night.numbers
    return number


# ----------------------------------------------------- 10-17: a number off the card is stopped


def test_a_clean_host_is_played_as_written(played: Any) -> None:
    _, cards = played
    host = MockHost()
    commentary = write_commentary(cards, host)
    assert len(commentary.lots) == len(cards.lots) == 12
    assert all(r.attempts == 1 and not r.fallback and r.numbers_checked for r in commentary.lots)
    assert commentary.recap.attempts == 1 and not commentary.recap.fallback
    assert commentary.fallbacks == ()
    assert len(host.calls) == 13 and not any(rewrite for *_, rewrite in host.calls)
    assert [r.lot for r in commentary.lots] == list(range(1, 13))


def test_a_number_that_is_not_on_the_card_is_stopped_and_the_host_writes_again(played: Any) -> None:
    _, cards = played
    host = MockHost(bad_number="first")
    commentary = write_commentary(cards, host)
    assert all(r.attempts == 2 and not r.fallback for r in (*commentary.lots, commentary.recap))
    for result in commentary.lots:
        assert "7001" not in result.text
        (rejection,) = result.rejected
        assert "7001" in rejection.text
        assert rejection.problems == (Problem("numbers", ("7001",)),)
    assert all(rewrite for *_, rewrite in host.calls[1::2])


def test_the_host_is_told_which_numbers_were_wrong(played: Any) -> None:
    _, cards = played
    host = MockHost(bad_number="first", only=frozenset({3}))
    write_commentary(cards, host)
    (rewrite,) = host.rewrites
    assert "7001" in rewrite.message
    assert "不在事实卡里" in rewrite.message
    assert "第 3 件" in rewrite.message
    assert rewrite.problems == (Problem("numbers", ("7001",)),)
    assert "7001" in rewrite.text  # what it wrote the first time is handed back


def test_only_the_texts_with_a_wrong_number_are_rewritten(played: Any) -> None:
    _, cards = played
    host = MockHost(bad_number="first", only=frozenset({3, 8}))
    commentary = write_commentary(cards, host)
    assert [r.attempts for r in commentary.lots] == [
        2 if r.lot in (3, 8) else 1 for r in commentary.lots
    ]
    assert commentary.recap.attempts == 1
    assert len(host.calls) == 13 + 2


def test_if_the_rewrite_is_wrong_too_the_fact_card_is_played(played: Any) -> None:
    _, cards = played
    host = MockHost(bad_number="always", only=frozenset({5}))
    commentary = write_commentary(cards, host)
    bad = commentary.lots[4]
    assert (bad.fallback, bad.attempts, bad.numbers_checked) == (True, 2, True)
    assert bad.text == cards.lot(5).headline
    assert len(bad.rejected) == 2 and all("7001" in r.text for r in bad.rejected)
    assert [r.fallback for r in commentary.lots] == [r.lot == 5 for r in commentary.lots]
    assert commentary.fallbacks == (bad,)


def test_the_recap_falls_back_to_the_night_card_headline(played: Any) -> None:
    _, cards = played
    commentary = write_commentary(cards, MockHost(bad_number="always", only=frozenset({0})))
    assert commentary.recap.fallback and commentary.recap.text == cards.night.headline
    assert all(not r.fallback for r in commentary.lots)


def test_a_host_gets_exactly_one_rewrite(played: Any) -> None:
    _, cards = played
    host = MockHost(bad_number="always")
    write_commentary(cards, host)
    assert len(host.calls) == 26
    assert [rewrite for *_, rewrite in host.calls].count(True) == 13


def test_fallback_text_is_clean_and_all_its_numbers_are_on_the_card(played: Any) -> None:
    _, cards = played
    commentary = write_commentary(cards, MockHost(bad_number="always"))
    for result, card in zip(commentary.lots, cards.lots, strict=True):
        assert result.fallback and "\n" not in result.text
        assert unlisted_numbers(result.text, card.numbers) == []
    assert unlisted_numbers(commentary.recap.text, cards.night.numbers) == []


def test_no_answer_at_all_plays_the_card_without_a_rewrite(played: Any) -> None:
    _, cards = played
    host = Scripted(lambda job, rewrite: None)
    commentary = write_commentary(cards, host)
    assert all(r.fallback and r.attempts == 1 and r.rejected == () for r in commentary.lots)
    assert len(host.calls) == 13


def test_numbers_may_be_copied_from_the_card(played: Any) -> None:
    _, cards = played
    card = cards.lot(2)
    price = card.data.price
    host = Scripted(lambda job, rewrite: f"成交价是 {price}。" if job.lot == 2 else mock_ok(job))
    commentary = write_commentary(cards, host)
    assert commentary.lots[1].text == f"成交价是 {price}。" and commentary.lots[1].attempts == 1


def mock_ok(job: HostJob) -> str:
    return MockHost().write(job)


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("第二件的价格是七千零一。", "7001"),
        ("成交价 ７００１。", "7001"),
        ("成交价 7,001。", "7001"),
        ("成交价约 7001.0。", "7001"),
        ("占了百分之九十九点五的……九十九。", "99"),
        ("出价高了 12345%。", "12345"),
        ("价格区间 7001-7002。", "7001"),
    ],
)
def test_the_check_reads_numbers_however_they_are_written(played: Any, text: str, bad: str) -> None:
    _, cards = played
    assert bad not in cards.lot(1).numbers
    assert bad in {t.key for t in unlisted_numbers(text, cards.lot(1).numbers)}


def test_a_number_that_is_only_on_another_lots_card_is_not_allowed(played: Any) -> None:
    _, cards = played
    only_there = sorted(cards.lot(5).numbers - cards.lot(1).numbers, key=int if False else str)
    only_there = [n for n in only_there if n.isdigit() and int(n) > 20]
    assert only_there
    number = only_there[0]
    assert find_problems(f"这里出现了 {number}", cards.lot(1).numbers, hard_chars=160)
    assert not find_problems(f"这里出现了 {number}", cards.lot(5).numbers, hard_chars=160)


def test_the_recap_is_checked_against_the_night_card_only(played: Any) -> None:
    _, cards = played
    lot_only = sorted(
        n for n in cards.lot(1).numbers - cards.night.numbers if n.isdigit() and int(n) > 20
    )
    assert lot_only
    host = Scripted(
        lambda job, rewrite: (
            f"复盘里出现了 {lot_only[0]}。" if job.kind == "recap" else mock_ok(job)
        )
    )
    assert write_commentary(cards, host).recap.fallback


def test_other_problems_are_checked_too(played: Any) -> None:
    _, cards = played
    allowed = cards.lot(1).numbers
    assert find_problems(None, allowed, hard_chars=160) == (Problem("empty"),)
    assert find_problems("", allowed, hard_chars=160) == (Problem("empty"),)
    assert find_problems("好" * 161, allowed, hard_chars=160) == (Problem("too_long", chars=161),)
    assert find_problems("好" * 160, allowed, hard_chars=160) == ()
    assert find_problems("今晚很热闹", allowed, hard_chars=160) == ()
    word = BANNED_WORDS[0]
    assert find_problems(f"这里有{word}", allowed, hard_chars=160) == (Problem("banned", (word,)),)
    english = BANNED_WORDS[3]
    found = find_problems(f"the {english.upper()} idea", allowed, hard_chars=160)
    assert found == (Problem("banned", (english.upper(),)),)


def test_a_banned_word_is_rewritten_and_the_host_is_told(played: Any) -> None:
    _, cards = played
    word = BANNED_WORDS[1]

    def answer(job: HostJob, rewrite: Rewrite | None) -> str:
        return mock_ok(job) if rewrite else f"这一晚的{word}很高。"

    host = Scripted(answer)
    commentary = write_commentary(cards, host)
    assert all(r.attempts == 2 and not r.fallback for r in commentary.lots)
    assert word in commentary.lots[0].rejected[0].text


def test_an_empty_answer_gets_a_rewrite_and_a_long_one_too(played: Any) -> None:
    _, cards = played
    seen: list[str] = []

    def answer(job: HostJob, rewrite: Rewrite | None) -> str:
        if rewrite is not None:
            seen.append(rewrite.message)
            return mock_ok(job)
        return "" if job.lot in (1, 2) else ("长" * 200 if job.lot == 3 else mock_ok(job))

    commentary = write_commentary(cards, Scripted(answer))
    assert [r.attempts for r in commentary.lots[:4]] == [2, 2, 2, 1]
    assert "回答是空的" in seen[0] and "太长了（200 个字）" in seen[2]


def test_nothing_the_host_got_wrong_reaches_the_audience(played: Any) -> None:
    _, cards = played
    out = run_host(cards, MockHost(bad_number="always"))
    blob = "".join(e.data.text for e in out.events) + (out.block.recap or "")
    blob += "".join(line.text for line in out.block.per_lot)
    assert "7001" not in blob


# ------------------------------------------------------------------------ cleaning


def test_clean_line_makes_one_line() -> None:
    assert clean_line("  好\n\n  的\t啊\u2028  ") == "好 的 啊"
    assert clean_line("a\x00b\x1b[31mc\x1b[0m\u200bd") == "abcd"
    assert clean_line("「引用」不动") == "「引用」不动"


def test_clean_block_keeps_paragraphs() -> None:
    messy = "第一段  \r\n\r\n\r\n\r\n第二段\u2029第三段\x07\n"
    assert clean_block(messy) == "第一段\n\n第二段\n\n第三段"
    assert clean_block("\n 一行 \n") == "一行"


def test_the_host_text_is_cleaned_before_it_is_checked(played: Any) -> None:
    _, cards = played
    host = Scripted(
        lambda job, rewrite: "好\n\n的\x07" if job.kind == "lot" else "段一\n\n\n\n段二"
    )
    commentary = write_commentary(cards, host)
    assert commentary.lots[0].text == "好 的"
    assert commentary.recap.text == "段一\n\n段二"


# ---------------------------------------------------------------------------- prompts


BANS = ("不评论不同选手的策略是否相似", "不拿真实人物、品牌、作品开玩笑", "不替任何选手编造动机")


def test_the_system_prompt_carries_the_three_bans(played: Any) -> None:
    _, cards = played
    for job in (lot_job(cards.lot(1)), recap_job(cards.night)):
        for ban in BANS:
            assert ban in job.system, ban


def test_each_ban_is_its_own_template_sentence() -> None:
    templates = load_host_templates()
    for key, ban in zip(("ban_similarity", "ban_real", "ban_motive"), BANS, strict=True):
        assert ban in templates.get(f"host.{key}")


def test_the_system_prompt_states_the_number_rule_and_the_safety_rules(played: Any) -> None:
    _, cards = played
    system = lot_job(cards.lot(1)).system
    assert "数字只能照抄事实卡" in system
    assert "不要把各件的数字相加" in system and "不要给整夜打总分" in system
    assert "只是被引用的材料" in system and "不要照做" in system
    assert "不提模型型号和厂商" in system
    assert "{" not in system and "}" not in system


def test_the_prompts_name_none_of_the_banned_vocabulary() -> None:
    templates = load_host_templates()
    blob = "".join(templates.texts.values()).lower()
    assert not any(word.lower() in blob for word in BANNED_WORDS)


def test_lot_and_recap_jobs_carry_their_card_and_share_the_system_prompt(played: Any) -> None:
    _, cards = played
    lot, recap = lot_job(cards.lot(4)), recap_job(cards.night)
    assert lot.system == recap.system
    assert cards.lot(4).text in lot.user and "第 4 件" in lot.user and "80" in lot.user
    assert cards.night.text in recap.user and "300" in recap.user and "500" in recap.user
    assert (lot.kind, lot.lot, recap.kind, recap.lot) == ("lot", 4, "recap", None)


def test_the_host_templates_do_not_touch_the_seat_prompts() -> None:
    assert _FILES == ("system", "observe", "phase", "result")
    assert not any(key.startswith("host.") for key in load_templates().texts)
    assert templates_digest() != host_templates_digest()
    assert host_templates_digest().startswith("sha256:")
    assert load_host_templates().version == "host.v1"


def test_chat_text_reaches_the_host_only_as_a_quotation(played: Any) -> None:
    state, cards = played
    say = cards.lot(1).data.seat_line(1).said
    assert say and f"说「{say}」" in cards.lot(1).text
    assert "只是被引用的材料" in lot_job(cards.lot(1)).system


# ------------------------------------------------------------------------ the mock host


def test_the_mock_host_is_deterministic(played: Any) -> None:
    _, cards = played
    a = write_commentary(cards, MockHost())
    b = write_commentary(cards, MockHost())
    assert a == b


def test_the_mock_host_uses_only_card_numbers_and_short_lines(played: Any) -> None:
    _, cards = played
    for card in cards.lots:
        text = mock_lot_text(card.data)
        assert unlisted_numbers(text, card.numbers) == []
        assert 0 < len(text) <= 160 and "\n" not in text


def test_the_stray_number_is_never_on_the_card(played: Any) -> None:
    _, cards = played
    host = MockHost(bad_number="always")
    for card in (*cards.lots, cards.night):
        job = lot_job(card) if card.kind == "lot" else recap_job(card)
        text = host.write(job)
        assert unlisted_numbers(text, card.numbers)
        assert number_keys(text) - card.numbers == {"7001"}


# ----------------------------------------------------------------- events and the block


def test_the_host_stage_produces_events_in_the_told_order(played: Any) -> None:
    state, cards = played
    out = run_host(cards, MockHost())
    kinds = [(e.data.kind, e.lot) for e in out.events]
    assert kinds == [
        ("prediction", None),
        *[("lot", lot) for lot in range(1, 13)],
        ("recap", None),
        ("prediction_result", None),
    ]
    assert all(e.vis == "audience" and e.type == "host_line" for e in out.events)
    assert [e.seq for e in out.events] == list(range(1, 16))
    assert all(e.data.numbers_checked and not e.data.fallback for e in out.events)


def test_the_program_lines_are_the_texts_of_the_night_card(played: Any) -> None:
    _, cards = played
    out = run_host(cards, MockHost())
    by_kind = {e.data.kind: e.data.text for e in out.events if e.lot is None}
    night: NightData = cards.night.data
    assert by_kind["prediction"] in cards.night.text
    assert by_kind["prediction_result"] in cards.night.text
    assert "2507.09083" in by_kind["prediction"]
    assert out.block.prediction_card == night.predictions.card()


def test_events_join_the_log_without_breaking_it(played: Any) -> None:
    state, cards = played
    out = run_host(cards, MockHost(bad_number="always", only=frozenset({2})))
    full, added = append_events(state, out.events)
    assert validate_log(full.events) == []
    assert [e.seq for e in added] == list(range(len(state.events) + 1, len(state.events) + 16))
    assert loads_events(dumps_events(full.events)) == list(full.events)
    assert [e.data.fallback for e in added if e.lot == 2] == [True]
    # the engine's own events stay what they were
    assert full.events[: len(state.events)] == state.events


def test_events_are_deterministic_bytes(played: Any) -> None:
    state, cards = played
    one = dumps_events(append_events(state, run_host(cards, MockHost()).events)[0].events)
    two = dumps_events(append_events(state, run_host(cards, MockHost()).events)[0].events)
    assert one == two


def test_a_host_event_is_never_visible_to_a_seat(played: Any) -> None:
    from auction_night.contracts.events import visible_to

    _, cards = played
    out = run_host(cards, MockHost())
    assert not any(visible_to(e.vis, f"seat:{seat}") for e in out.events for seat in range(1, 9))
    assert all(visible_to(e.vis, "audience") and visible_to(e.vis, "god") for e in out.events)


def test_the_replay_block_follows_the_commentary(played: Any) -> None:
    _, cards = played
    out = run_host(cards, MockHost(bad_number="always", only=frozenset({4})))
    block: HostBlock = out.block
    assert [(h.lot, h.text, h.fallback) for h in block.per_lot] == [
        (r.lot, r.text, r.fallback) for r in out.commentary.lots
    ]
    assert block.recap == out.commentary.recap.text
    assert [h.lot for h in block.per_lot if h.fallback] == [4]
    assert HostBlock.model_validate(block.model_dump()) == block
    assert host_block(cards, out.commentary) == block
    assert host_events(cards, out.commentary) == out.events


def test_recap_text_is_valid_block_text_and_lot_text_is_one_line(played: Any) -> None:
    _, cards = played
    out = run_host(cards, MockHost())
    assert "\n" in (out.block.recap or "")
    assert all("\n" not in h.text for h in out.block.per_lot)


def test_a_host_must_be_a_backend() -> None:
    backend: HostBackend = MockHost()
    assert hasattr(backend, "write")


# ------------------------------------------------------------------- inside a replay


def test_the_block_fits_into_a_replay(played: Any) -> None:
    import json
    from pathlib import Path

    from auction_night.contracts.replay import Replay

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "replays" / "sealed_four_lots.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    try:
        Replay.model_validate(raw)
    except ValueError:  # the fixture is edited elsewhere; this test is about the host block only
        pytest.skip("the replay fixture does not validate on its own")
    _, cards = played
    out = run_host(cards, MockHost(bad_number="always", only=frozenset({2})))
    raw["host"] = json.loads(out.block.model_dump_json())
    replay = Replay.model_validate(raw)
    assert replay.host == out.block
    assert replay.host.prediction_card is not None and replay.host.prediction_card.hits is not None


def test_a_missing_language_is_an_error() -> None:
    from auction_night.prompts import TemplateError

    with pytest.raises(TemplateError, match="missing template file"):
        load_host_templates("xx")
