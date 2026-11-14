"""Second-batch owner checklist, without name suggestions or identity mutations."""

import json
from collections import defaultdict
from datetime import date

from zucai.alias_proposals import cell, write_new_output
from zucai.aliases_snapshot import fdorg_formal_aliases, formal_aliases, snapshot
from zucai.cup_history import (
    CUPS,
    PROVISIONAL,
    coverage_lines,
    history_metrics,
    jc_cup_teams,
    team_games,
)
from zucai.sources.fd_ingest import _canonical
from zucai.sources.fd_verify import _utc
from zucai.sources.teams import normalize_team


def _season(instant):
    return instant.year - (instant.month < 7)


def _season_range(games):
    years = {_season(_utc(game["kickoff_utc"])) for game in games}
    if not years:
        return "无欧战记录"
    labels = [f"{year}/{str(year + 1)[-2:]}" for year in (min(years), max(years))]
    return "–".join(dict.fromkeys(labels))


def _aliases_by_team(teams, aliases):
    owners = defaultdict(set)
    for team in teams:
        labels = [team["name"], *json.loads(team["aliases"] or "[]")]
        for label in labels:
            owners[normalize_team(label).casefold()].add(team["id"])
    bound = defaultdict(list)
    for league, entries in aliases.items():
        for key, entry in entries.items():
            canonical = _canonical(aliases, league, key)
            ids = owners.get(canonical.casefold(), set()) if canonical else set()
            if len(ids) == 1:
                bound[next(iter(ids))].append((league, key, entry.get("name", "未知")))
    return bound


def _source_names(rows, field):
    names = defaultdict(set)
    for row in rows:
        if row.get("team_id") and row.get(field):
            names[row["team_id"]].add(row[field])
    return {key: " / ".join(sorted(values)) for key, values in names.items()}


def write_cups_history(archive, out):
    data = snapshot(archive)
    teams, matches = data["teams"], data["matches"]
    games = team_games(matches)
    indexed = {team["id"]: team for team in teams}
    aliases = {"apif": formal_aliases(), "fdorg": fdorg_formal_aliases()}
    bound = {provider: _aliases_by_team(teams, rows) for provider, rows in aliases.items()}
    known = (
        {
            match[side + "_team_id"]
            for match in matches
            if match["source"] == "jc"
            for side in ("home", "away")
        }
        | set(bound["apif"])
        | set(bound["fdorg"])
    )
    known = {key for key in known if not PROVISIONAL.fullmatch(indexed[key]["name"])}
    latest = _season(date.fromisoformat(data["manifest"]["through_beijing"]))
    names = {
        "apif": _source_names(data["audits"]["cup_aliases"], "name"),
        "fdorg": _source_names(data["audits"]["fdorg_unmatched_teams"], "english_name"),
    }
    candidates = {"apif": [], "fdorg": []}
    for team in teams:
        found = PROVISIONAL.fullmatch(team["name"])
        if not found:
            continue
        provider, key = found.groups()
        cups = [
            g
            for g in games[team["id"]]
            if g["league"] in ({"欧罗巴", "欧协联"} if provider == "apif" else CUPS)
        ]
        if provider == "apif" and len(cups) < 10:
            continue
        if provider == "fdorg" and any(
            _canonical(aliases[provider], league, key) for league in aliases[provider]
        ):
            continue  # Already authorized fdorg IDs go straight to guarded reconcile.
        opponents = set()
        for game in cups:
            if _season(_utc(game["kickoff_utc"])) not in {latest - 1, latest}:
                continue
            other = (
                game["away_team_id"] if game["home_team_id"] == team["id"] else game["home_team_id"]
            )
            if other in known:
                opponents.add(indexed[other]["name"])
        candidates[provider].append((key, cups, sorted(opponents)[:3]))
    lines = [
        "# 欧战历史身份审批清单（第二批）",
        "",
        f"数据归档清单 SHA256：`{data['manifest_sha256']}`；"
        f"归档北京日：{data['manifest']['through_beijing']}。",
        "每行一个来源 ID；建议中文规范名全部留空，只有所有者逐行批准才可写入正式别名。",
        "赛季范围按库内开球日期、7 月换季分组，不保证完整季；"
        f"已绑定对手示例只取归档对应的 {latest - 1}/{str(latest)[-2:]}、"
        f"{latest}/{str(latest + 1)[-2:]} 两季，最多 3 个，仅供核对。",
        "名称取来源审计原文；原名缺失则明确标缺失，不从中文名或相似名称推断。",
        "",
        "## 度量",
        "",
        *coverage_lines(history_metrics(teams, matches)),
        "合并身份：0；重定向比赛：0；冲突跳过：0（本命令只读，不执行合并）。",
        "",
        "## apif（欧罗巴 / 欧协联历史至少 10 场）",
        "",
        "| apif ID | apif 名称 | 出场赛季范围 | 欧罗巴出场 | 欧协联出场 | "
        "已绑定对手示例 | 建议中文规范名（所有者填写） |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for key, cups, opponents in sorted(
        candidates["apif"], key=lambda row: (-len(row[1]), int(row[0]))
    ):
        values = [
            key,
            names["apif"].get(key, "缺失（apif_cup_aliases.csv 未记录）"),
            _season_range(cups),
            sum(g["league"] == "欧罗巴" for g in cups),
            sum(g["league"] == "欧协联" for g in cups),
            "；".join(opponents) or "无已绑定对手记录",
        ]
        lines.append("| " + " | ".join(map(cell, values)) + " |  |")
    lines += [
        "",
        "## fdorg（尚无生效正式别名的临时身份）",
        "",
        "| fdorg ID | fdorg 名称 | 出场赛季范围 | 欧战出场 | "
        "已绑定对手示例 | 建议中文规范名（所有者填写） |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for key, cups, opponents in sorted(
        candidates["fdorg"], key=lambda row: (-len(row[1]), int(row[0]))
    ):
        values = [
            key,
            names["fdorg"].get(key, "缺失（fdorg_unmatched_teams.csv 未记录）"),
            _season_range(cups),
            len(cups),
            "；".join(opponents) or "无已绑定对手记录",
        ]
        lines.append("| " + " | ".join(map(cell, values)) + " |  |")
    lines += [
        "",
        "## 参考：当前 jc 在售欧战球队中没有 apif 别名的球队",
        "",
        "沿用上方 jc 样本口径，仅供所有者对照，不是绑定证据。proposed 不算生效绑定。",
        "| 中文名 | 库内比赛数 | 已有 fdorg 别名 |",
        "| --- | ---: | --- |",
    ]
    for key in sorted(jc_cup_teams(matches), key=lambda key: (indexed[key]["name"], key)):
        team = indexed[key]
        if key in bound["apif"] or any(
            re_binding.startswith("apif:") for re_binding in json.loads(team["aliases"] or "[]")
        ):
            continue
        values = [
            team["name"],
            len(games[key]),
            "；".join(" / ".join(entry) for entry in sorted(bound["fdorg"].get(key, []))) or "无",
        ]
        lines.append("| " + " | ".join(map(cell, values)) + " |")
    lines += [
        "",
        "批准需附 approved_by、UTC 的 approved_utc 与本仓库 approval_pr；"
        "本命令未修改别名、数据库或归档。",
        "",
    ]
    write_new_output(out, archive, "\n".join(lines))
    return sum(map(len, candidates.values()))
