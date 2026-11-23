"""Read-only public page probe for the project runner. No production imports or writes."""

import argparse
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests
from lxml import etree, html

from zucai.sfc_verification import verify_issue
from zucai.sources.base import RateLimiter, _atomic_write
from zucai.sources.sfc_official import parse_notice
from zucai.sources.sporttery import BEIJING

ORIGIN = "https://www.okooo.com"
AGENT = "ZucaiResearch/0.1"
LIMIT = 2 * 1024 * 1024


def _text(nodes):
    return " ".join(nodes[0].text_content().split()) if len(nodes) == 1 else ""


def _deadline(label, rows):
    # The page prints MM-DD only. Accept the unique matching weekday near the first kickoff.
    m = re.fullmatch(r"(\d\d-\d\d)\(周([一二三四五六日])\) (\d\d:\d\d)", label)
    times = [r["kickoff_beijing"] for r in rows if r["kickoff_beijing"]]
    if not m or not times:
        return None
    first = datetime.fromisoformat(min(times))
    candidates = []
    for year in (first.year - 1, first.year):
        try:
            end = datetime.fromisoformat(f"{year}-{m[1]}T{m[3]}").replace(tzinfo=BEIJING)
        except ValueError:
            continue
        if "一二三四五六日"[end.weekday()] == m[2] and timedelta(0) <= first - end <= timedelta(
            days=7
        ):
            candidates.append(end.astimezone(UTC).isoformat())
    return candidates[0] if len(candidates) == 1 else None


def inspect_page(raw, expected, game, at):
    result = dict(
        read_status="pending",
        issue_no=None,
        game=game,
        rows=[],
        reasons=[],
        deadline_label=None,
        sale_end_utc=None,
        sales_status="页面未明确记录",
    )
    try:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            text = raw.decode("gb18030")
        doc = html.fromstring(text)
    except (UnicodeError, etree.ParserError):
        result["reasons"].append("页面不可解析")
        return result
    reasons, rows = result["reasons"], result["rows"]
    ids = doc.xpath('//*[@id="topLotteryNo"]/@rel')
    result["issue_no"] = ids[0] if len(ids) == 1 else None
    if (
        not re.fullmatch(r"[0-9]{5}", result["issue_no"] or "")
        or expected
        and result["issue_no"] != expected
    ):
        reasons.append("页面期号缺失或与请求不符")
    game_label = "胜负彩" if game == "sfc" else "任选九场"
    title = _text(doc.xpath("//title"))
    if game_label not in title and not (game == "ren9" and "任选9场" in title):
        reasons.append("页面玩法待核")
    result["deadline_label"] = _text(
        doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," overTime ")]/em')
    )
    for tr in doc.xpath('//table[@id="table4"]//tr[td]'):

        def field(cls, tr=tr):
            return tr.xpath(
                './/*[contains(concat(" ",normalize-space(@class)," "),$c)]', c=f" {cls} "
            )

        seq = _text(field("xh"))
        home, away = _text(field("homenameobj")), _text(field("awaynameobj"))
        times = [node.get("title", "") for node in field("timetd")]
        kickoff = None
        if len(times) == 1 and (
            m := re.fullmatch(r"比赛时间[:：]\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", times[0])
        ):
            try:
                kickoff = datetime.fromisoformat(m[1]).replace(tzinfo=BEIJING).isoformat()
            except ValueError:
                pass
        fixture_ids = {
            m[1]
            for link in tr.xpath(".//a/@href")
            if (m := re.fullmatch(r"/soccer/match/([0-9]+)/(?:odds|history|qingbao)/", link))
        }
        fid = next(iter(fixture_ids)) if len(fixture_ids) == 1 else None
        if not all((home, away, kickoff, fid)) or home == away:
            reasons.append(f"第{seq}场队名、时间或比赛编号缺失/不唯一")
        rows.append(
            dict(
                seq=int(seq) if re.fullmatch(r"[0-9]{1,2}", seq) else None,
                league=_text(field("jsLeagueName")),
                home=home,
                away=away,
                kickoff_beijing=kickoff,
                okooo_fixture_id=fid,
                home_team_id=None,
                away_team_id=None,
            )
        )
    if [r["seq"] for r in rows] != list(range(1, 15)):
        reasons.append("场序缺失、重复或顺序不符")
    if len({r["okooo_fixture_id"] for r in rows}) != len(rows):
        reasons.append("比赛编号重复")
    result["sale_end_utc"] = _deadline(result["deadline_label"], rows)
    if not result["sale_end_utc"]:
        reasons.append("停售时间待核")
    else:
        result["deadline_state"] = (
            "截止前"
            if datetime.fromisoformat(at) < datetime.fromisoformat(result["sale_end_utc"])
            else "已到截止"
        )
    if not reasons:
        result["read_status"] = "complete"
    return result


def compare(page, notices, at):
    # Okooo IDs have their own namespace. Never pass them to the 500 team alias resolver.
    check = verify_issue(page["issue_no"], page["sale_end_utc"], page["rows"], notices, at)
    check.update(provider="okooo.com", notice_input="archived_notice_replay")
    return check


def _get(session, url):
    with session.get(
        url, headers={"User-Agent": AGENT}, timeout=(8, 15), allow_redirects=False, stream=True
    ) as response:
        raw = bytearray()
        for chunk in response.iter_content(32768):
            raw.extend(chunk)
            if len(raw) > LIMIT:
                raise ValueError("响应过大")
        return response.status_code, bytes(raw)


def observe(session, url, expected, game, out):
    at = datetime.now(UTC).isoformat()
    result = dict(
        url=url,
        expected=expected,
        game=game,
        observed_utc=at,
        read_status="pending",
        http_status=None,
        rows=[],
        reasons=[],
    )
    try:
        status, raw = _get(session, url)
    except (requests.RequestException, ValueError) as exc:
        result["reasons"] = [type(exc).__name__]
        return result
    sha = hashlib.sha256(raw).hexdigest()
    result.update(http_status=status, sha256=sha)
    directory = out / "raw"
    directory.mkdir(exist_ok=True, parents=True)
    target = directory / f"{sha}.html"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
            raise ValueError("已有页面哈希不符")
    else:
        _atomic_write(target, raw)
    if status == 200:
        result.update(inspect_page(raw, expected, game, at))
    else:
        result["reasons"] = [f"HTTP {status}"]
    return result


def permitted_time(raw, now):
    """Visit-time is expressed in UTC. Observe the site's published UTC window."""
    ranges = re.findall(rb"^Visit-time:\s*(\d{4})-(\d{4})", raw, re.I | re.M)
    stamp = now.astimezone(UTC).strftime("%H%M").encode()
    return not ranges or any(
        (start <= stamp < end if start <= end else stamp >= start or stamp < end)
        for start, end in ranges
    )


def compare_sheets(observations, number):
    pages = [p for p in observations if number in (p.get("issue_no"), p.get("expected"))]
    complete = [p for p in pages if p["read_status"] == "complete" and p["issue_no"] == number]
    reasons = []
    if {p["game"] for p in complete} != {"sfc", "ren9"}:
        reasons.append("两种玩法未同时取得完整页面")
    differences = []
    if complete:
        reference = complete[0]
        for page in complete[1:]:
            if reference["sale_end_utc"] != page["sale_end_utc"]:
                differences.append(
                    dict(
                        field="sale_end_utc",
                        first=reference["sale_end_utc"],
                        second=page["sale_end_utc"],
                        urls=[reference["url"], page["url"]],
                    )
                )
            for a, b in zip(reference["rows"], page["rows"], strict=True):
                for field in ("home", "away", "kickoff_beijing", "okooo_fixture_id", "league"):
                    if a[field] != b[field]:
                        differences.append(
                            dict(
                                seq=a["seq"],
                                field=field,
                                first=a[field],
                                second=b[field],
                                urls=[reference["url"], page["url"]],
                            )
                        )
    if any(
        p.get("read_status") != "complete" or p.get("notice_check", {}).get("status") != "verified"
        for p in pages
    ):
        reasons.append("页面或公告逐项核对仍有待核项")
    conflict = bool(differences) or any(
        p.get("notice_check", {}).get("status") == "conflict" for p in pages
    )
    return dict(
        status="conflict" if conflict else "pending" if reasons else "verified",
        reasons=reasons,
        differences=differences,
    )


def run(numbers, out, official, notice_url):
    if (
        len(numbers) != 2
        or len(set(numbers)) != 2
        or not all(re.fullmatch("[0-9]{5}", n) for n in numbers)
    ):
        raise ValueError("须指定两个不同的五位期号")
    output = dict(robot={}, observations=[])
    with requests.Session() as session:
        robot = RobotFileParser()
        limiter = RateLimiter(3)
        try:
            limiter.wait()
            status, raw = _get(session, ORIGIN + "/robots.txt")
            output["robot"].update(
                status=status,
                sha256=hashlib.sha256(raw).hexdigest(),
                observed_utc=datetime.now(UTC).isoformat(),
            )
            if status in (404, 410):
                robot.parse([])
                raw = b""
            elif status == 200 and b"<html" not in raw.lower() and b"user-agent:" in raw.lower():
                robot.parse(raw.decode("utf-8-sig").splitlines())
                delay = robot.crawl_delay(AGENT)
                rate = robot.request_rate(AGENT)
                limiter.min_interval_s = max(
                    3, delay or 0, rate.seconds / rate.requests if rate and rate.requests else 0
                )
            else:
                raise ValueError("robots HTTP 或内容无法核实")
        except (ValueError, requests.RequestException) as exc:
            raw = b""
            output["robot"]["error"] = type(exc).__name__
        for game, base in (("sfc", "/zucai/"), ("ren9", "/zucai/ren9/")):
            for expected in (None, *numbers):
                url = ORIGIN + base + (expected + "/" if expected else "")
                allowed = not output["robot"].get("error") and robot.can_fetch(AGENT, url)
                if allowed:
                    limiter.wait()
                if not allowed or not permitted_time(raw, datetime.now(UTC)):
                    item = dict(
                        url=url,
                        expected=expected,
                        game=game,
                        read_status="pending",
                        reasons=["robots 或访问时段未放行"],
                        rows=[],
                    )
                else:
                    item = observe(session, url, expected, game, out)
                    if item.get("issue_no") in numbers:
                        notices = parse_notice(
                            official.read_bytes(),
                            source_url=notice_url,
                            observed_utc=item["observed_utc"],
                        )
                        item["notice_check"] = compare(item, notices, item["observed_utc"])
                output["observations"].append(item)
    output["periods"] = {n: compare_sheets(output["observations"], n) for n in numbers}
    out.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(output, ensure_ascii=False, indent=2)
    # Keep successive observations and deduplicate unchanged response bytes.
    _atomic_write(out / f"observation-{uuid.uuid4().hex}.json", (encoded + "\n").encode("utf-8"))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issues", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--notice-file", type=Path, required=True)
    parser.add_argument("--notice-url", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{5},[0-9]{5}", args.issues):
        parser.error("须指定两个五位期号")
    result = run(args.issues.split(","), args.out, args.notice_file, args.notice_url)
    for row in result["observations"]:
        print(row["url"], row.get("http_status"), row["read_status"], len(row["rows"]))
