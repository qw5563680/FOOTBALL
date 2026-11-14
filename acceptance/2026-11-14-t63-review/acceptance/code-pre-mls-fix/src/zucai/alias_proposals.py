"""Render references for an owner; never infer or approve a Chinese team name."""

import html
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zucai.aliases_snapshot import (
    formal_aliases,
    observed_games,
    proposed_entries,
    selected_leagues,
    snapshot,
)
from zucai.sources.fd_ingest import WINDOW
from zucai.sources.fd_verify import _utc


def cell(value):
    return html.escape(str(value)).replace("|", "&#124;").replace("\r", " ").replace("\n", " ")


def write_new_output(out, archive, text):
    out, archive = Path(out).resolve(), Path(archive).resolve()
    if out.is_relative_to(archive):
        raise ValueError("输出不得写入只读数据归档")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        stream.write(text)


def write_proposals(archive, codes, out):
    selected = selected_leagues(codes)
    data, aliases = snapshot(archive), formal_aliases()
    records = proposed_entries(data, aliases, selected)
    games = observed_games(data, aliases)
    official = [row for row in data["matches"] if row["source"] == "jc"]
    by_team = defaultdict(list)
    for game in games:
        for side in ("home", "away"):
            if game.get(side + "_provider_id"):
                by_team[game["league"], str(game[side + "_provider_id"])].append(game)
    lines = [
        f"# APIF 别名审批清单 · {datetime.now(ZoneInfo('Asia/Shanghai')).date()}",
        "",
        "这里只列未核验候选。jc 中文名来自同联赛、同三小时窗口，仅供参考，不是身份绑定证据。",
        "建议中文规范名留空；所有者须填写或明确确认后，才可在 PR 批准。"
        "被划掉或仍未确定的行不启用。",
        "国家未知就标未知；旧审计缺少赛季字段时，不按日期猜赛季，只列已见比赛数。出场数不保证覆盖完整赛季。",
        "欧战第一批只列 2026 季有 jc 同窗的球队；旧审计未记赛季时，"
        "按 2026-07-01 至 2027-07-01 窗口初筛，仍标赛季未知，不作为核验证据。",
        f"数据归档清单 SHA256：`{data['manifest_sha256']}`。",
        "",
    ]
    count = 0
    for code, config in selected.items():
        league = config["league"]
        lines += [
            f"## {cell(league)}（{code}）",
            "",
            "| apif ID | apif 名称 | 所在国 | 2025 / 2026 已见出场 | "
            "jc 候选中文名（未核验） | 建议中文规范名（所有者填写） |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for (name, key), entry in sorted(records.items()):
            if name != league:
                continue
            appearances = by_team[name, key]
            years = {
                year: {
                    g["provider_match_id"]
                    for g in appearances
                    if str(g.get("season", "")) == str(year)
                }
                for year in (2025, 2026)
            }
            unknown = {
                g["provider_match_id"]
                for g in appearances
                if not g.get("season")
                and g.get("kickoff_utc")
                and _utc(g["kickoff_utc"]).year in (2025, 2026, 2027)
            }
            recent = [
                g
                for g in appearances
                if str(g.get("season", "")) in {"2025", "2026"} or g["provider_match_id"] in unknown
            ]
            if code in {"3", "848"}:
                recent = [
                    g
                    for g in recent
                    if str(g.get("season", "")) == "2026"
                    or (
                        not g.get("season")
                        and datetime(2026, 7, 1, tzinfo=UTC)
                        <= _utc(g["kickoff_utc"])
                        < datetime(2027, 7, 1, tzinfo=UTC)
                    )
                ]
            references = sorted(
                {
                    f"{j['match_id']}: {j['home']} / {j['away']}"
                    for j in official
                    if j["league"] == league
                    and any(
                        abs(_utc(j["kickoff_utc"]) - _utc(g["kickoff_utc"])) <= WINDOW
                        for g in recent
                    )
                }
            )
            # Cups' first batch is limited to teams with a 2026-season JC window;
            # older audit rows disclose missing season metadata instead of guessing it.
            if code in {"3", "848"} and not references:
                continue
            counts = f"{len(years[2025])} / {len(years[2026])}"
            if unknown:
                counts += f"；另 {len(unknown)} 场缺少赛季字段"
            lines.append(
                "| "
                + " | ".join(
                    map(
                        cell,
                        (
                            key,
                            entry.get("name", "未知"),
                            entry.get("country") or config.get("country") or "未知（归档未记录）",
                            counts,
                            "；".join(references) or "无同窗 jc 记录",
                        ),
                    )
                )
                + " |  |"
            )
            count += 1
        lines.append("")
    lines += [
        "批准后的新记录须带 `status: approved`、`approved_by`、"
        "UTC 的 `approved_utc` 和完整 `approval_pr` 链接。",
        "本命令未修改候选、正式别名或数据库。",
        "",
    ]
    write_new_output(out, archive, "\n".join(lines))
    return count
