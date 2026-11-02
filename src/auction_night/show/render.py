"""The Chinese text of a lot's fact card, and its one-line headline.

The host reads the text. It lists what happened in the lot, seat by seat, in a fixed order and
with fixed wording, so the same lot always renders to the same bytes. Every number in the text
comes from the card data: the renderer does no arithmetic of its own beyond reading a stored
integer back as a decimal (thousandths) or taking the absolute value of a stored difference.

``lot_headline`` is the shortest honest account of the lot: who got it and for how much, plus the
first highlight candidate. It is what the show plays when the host cannot be trusted with a lot.

A lot in which the cost gate skipped some seats has one more line, right after the line that
names the table, and the skipped seats are written as not having acted.
"""

from collections.abc import Sequence
from typing import Final

from auction_night.contracts.events import AbstainReason
from auction_night.contracts.replay import PledgeRecord
from auction_night.show.cards import Candidate, LotData, MissionLine, SeatLine
from auction_night.show.pledges import bid_word, pledge_text

__all__ = [
    "CATEGORY_ZH",
    "OUTCOME_ZH",
    "lot_headline",
    "milli_text",
    "render_lot",
    "seats_text",
    "signed",
]

ABSTAIN_ZH: Final[dict[AbstainReason, str]] = {
    "missing": "没有回答",
    "parse_failed": "回答无法解析",
    "timeout": "超时",
    "refusal": "拒答",
    "budget_aborted": "费用上限",
    "not_asked": "没有被询问",
}
OUTCOME_ZH: Final = {"kept": "遵守", "violated": "违约", "void": "无法检验"}
CATEGORY_ZH: Final = {
    "winners_curse": "赢家诅咒",
    "shill_stuck": "托儿砸手里",
    "backstab": "背刺",
    "claim_vs_hand": "说法与底牌不符",
    "plan_contradiction": "计划打脸",
    "all_pay_tilt": "全员付费上头",
    "bargain": "捡漏",
    "mindread_miss": "读心大失败",
}


def milli_text(thousandths: int) -> str:
    """A number stored in thousandths, as a decimal with three places (``874`` is ``0.874``)."""
    sign = "-" if thousandths < 0 else ""
    whole, frac = divmod(abs(thousandths), 1000)
    return f"{sign}{whole}.{frac:03d}"


def signed(value: int) -> str:
    """``+12``, ``-55`` or ``0``."""
    return f"+{value}" if value > 0 else str(value)


def seats_text(seats: Sequence[int]) -> str:
    """Seat numbers as ``1、2、3 号``."""
    return "、".join(str(seat) for seat in seats) + " 号"


def _who(line: SeatLine) -> str:
    return f"{line.seat} 号「{line.codename}」"


def _bot_tag(line: SeatLine) -> str:
    return "（机器人）" if line.role == "bot" else ""


# ------------------------------------------------------------------------------ one seat


def _bid_part(data: LotData, line: SeatLine) -> str:
    if not line.asked:
        return "预算用尽，没有出价资格"
    if line.muted:
        return "费用上限，没有行动"
    if line.abstained:
        reasons = [a.reason for a in line.actions if a.reason is not None]
        return f"弃权（{ABSTAIN_ZH[reasons[-1]] if reasons else ABSTAIN_ZH['missing']}）"
    word = bid_word(data.format)
    counted = [a for a in line.actions if a.value is not None]
    if data.format == "english":
        left = next((d for d in data.dropouts if d.seat == line.seat), None)
        if left is not None and not left.participated:
            declared = counted[0].value if counted else None
            return "未参加" + (f"（报了 {declared}，低于 1 或保留价）" if declared else "")
    text = f"{word} {line.played}"
    clamped = [a.raw_value for a in line.actions if a.status == "clamped" and a.raw_value]
    if clamped:
        text += f"（原报 {max(clamped)}，按预算夹取）"
    if any(a.status == "kept_previous" for a in line.actions):
        text += "（改价失败，保留原价）"
    elif len(counted) > 1 and counted[0].value != line.played:
        text += f"（初报 {counted[0].value}，后来改价）"
    return text


def _seat_line(data: LotData, line: SeatLine) -> str:
    parts = [f"{_who(line)}{_bot_tag(line)}", f"预算余 {line.budget_before}"]
    if line.value is not None:
        parts.append(f"估值 {line.value}")
    if line.signal is not None:
        parts.append(f"鉴定价 {line.signal}")
    if line.plan_stars is not None:
        plan = f"计划 {line.plan_stars} 星"
        if line.plan_budget is not None:
            plan += f"（打算最多花 {line.plan_budget}）"
        parts.append(plan)
    if line.said:
        parts.append(f"说「{line.said}」")
    if line.pledge is not None:
        parts.append(f"承诺{pledge_text(line.pledge)}")
    parts.append(_bid_part(data, line))
    if line.paid:
        parts.append(f"付 {line.paid}（付后预算余 {line.budget_after}）")
    if line.won:
        parts.append("拍得")
    if line.ref_bid is not None:
        parts.append(f"教科书参考 {line.ref_bid}")
    if line.excess is not None:
        parts.append(f"超额收益 {signed(line.excess)}")
    if line.regret is not None:
        parts.append(f"后悔值 {line.regret}")
    if line.alpha_milli is not None:
        parts.append(f"α {milli_text(line.alpha_milli)}")
    return "- " + "｜".join(parts)


# ------------------------------------------------------------------------ the cost cap


def _muted_line(data: LotData) -> list[str]:
    """The line that tells the host which seats the cost gate skipped, or nothing."""
    if not data.muted or data.muted_from is None:
        return []
    return [
        f"费用上限：这一夜从第 {data.muted_from} 件起，模型座位因为费用上限不再行动。"
        f"本件被跳过的座位：{seats_text(data.muted)}，这不是它们自己的选择。"
    ]


# ---------------------------------------------------------------------------- the result


def _result_line(data: LotData) -> str:
    if data.unsold or data.winner is None:
        return "结果：流拍，谁都没付款。"
    who = _who(data.seat_line(data.winner))
    if data.format == "all_pay":
        return f"结果：{who}出价最高拍得，付 {data.price}；出过价的人都要付自己的出价。"
    return f"结果：{who}拍得，成交价 {data.price}。"


def _extra_result_lines(data: LotData) -> list[str]:
    lines = []
    if data.tied and data.winner is not None:
        lines.append(f"平局：{seats_text(data.tied)}同价，按抽签键决出 {data.winner} 号。")
    if data.prior is not None and data.true_value is not None:
        text = f"盲盒真值 {data.true_value}（先验 {data.prior.low} 到 {data.prior.high}，"
        text += f"鉴定噪声 ±{data.prior.noise}）。"
        if data.curse_gap is not None:
            if data.curse_gap > 0:
                text += f"成交价 {data.price}，多付 {data.curse_gap}。"
            elif data.curse_gap < 0:
                text += f"成交价 {data.price}，少付 {-data.curse_gap}。"
            else:
                text += f"成交价 {data.price}，正好等于真值。"
        if data.top_signal_error is not None:
            if data.top_signal_error >= 0:
                text += f"最高鉴定价比真值高 {data.top_signal_error}。"
            else:
                text += f"最高鉴定价比真值低 {-data.top_signal_error}。"
        lines.append(text)
    if data.to_highest_value is not None and data.top_value is not None:
        verdict = "给了" if data.to_highest_value else "没有给"
        lines.append(
            f"估值最高的是 {seats_text(data.top_value_seats)}（估值 {data.top_value}），"
            f"这件{verdict}估值最高的人。"
        )
    if data.newly_out:
        lines.append(f"预算用尽出局：{seats_text(data.newly_out)}。")
    return lines


def _clock_lines(data: LotData) -> list[str]:
    lines = []
    if data.format == "english":
        steps = [
            f"{d.seat} 号@{d.price}" if d.participated else f"{d.seat} 号未参加"
            for d in data.dropouts
        ]
        text = "退出顺序：" + ("，".join(steps) if steps else "无")
        if data.winner is not None and data.winner_exit is not None:
            text += f"；留到最后的 {data.winner} 号退出价 {data.winner_exit}，付 {data.price}"
        lines.append(text + "。")
    elif data.dutch is not None:
        ladder = "、".join(str(price) for price in data.dutch.ladder)
        if data.dutch.unsold:
            lines.append(f"价格档：{ladder}；没有人出手，流拍。")
        else:
            lines.append(
                f"价格档：{ladder}；降到第 {data.dutch.level} 档（价格 {data.dutch.price}）时 "
                f"{data.dutch.buyer} 号出手买下。"
            )
    return lines


# ---------------------------------------------------------------- pledges and missions


def _pledge_lines(data: LotData) -> list[str]:
    lines = []
    for record in data.pledges_made:
        who = _who(data.seat_line(record.seat))
        lines.append(f"誓约（本件喊话）：{who}承诺{pledge_text(record.pledge)}。")
    for record in data.pledges_due:
        lines.append(_due_line(data, record))
    return lines


def _due_line(data: LotData, record: PledgeRecord) -> str:
    who = _who(data.seat_line(record.seat))
    return (
        f"誓约（涉及本件）：{who}在第 {record.lot} 件承诺{pledge_text(record.pledge)}，"
        f"{OUTCOME_ZH[record.outcome]}：{record.detail}。"
    )


def _mission_line(data: LotData, mission: MissionLine) -> str:
    who = _who(data.seat_line(mission.seat))
    if mission.kind == "collector":
        base = f"收藏家 {who}（{mission.tag}，要 {mission.need} 件，已有 {mission.won_so_far} 件）"
        if mission.outcome == "done":
            return f"任务：{base}，本件到手，任务完成。"
        if mission.outcome == "advanced":
            return f"任务：{base}，本件到手，之后同标签还有 {mission.tag_lots_left} 件。"
        return f"任务：{base}，本件没拿到，之后同标签还有 {mission.tag_lots_left} 件。"
    base = f"托儿 {who}（目标第 {mission.target_lot} 件，门槛 {mission.threshold}）"
    if mission.outcome == "unsold":
        return f"任务：{base}，这件流拍，任务失败。"
    if mission.outcome == "stuck":
        return f"任务：{base}，成交价 {mission.price}，自己买下，任务失败。"
    if mission.outcome == "reached":
        return f"任务：{base}，成交价 {mission.price}，买家 {mission.buyer} 号，任务完成。"
    return f"任务：{base}，成交价 {mission.price}，没到门槛，任务失败。"


def _candidate_line(candidate: Candidate) -> str:
    return f"高光候选（{CATEGORY_ZH[candidate.category]}）：{candidate.reason}"


# ---------------------------------------------------------------------------- the card


def render_lot(data: LotData) -> str:
    """The fact card of one lot as text."""
    lines = [
        f"【第 {data.lot} 件】{data.emoji}{data.name}｜{data.title}｜标签 {data.tag}"
        f"｜档位 {data.tier}｜保留价 {data.reserve}",
        f"规则：{data.rule}",
        f"在场 {len(data.bidders)} 人：{seats_text(data.bidders)}"
        + (f"；预算用尽没有出价资格：{seats_text(data.sitting_out)}" if data.sitting_out else "")
        + "。",
        *_muted_line(data),
        _result_line(data),
        *_extra_result_lines(data),
        *_clock_lines(data),
        "各座：",
        *(_seat_line(data, line) for line in data.seats),
    ]
    reads = [line for line in data.seats if line.guess is not None]
    for line in reads:
        guess = line.guess
        assert guess is not None
        lines.append(
            f"读心：{line.seat} 号猜 {guess.target} 号预算余 {guess.guess}，"
            f"实际 {guess.truth}，差 {guess.abs_error}。"
        )
    lines += _pledge_lines(data)
    lines += [_mission_line(data, mission) for mission in data.missions]
    lines += [_candidate_line(candidate) for candidate in data.candidates]
    if data.expected_revenue_milli is not None:
        lines.append(
            f"教科书：在场 {len(data.bidders)} 人，本件期望成交额 "
            f"{milli_text(data.expected_revenue_milli)}。"
        )
    return "\n".join(lines)


def lot_headline(data: LotData) -> str:
    """One or two sentences with the result of the lot and its first highlight candidate."""
    head = f"第 {data.lot} 件 {data.name}（{data.title}）："
    if data.unsold or data.winner is None:
        body = "流拍，谁都没付款。"
    else:
        line = data.seat_line(data.winner)
        body = f"{line.seat} 号{line.codename}拍得，成交价 {data.price}。"
        if data.true_value is not None:
            body += f"真值 {data.true_value}。"
    text = head + body
    if data.muted:
        text += "费用上限已满，模型座位没有行动。"
    if data.candidates:
        text += data.candidates[0].reason
    return text
