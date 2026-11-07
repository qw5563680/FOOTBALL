"""Offline, portable report. This script never calls a sports API or writes production."""

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BASE_URL = "https://github.com/qw5563680/FOOTBALL/blob/data/t49-core39-20261108/"
MAPPING = {
    4: ("shots", "S"),
    5: ("shots_on_target", "ST"),
    33: ("corners", "C"),
    35: ("offsides", "O"),
    36: ("fouls", "F"),
    38: ("yellow_cards", "Y"),
    39: ("red_cards", "R"),
}
SCOPE = "候选：仅归档联赛；全部正式赛事名单未核实"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def slug(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def select_candidates(matches, cutoff):
    groups = defaultdict(list)
    for row in matches:
        if instant(row["kickoff_utc"]) >= cutoff or row["status"] != "finished":
            continue
        # Explicit archive-only scope. No inference that there were no cups.
        if row["league"] != "英冠":
            raise ValueError("New competition in archive: classify senior official/friendly first")
        assert row["home_goals"] is not None and row["away_goals"] is not None
        groups[(instant(row["kickoff_utc"]), row["home_team_id"], row["away_team_id"])].append(row)
    selected = []
    for team in (58, 52):
        eligible = [(key, rows) for key, rows in groups.items() if team in key[1:]]
        eligible.sort(key=lambda item: item[0][0], reverse=True)
        for rank, (_key, rows) in enumerate(eligible[:10], 1):
            rows.sort(
                key=lambda row: (
                    {"jc": 0, "fdorg": 1, "fd": 2}.get(row["source"], 3),
                    row["match_id"],
                )
            )
            row = dict(rows[0])
            assert len({(r["home_goals"], r["away_goals"]) for r in rows}) == 1, rows
            row.update(
                research_team_id=team,
                rank=rank,
                sample_status=SCOPE,
                aliases=sorted(
                    {r["match_id"] for r in rows}
                    | {r["counterpart_id"] for r in rows if r["counterpart_id"]}
                ),
            )
            selected.append(row)
    return selected


def csv_index(root, manifest):
    by_sha = {}
    for item in manifest:
        path = root / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        data = {}
        for line, row in enumerate(csv.DictReader(path.open(encoding="utf-8-sig", newline="")), 2):
            if not row.get("Date"):
                continue
            fmt = "%d/%m/%Y %H:%M" if len(row["Date"].split("/")[-1]) == 4 else "%d/%m/%y %H:%M"
            dt = (
                datetime.strptime(row["Date"] + " " + row["Time"], fmt)
                .replace(tzinfo=ZoneInfo("Europe/London"))
                .astimezone(UTC)
            )
            fixture = f"fd:{row['Div']}:{dt:%Y%m%d}:{slug(row['HomeTeam'])}:{slug(row['AwayTeam'])}"
            assert fixture not in data
            data[fixture] = {"row": row, "csv_line": line, "kickoff_utc": dt.isoformat(), **item}
        by_sha[item["sha256"]] = data
    return by_sha


def measure(game, team, indicator, stats, indexed, teams, aliases):
    side = "home" if team == game["home_team_id"] else "away"
    base = dict(
        research_team_id=game["research_team_id"],
        rank=game["rank"],
        match_id=game["match_id"],
        match_aliases=" | ".join(game["aliases"]),
        team_id=team,
        team_name=teams[str(team)]["name"],
        side=side,
        indicator_id=int(indicator["编号"]),
        indicator_name=indicator["指标名称"],
        status="缺失",
        value=None,
        raw_candidate=None,
        period="H1+补时" if indicator["编号"] == "34" else "90分钟+补时",
        provider="",
        provider_fixture_id="",
        method="未取得",
        raw_field="",
        raw_sha256="",
        raw_location="",
        source_url="",
        csv_line=None,
        captured_utc="",
        checked_at_utc="",
        reason="该场现有归档无匹配字段；API-Football/Sportmonks 本次未实测",
        sample_status=SCOPE,
        archive_db_row_id=None,
    )
    number = base["indicator_id"]
    candidates = [
        s
        for s in stats
        if s["team_id"] == team
        and (s["match_id"] in game["aliases"] or s["provider_fixture_id"] in game["aliases"])
    ]
    if number not in MAPPING and number != 12:
        special = {
            10: "无原始绝佳机会记录；不能将创造绝佳机会直接等同",
            25: "无广义丢失球权记录；不能用被抢断代替",
            26: "无明确一对一成功定义及记录；不能用全部对抗成功或成功过人代替",
            34: "无上半场统计或完整逐次角球事件；本批未调用 half=true",
            37: "无获得/执行任意球字段；不使用 free_kicks_conceded，也不由对手犯规/越位推算",
        }
        base["reason"] = special.get(number, base["reason"])
        return base
    if not candidates:
        base["reason"] = "该比赛没有归档球队统计、球员明细或事件；保留该行，不以更早比赛替代"
        return base
    selected = [
        s
        for s in candidates
        if s["stat_key"] == (MAPPING[number][0] if number in MAPPING else "shots")
    ]
    if not selected:
        return base
    # Refuse conflicts rather than selecting a convenient source or averaging.
    signatures = {(s["raw_sha256"], s["value"], s["period_basis"], s["side"]) for s in selected}
    if len(signatures) != 1:
        base.update(
            status="未核实",
            reason="多个原始记录冲突，未自动选值",
            raw_candidate=json.dumps(selected, ensure_ascii=False),
        )
        return base
    record = selected[0]
    assert record["provider"] == "fd" and record["side"] == side
    assert (
        record["eligible_90"]
        and record["period_basis"] == "90"
        and not record["extra_time_included"]
    )
    original = indexed[record["raw_sha256"]][record["provider_fixture_id"]]
    row = original["row"]
    assert instant(original["kickoff_utc"]) == instant(game["kickoff_utc"])
    assert aliases["英冠"][row["HomeTeam"]]["canonical"] == teams[str(game["home_team_id"])]["name"]
    assert aliases["英冠"][row["AwayTeam"]]["canonical"] == teams[str(game["away_team_id"])]["name"]
    assert (int(row["FTHG"]), int(row["FTAG"])) == (game["home_goals"], game["away_goals"])
    column = ("H" if side == "home" else "A") + (MAPPING[number][1] if number in MAPPING else "xG")
    raw = row.get(column)
    base.update(
        provider="football-data.co.uk",
        provider_fixture_id=record["provider_fixture_id"],
        raw_field=column,
        raw_sha256=record["raw_sha256"],
        raw_location=record["raw_location"],
        source_url=BASE_URL + original["path"],
        csv_line=original["csv_line"],
        captured_utc=record["captured_utc"],
        archive_db_row_id=record["id"],
        method="归档 CSV 原值；已核对主客队、时间、比分、SHA256 与数据库行",
    )
    if number == 12:
        if raw not in (None, ""):
            base.update(
                status="未核实",
                raw_candidate=raw,
                reason=(
                    "原 CSV 有 HxG/AxG，但底层模型、版本及统计定义未核实；只展示原值，不计可靠覆盖"
                ),
                method="读取已有原件中未入库的 xG 列；未新增接口请求",
            )
        return base
    assert json.loads(record["raw_value"]) == raw
    if raw in (None, ""):
        assert record["value"] is None
        base["reason"] = (
            "原 CSV 无该列（column_absent）" if column not in row else "原 CSV 字段为空"
        )
        return base
    assert float(raw) == record["value"] and float(raw) >= 0
    base.update(status="已核实", value=float(raw), reason="来源明确数值；保留来源口径")
    if number == 33:
        base["reason"] = "原字段为 Corners；获得/开出未进一步区分，不据此推算上半场"
    if number in (38, 39):
        base["reason"] = "保留 FD 原始球队牌数；第二黄、替补席细分未提供，不另行拆分或叠加"
    return base


def display(obs):
    if obs["status"] == "已核实":
        return f"{obs['value']:g}"
    if obs["status"] == "未核实" and obs["raw_candidate"] is not None:
        return f"未核实({obs['raw_candidate']})"
    return obs["status"]


def csv_write(path, rows, fields=None):
    with path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def sheet(wb, name, headers, rows):
    ws = wb.create_sheet(name)
    ws.append(headers)
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="16384A")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 44
    for i, col in enumerate(ws.columns, 1):
        length = max((len(str(c.value or "")) for c in col), default=12)
        ws.column_dimensions[get_column_letter(i)].width = min(58, max(15, length + 2))
    for row in ws.iter_rows(min_row=2):
        max_lines = 1
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            width = ws.column_dimensions[cell.column_letter].width
            display_width = sum(2 if ord(ch) > 255 else 1 for ch in str(cell.value or ""))
            max_lines = max(max_lines, int(display_width / width) + 1)
            if isinstance(cell.value, str) and cell.value.startswith("https://"):
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"
        ws.row_dimensions[row[0].row].height = min(120, 17 * max_lines + 4)
    return ws


def build(root):
    out = root / "outputs"
    out.mkdir(exist_ok=True)
    checks = root / "checks"
    checks.mkdir(exist_ok=True)
    inputs = root / "inputs"
    registry = list(csv.DictReader((inputs / "core-39.csv").open(encoding="utf-8-sig")))
    assert [int(r["编号"]) for r in registry] == list(range(1, 40))
    matches, teams, stats = (read(inputs / f"{n}.json") for n in ("matches", "teams", "stats"))
    manifest = read(inputs / "raw_manifest.json")
    provenance = read(inputs / "provenance.json")
    games = select_candidates(matches, instant(provenance["target"]["kickoff_utc"]))
    indexed = csv_index(root, manifest)
    aliases = read(inputs / "fd-team-aliases.json")
    reported_utc = datetime.now(UTC).isoformat()
    observations, pairs, team_rows, coverage = [], [], defaultdict(list), []
    for game in games:
        team = game["research_team_id"]
        opponent = game["away_team_id"] if game["home_team_id"] == team else game["home_team_id"]
        team_row = {
            "序号": game["rank"],
            "北京时间": instant(game["kickoff_utc"])
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .strftime("%Y-%m-%d %H:%M"),
            "赛事": game["league"],
            "研究球队": teams[str(team)]["name"],
            "本队主客": "主" if team == game["home_team_id"] else "客",
            "当场对手": teams[str(opponent)]["name"],
            "主队": teams[str(game["home_team_id"])]["name"],
            "客队": teams[str(game["away_team_id"])]["name"],
            "比分(主:客)": f"{game['home_goals']}:{game['away_goals']}",
            "比赛编号": game["match_id"],
            "名单核实状态": SCOPE,
        }
        for indicator in registry:
            own, other = [
                measure(game, t, indicator, stats, indexed, teams, aliases)
                for t in (team, opponent)
            ]
            long_row = len(observations) + 2
            for observation in (own, other):
                observation["checked_at_utc"] = reported_utc
            observations.extend([own, other])
            full = all(o["status"] == "已核实" for o in (own, other))
            one = sum(o["status"] == "已核实" for o in (own, other)) == 1
            label = f"{indicator['编号']} {indicator['指标名称']}"
            team_row[label] = display(own) + " / " + display(other)
            pairs.append(
                dict(
                    team_id=team,
                    rank=game["rank"],
                    indicator=int(indicator["编号"]),
                    full=full,
                    one_sided=one,
                    unverified=any(o["status"] == "未核实" for o in (own, other)),
                    detail_row=long_row,
                    display=team_row[label],
                )
            )
        team_rows[team].append(team_row)
    for indicator in registry:
        subset = [p for p in pairs if p["indicator"] == int(indicator["编号"])]
        reasons = sorted(
            {
                o["reason"]
                for o in observations
                if o["indicator_id"] == int(indicator["编号"]) and o["status"] != "已核实"
            }
        )
        coverage.append(
            {
                "编号": int(indicator["编号"]),
                "中文名": indicator["指标名称"],
                "定义": indicator["怎样记／怎么算"],
                "归档双方填齐": sum(p["full"] for p in subset),
                "API-Football新增": 0,
                "API实测状态": "未执行",
                "Sportmonks独有新增": 0,
                "Sportmonks实测状态": "未实测",
                "最终双方填齐": sum(p["full"] for p in subset),
                "仅一方可靠": sum(p["one_sided"] for p in subset),
                "仍未填齐(含未核实)": sum(not p["full"] for p in subset),
                "其中有原值未核实": sum(p["unverified"] for p in subset),
                "分母": len(subset),
                "原因": "；".join(reasons),
                "样本限制": SCOPE,
            }
        )
    assert len(games) == 20 and len(pairs) == 780 and len(observations) == 1560
    full = sum(p["full"] for p in pairs)
    summary = {
        "sample_complete_all_competitions": False,
        "team_view_rows": len(games),
        "unique_fixtures": len(
            {(g["kickoff_utc"], g["home_team_id"], g["away_team_id"]) for g in games}
        ),
        "paired_indicator_cells": len(pairs),
        "archive_complete_pairs": full,
        "one_sided_pairs": sum(p["one_sided"] for p in pairs),
        "not_complete_pairs": len(pairs) - full,
        "unverified_pairs": sum(p["unverified"] for p in pairs),
        "api_football_new_pairs": 0,
        "api_football_new_requests": 0,
        "sportmonks_new_pairs": 0,
        "procurement_verdict": "值得试用但暂不购买",
    }
    write(out / "summary.json", summary)
    write(out / "selected-candidates.json", games)
    write(out / "observations.json", observations)
    csv_write(out / "coverage-39.csv", coverage)
    csv_write(out / "observations.csv", observations)
    for team, rows in team_rows.items():
        csv_write(out / (teams[str(team)]["name"] + "-归档候选.csv"), rows)
    wb = Workbook()
    wb.remove(wb.active)
    notes = [
        [
            "结论",
            f"归档阶段已实做；当前候选20行中{full}/780双方指标格有可靠值。全部正式赛事名单与新接口尚未完成实测。",
        ],
        [
            "重要限制",
            "两张球队表是重新从归档去重筛出的联赛候选，不是已经核准的所有正式比赛近10场；杯赛补核后名单、分母覆盖必须重算。",
        ],
        [
            "目标身份",
            "jc:2042565 / fdorg:561932；英冠；德比郡主场 vs 博尔顿；"
            "北京时间2026-11-07 04:00。仓库内部身份已核实，API跨源身份待核。",
        ],
        [
            "每格顺序",
            "研究球队 / 当场对手；客场也不颠倒。点击指标格跳到逐项明细，两行分别为研究球队和对手。",
        ],
        [
            "状态",
            "已核实=与原件相符；缺失=无有效记录；未核实=有原值但定义/时段待核；不适用=定义上不适用。本批没有用零代替任何空值。",
        ],
        [
            "时间",
            "cutoff仅用于选择历史比赛；原始captured_utc照实保留。今天读取旧归档不等于今天采集，更不证明当时的预测使用过该数。",
        ],
        ["API-Football", provenance["api_football_status"]],
        ["Sportmonks", provenance["sportmonks_status"]],
        ["xG", "已有CSV HxG/AxG原值单列未核实，未计覆盖；未使用赔率列。"],
        ["采购判断", summary["procurement_verdict"]],
        [
            "下一步",
            "执行助手先完成Actions定向probe并核实全赛事名单；账户所有者只需在Sportmonks后台确认是否已有试用及赛事/统计权限，不在聊天提交密钥、不购买。",
        ],
        ["正式数据", provenance["official_data_commit"]],
        ["代码基线", provenance["code_commit"]],
    ]
    sheet(wb, "先读说明", ["项目", "内容"], notes)
    for team, rows in team_rows.items():
        ws = sheet(wb, teams[str(team)]["name"], list(rows[0]), [list(r.values()) for r in rows])
        ws.freeze_panes = "G2"
        for column, width in zip(
            "ABCDEFGHIJK", (6, 20, 9, 12, 8, 14, 14, 14, 10, 24, 24), strict=True
        ):
            ws.column_dimensions[column].width = width
        for i, _row in enumerate(rows, 2):
            for j, indicator in enumerate(registry, 12):
                pair = next(
                    p
                    for p in pairs
                    if p["team_id"] == team
                    and p["rank"] == i - 1
                    and p["indicator"] == int(indicator["编号"])
                )
                cell = ws.cell(i, j)
                cell.hyperlink = f"#'逐项明细'!A{pair['detail_row']}"
                cell.fill = PatternFill(
                    "solid",
                    fgColor="DDEEDB"
                    if pair["full"]
                    else "FFF0CF"
                    if pair["unverified"]
                    else "F0F2F4",
                )
                cell.comment = Comment(
                    indicator["怎样记／怎么算"]
                    + "\n"
                    + indicator["口径与常见错误"]
                    + "\n点击查看两队原值、SHA256、原件行号及原因。",
                    "T49",
                )
                ws.column_dimensions[get_column_letter(j)].width = 23
    sheet(wb, "39项覆盖", list(coverage[0]), [list(r.values()) for r in coverage])
    sheet(wb, "指标原定义", list(registry[0]), [list(r.values()) for r in registry])
    sheet(wb, "逐项明细", list(observations[0]), [list(r.values()) for r in observations])
    sheet(wb, "原始资料", list(manifest[0]), [list(r.values()) for r in manifest])
    example = next(g for g in games if g["research_team_id"] == 58 and g["rank"] == 2)
    example_rows = []
    for number in (34, 10, 25, 26, 33, 4, 5):
        obs = [
            o
            for o in observations
            if o["research_team_id"] == 58 and o["rank"] == 2 and o["indicator_id"] == number
        ]
        example_rows.append(
            [
                example["match_id"],
                obs[0]["indicator_name"],
                display(obs[0]) + " / " + display(obs[1]),
                obs[0]["reason"],
            ]
        )
    sheet(wb, "实际比赛示例", ["比赛编号", "指标", "德比郡 / 米尔沃尔", "依据"], example_rows)
    half_evidence = read(checks / "t30-half-evidence.json")
    for evidence in half_evidence:
        path = root / evidence["raw_path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence["raw_sha256"]
        payload = read(path)
        assert payload["parameters"] == {"fixture": "1636207", "half": "true"}
        team_payload = next(
            t for t in payload["response"] if t["team"]["id"] == evidence["team_id"]
        )
        for field, key in (
            ("statistics", "corners_ft"),
            ("statistics_1h", "corners_h1"),
            ("statistics_2h", "corners_h2"),
        ):
            assert (
                next(s["value"] for s in team_payload[field] if s["type"] == "Corner Kicks")
                == evidence[key]
            )
        assert evidence["corners_h1"] + evidence["corners_h2"] == evidence["corners_ft"]
    sheet(wb, "T30半场结论纠正", list(half_evidence[0]), [list(r.values()) for r in half_evidence])
    wb["先读说明"].append(
        [
            "T30重要纠正",
            "旧原件确有 statistics_1h/2h；AC米兰/本菲卡上半场角球4/1。"
            "旧报告半场不可得结论不成立；非目标样本，不计本次20行覆盖。",
        ]
    )
    sheet(
        wb,
        "请求记录",
        ["步骤", "本次体育请求", "状态与限制"],
        [
            ["现有归档", 0, "本地只读数据库及原件；下载旧Actions原件不属于新体育采集"],
            ["API-Football", 0, provenance["api_football_status"]],
            ["Sportmonks", 0, provenance["sportmonks_status"]],
        ],
    )
    path = out / "FOOTBALL-39项覆盖-归档阶段.xlsx"
    wb.save(path)
    checked = load_workbook(path, read_only=False, data_only=False)
    assert checked["德比郡"].max_row == 11 and checked["博尔顿"].max_row == 11
    assert checked["德比郡"].max_column == 50 and checked["39项覆盖"].max_row == 40
    assert checked["逐项明细"].max_row == 1561
    assert [[c.value for c in row] for row in checked["指标原定义"].iter_rows(min_row=2)] == [
        list(r.values()) for r in registry
    ]
    for team in (58, 52):
        name = teams[str(team)]["name"]
        for pair in (p for p in pairs if p["team_id"] == team):
            cell = checked[name].cell(pair["rank"] + 1, pair["indicator"] + 11)
            assert cell.value == pair["display"] and cell.hyperlink.target.endswith(
                str(pair["detail_row"])
            )
    checked.close()
    write(
        checks / "validation.json",
        {
            "passed": True,
            "checks": [
                "原件SHA256",
                "归档至CSV主客/时间/比分/数值逐条对照",
                "39项编号名称定义原样保留",
                "20候选行/780双方格/1560单方明细",
                "客场本队优先",
                "缺失不替换不补零",
                "同场去重",
                "工作簿重新打开及780链接核对",
            ],
            **summary,
        },
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    build(parser.parse_args().root)
