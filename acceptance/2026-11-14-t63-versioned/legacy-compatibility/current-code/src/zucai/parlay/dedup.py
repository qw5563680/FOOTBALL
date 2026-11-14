"""Read-only opportunity counts; never alter group decisions or cash accounting."""

from collections import Counter

from zucai.digest_display import _text
from zucai.scoreboard.score import instant

OUTCOMES = {"h": "主胜", "d": "平", "a": "客胜"}


def _team_names(leg):
    return dict(home=leg.get("home"), away=leg.get("away"))


def combination_key(group):
    frozen = group["frozen"]
    return (
        frozen["single"]["match_id"],
        frozen["single_selection"],
        frozen["double"]["match_id"],
        tuple(sorted(frozen["double_selections"])),
    )


def summarize(ledger):
    # The decision time defines first appearance; stable ledger order breaks ties.
    groups = sorted(
        (row for row in ledger if row["record_type"] == "group"),
        key=lambda row: instant(row["decision_utc"]),
    )
    tickets = {row["record_id"]: row for row in ledger if row["record_type"] == "ticket"}
    combinations = {}
    names = {}
    for group in groups:
        combinations.setdefault(combination_key(group), []).append(group)
        for side in ("single", "double"):
            leg = group["frozen"][side]
            names.setdefault(leg["match_id"], _team_names(leg))
    first = [rows[0] for rows in combinations.values()]
    settled = [row for row in first if row["settlement"] is not None]
    first_tickets = [tickets[key] for row in first for key in row["ticket_ids"]]
    n = len(settled)
    covered = sum(row["settlement"]["covered"] for row in settled)
    single_hits = sum(row["settlement"]["single_hit"] for row in settled)
    repeated = []
    for (single, option, double, options), rows in combinations.items():
        batches = list(dict.fromkeys(row["batch_id"] for row in rows))
        if len(batches) > 1:
            repeated.append(
                dict(
                    single=dict(
                        match_id=single,
                        selection=option,
                        **_team_names(rows[0]["frozen"]["single"]),
                    ),
                    double=dict(
                        match_id=double,
                        selections=list(options),
                        **_team_names(rows[0]["frozen"]["double"]),
                    ),
                    occurrences=len(rows),
                    repeat_count=len(rows) - 1,
                    batch_ids=batches,
                    first_record_id=rows[0]["record_id"],
                    record_ids=[row["record_id"] for row in rows],
                )
            )
    group_matches = Counter(
        group["frozen"][side]["match_id"] for group in groups for side in ("single", "double")
    )
    ticket_matches = Counter(leg["match_id"] for row in tickets.values() for leg in row["legs"])
    most = max(ticket_matches.values(), default=0)
    return dict(
        unique_combinations=len(combinations),
        repeated_group_appearances=len(groups) - len(combinations),
        first_occurrence=dict(
            groups=len(first),
            settled_groups=n,
            pending_groups=len(first) - n,
            covered_groups=covered,
            coverage_rate=covered / n if n else None,
            single_hits=single_hits,
            single_hit_rate=single_hits / n if n else None,
            tickets=len(first_tickets),
            settled_tickets=sum(row["settlement"] is not None for row in first_tickets),
            won_tickets=sum(
                bool(row["settlement"] and row["settlement"]["status"] == "won")
                for row in first_tickets
            ),
        ),
        repeated_combinations=repeated,
        most_repeated_matches=[
            dict(
                match_id=mid,
                group_appearances=group_matches[mid],
                ticket_appearances=count,
                **names.get(mid, {}),
            )
            for mid, count in sorted(ticket_matches.items())
            if count == most and group_matches[mid] > 1
        ],
    )


def _match_label(row):
    home = _text(row.get("home") or "主队名未记录")
    away = _text(row.get("away") or "客队名未记录")
    return f"{home} VS {away}（{_text(row['match_id'])}）"


def render(summary):
    dedup = summary.get("deduplication")
    if dedup is None:
        return []
    totals = summary["totals"]
    lines = [
        f"机会计数：{totals['groups']} 组 / {dedup['unique_combinations']} 种不同组合；"
        f"重复出现 {dedup['repeated_group_appearances']} 组。",
        "相同比赛可用于不同组合，去重后仍不代表相互独立的机会；"
        "下表仅并列全账命中计数，规则/证据分层及原本金、奖金、结算照旧。",
        "",
        "| 命中口径 | 组合数 | 已结 | 待结 | 覆盖数 | 覆盖率 | 原单选命中数 | 两注命中数 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, row in (("原按组", totals), ("按首次出现去重", dedup["first_occurrence"])):
        rate = "—" if row["coverage_rate"] is None else f"{row['coverage_rate']:.2%}"
        lines.append(
            f"| {label} | {row['groups']} | {row['settled_groups']} | {row['pending_groups']} | "
            f"{row['covered_groups']} | {rate} | {row['single_hits']} | {row['won_tickets']} |"
        )
    lines.extend(["", "跨批次重复组合："])
    for row in dedup["repeated_combinations"]:
        a, b = row["single"], row["double"]
        options = "+".join(OUTCOMES[option] for option in "hda" if option in b["selections"])
        lines.append(
            f"- 单选 {_match_label(a)}{OUTCOMES[a['selection']]} × "
            f"双选 {_match_label(b)}{options}："
            f"出现 {row['occurrences']} 次（重复 {row['repeat_count']} 次），"
            f"涉及 {len(row['batch_ids'])} 批。"
        )
    if not dedup["repeated_combinations"]:
        lines.append("无。")
    lines.append(
        "被重复押注最多的比赛（并列全部列出，按基础注出现次数）："
        + (
            "；".join(
                f"{_match_label(row)}{row['ticket_appearances']} 注 / {row['group_appearances']} 组"
                for row in dedup["most_repeated_matches"]
            )
            or "无重复比赛"
        )
        + "。"
    )
    return lines + [""]
