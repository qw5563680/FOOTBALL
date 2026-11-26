"""Inspect public fixture pages on the collection runner without importing data."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from zucai.backup_source_pages import discover_pages, inspect_page
from zucai.okooo_probe import AGENT, _get
from zucai.sources.base import RateLimiter, _atomic_write

PAGE_LIMIT = 3
PAGE_LIMITS = {"zgzcw": 6}
DISCOVERY_DOMAINS = {"zgzcw": "zgzcw.com"}
MIN_INTERVAL = 3
MAX_PROBE_INTERVAL = 60
POLICY_KEYS = {"allow", "disallow", "crawl-delay", "request-rate", "visit-time"}
CANDIDATES = (
    ("zgzcw", "中国足彩网", {"sfc": None, "jc": None}, "https://www.zgzcw.com/"),
    ("okooo", "澳客", {"jc": "https://www.okooo.com/jingcai/"}, None),
)
PURPOSE_LABELS = {"sfc": "14 场", "jc": "竞彩", "home": "公开首页"}
UNAVAILABLE = {"robots_denied", "http_error", "requires_js", "blocked"}


@dataclass(frozen=True)
class _Robots:
    rules: list[tuple[str, str]]
    min_interval: float
    windows: list[tuple[str, str]]

    def allows(self, path):
        matches = []
        for key, value in self.rules:
            if key in {"allow", "disallow"} and value:
                end = value.endswith("$")
                parts = [unquote(part) for part in value.removesuffix("$").split("*")]
                regex = ".*".join(re.escape(part) for part in parts)
                if re.match(regex + (r"\Z" if end else ""), unquote(path)):
                    matches.append((sum(map(len, parts)), key == "allow"))
        return not matches or max(matches)[1]

    def open_now(self):
        stamp = datetime.now(UTC).strftime("%H%M")
        return not self.windows or any(
            start <= stamp < end if start <= end else stamp >= start or stamp < end
            for start, end in self.windows
        )


def _robot_policy(raw):
    groups, agents, rules, site_rules = [], [], [], []
    for raw_line in raw.decode("utf-8-sig").splitlines():
        key, _, value = raw_line.partition("#")[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent" and value:
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in POLICY_KEYS:
            (rules if agents else site_rules).append((key, value))
    groups.append((agents, rules))
    if not any(names for names, _ in groups) or b"<html" in raw.lower():
        raise ValueError("robots 正文无法核实")
    products = {AGENT.lower(), AGENT.split("/")[0].lower()}
    specific = [group for names, group in groups if products.intersection(names)]
    applicable = site_rules + [
        rule
        for group in (specific or [group for names, group in groups if "*" in names])
        for rule in group
    ]
    constraints = {
        (key, value)
        for key, value in applicable
        if key in {"crawl-delay", "request-rate", "visit-time"}
    }
    interval, windows = MIN_INTERVAL, []
    for key, value in sorted(constraints):
        if key == "crawl-delay" and re.fullmatch(r"\d{1,5}(?:\.\d{1,3})?", value):
            interval = max(interval, float(value))
        elif key == "request-rate" and (
            rate := re.fullmatch(r"([1-9]\d{0,5})\s*/\s*([1-9]\d{0,5})([smh]?)", value)
        ):
            unit_seconds = {"": 1, "s": 1, "m": 60, "h": 3600}[rate[3]]
            interval = max(interval, int(rate[2]) * unit_seconds / int(rate[1]))
        elif key == "visit-time" and (
            window := re.fullmatch(
                r"([01]\d|2[0-3])([0-5]\d)\s*-\s*(([01]\d|2[0-3])[0-5]\d|2400)", value
            )
        ):
            windows.append((window[1] + window[2], window[3]))
        else:
            raise ValueError(f"robots 的 {key} 无法核实")
    if interval > MAX_PROBE_INTERVAL:
        raise ValueError("robots 请求间隔超出单次探测可用时间")
    return _Robots(applicable, interval, sorted(set(windows)))


def _origin(url):
    return f"https://{urlsplit(url).hostname}"


def _safe_link(url, origin, site_domain):
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    if not hostname or parsed.netloc != hostname:
        return False
    in_scope = (
        hostname == site_domain or hostname.endswith("." + site_domain)
        if site_domain
        else parsed.netloc == urlsplit(origin).netloc
    )
    return (
        parsed.scheme == "https"
        and in_scope
        and parsed.path.startswith("/")
        and not parsed.query
        and not parsed.fragment
    )


def _observation(url):
    return dict(url=url, http_status=None, sha256=None, observed_utc=None, reason=None)


def _observe(session, row):
    row["observed_utc"] = datetime.now(UTC).isoformat()
    try:
        status, raw = _get(session, row["url"])
    except (requests.RequestException, ValueError) as exc:
        row.update(status="transport_error", reason=type(exc).__name__)
        return None
    row.update(http_status=status, sha256=hashlib.sha256(raw).hexdigest())
    if status != 200:
        reason = f"HTTP {status}"
        if 300 <= status < 400:
            reason += "，未跟随跳转"
        row.update(status="http_error", reason=reason)
    return raw


def _save_json(path, payload):
    _atomic_write(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


class _CandidateProbe:
    def __init__(self, session, limiter, output, code, name):
        self.session, self.limiter, self.output = session, limiter, output
        self.code = code
        self.site_domain = DISCOVERY_DOMAINS.get(code)
        self.policies = {}
        self.samples = []
        self.result = dict(
            code=code,
            name=name,
            page_limit=PAGE_LIMITS.get(code, PAGE_LIMIT),
            page_requests=0,
            robots_requests=0,
            robots=[],
            pages=[],
        )

    def _sample(self, row, packet):
        relative = f"samples/{self.code}-{len(self.samples) + 1:02d}.json"
        row["sample_path"] = relative
        _save_json(self.output / relative, {**row, **packet})
        self.samples.append(relative)

    def _robots(self, origin):
        if origin in self.policies:
            return self.policies[origin]
        row = _observation(origin + "/robots.txt")
        self.result["robots"].append(row)
        self.limiter.min_interval_s = MIN_INTERVAL
        self.limiter.wait()
        self.result["robots_requests"] += 1
        raw = _observe(self.session, row)
        policy = None
        if row["http_status"] == 200:
            try:
                policy = _robot_policy(raw)
            except (UnicodeError, ValueError) as exc:
                row.update(status="unverified", reason=str(exc))
            else:
                row.update(
                    status="readable",
                    applied_user_agent=AGENT,
                    rules=[list(rule) for rule in policy.rules],
                    min_interval_s=policy.min_interval,
                    utc_windows=[list(window) for window in policy.windows],
                )
        if raw is not None:
            self._sample(row, {})
        self.policies[origin] = policy
        return policy

    def page(self, purpose, url):
        row = {**_observation(url), "purpose": purpose}
        self.result["pages"].append(row)
        if self.result["page_requests"] >= self.result["page_limit"]:
            row.update(status="budget_exhausted", reason="候选请求已达上限")
            return row
        origin = _origin(url)
        policy = self._robots(origin)
        if policy is None:
            row.update(status="robots_unverified", reason="未取得可核实的访问规则")
            return row
        if not policy.allows(urlsplit(url).path):
            row.update(status="robots_denied", reason="robots 禁止请求该路径")
            return row
        if not policy.open_now():
            row.update(status="outside_window", reason="当前不在 robots 公布的 UTC 时段")
            return row
        self.limiter.min_interval_s = policy.min_interval
        self.limiter.wait()
        if not policy.open_now():
            row.update(status="outside_window", reason="等待限速后已过 robots 公布的 UTC 时段")
            return row
        self.result["page_requests"] += 1
        raw = _observe(self.session, row)
        if raw is not None:
            success = row["http_status"] == 200
            inspection = inspect_page(raw, purpose if success else "home")
            if success and purpose == "home" and inspection["status"] == "discovery":
                labels = []
                links = discover_pages(
                    raw, origin, site_domain=self.site_domain, link_labels=labels
                )
                row.update(status="discovery", links=links, link_labels=labels)
                if not any(links.values()):
                    row["reason"] = "静态首页未确认专用足彩或竞彩赛程链接"
            elif success:
                row.update({k: v for k, v in inspection.items() if k not in {"sample", "evidence"}})
                row["pair_rows"] = inspection["evidence"]["pair_rows"]
            self._sample(
                row,
                {
                    "sample": inspection["sample"] if success and purpose != "home" else [],
                    "evidence": inspection["evidence"],
                },
            )
        return row


def _has_pairs(page):
    return page["status"] in {"readable", "unconfirmed"} and bool(page.get("pair_rows"))


def _stability(checks):
    readings = [
        {
            "status": page["status"],
            "row_count": page.get("row_count"),
            "pair_rows": page.get("pair_rows"),
            "fields": page.get("fields", []),
            "complete_fields": page.get("complete_fields", []),
        }
        for page in checks
    ]
    if (
        len(checks) != 2
        or checks[0]["url"] != checks[1]["url"]
        or not all(_has_pairs(page) for page in checks)
    ):
        return dict(
            status="not_verified", row_count_matches=None, fields_match=None, readings=readings
        )
    first, second = readings
    rows_match = all(first[key] == second[key] for key in ("row_count", "pair_rows"))
    fields_match = all(set(first[key]) == set(second[key]) for key in ("fields", "complete_fields"))
    return dict(
        status="consistent" if rows_match and fields_match else "different",
        row_count_matches=rows_match,
        fields_match=fields_match,
        readings=readings,
    )


def _assessment(purpose, pages):
    checks = [page for page in pages if page["purpose"] == purpose]
    stability = _stability(checks)
    if not checks:
        reason = pages[0]["reason"] if pages and pages[0]["purpose"] == "home" else None
        return {
            "result": "需所有者决定",
            "reason": "专用公开赛程页未确认" + (f"。首页检查：{reason}" if reason else ""),
            "fields": [],
            "stability": stability,
        }
    good = [page for page in checks if page["status"] == "readable"]
    fields = sorted({key for page in checks for key in page.get("fields", [])})
    required = {"seq", "league", "home", "away", "kickoff"}
    if purpose == "sfc":
        required |= {"period", "sale_end"}
    complete = (
        len(good) == 2
        and stability["status"] == "consistent"
        and all(
            required <= set(p["complete_fields"]) and (purpose != "sfc" or p["sfc_order_complete"])
            for p in good
        )
    )
    if complete:
        result, reason = "可用", "同页两次读取的比赛行数和字段一致，均有核心赛程字段"
    elif all(page["status"] in UNAVAILABLE for page in checks):
        result, reason = (
            "不可用",
            "。".join(dict.fromkeys(p["reason"] or p["status"] for p in checks)),
        )
    elif stability["status"] == "different":
        result, reason = "需所有者决定", "同页两次读取的比赛行数或字段不同"
    elif stability["status"] == "consistent":
        result, reason = "需所有者决定", "同页复读结果一致，核心赛程字段未齐"
    elif good:
        result, reason = "需所有者决定", "读到静态赛程，完整性或同页稳定性尚未核实"
    else:
        result, reason = (
            "需所有者决定",
            "。".join(dict.fromkeys(p["reason"] or p["status"] for p in checks)),
        )
    return {"result": result, "reason": reason, "fields": fields, "stability": stability}


def _markdown_text(value):
    return re.sub(r"([\\`*_\[\]])", r"\\\1", escape(value, quote=False))


def _write_report(output, report, samples):
    _save_json(output / "probe.json", report)
    lines = ["# 备用赛程渠道探测", "", f"运行时间（UTC） {report['started_utc']}", ""]
    for site in report["candidates"]:
        lines += [f"## {site['name']}", "", f"候选结论 **{site['result']}**。", ""]
        for purpose, verdict in site["assessment"].items():
            fields = ", ".join(verdict["fields"]) or "未取得"
            lines.append(
                f"- {PURPOSE_LABELS[purpose]} {verdict['result']}。{verdict['reason']}。"
                f"已观察字段 {fields}。"
            )
            stability = verdict["stability"]
            if len(stability["readings"]) == 2:
                descriptions = {
                    "consistent": "一致",
                    "different": "存在差异",
                    "not_verified": "未确认",
                }
                lines.append(f"  - 同页复读 {descriptions[stability['status']]}。")
                for i, reading in enumerate(stability["readings"], 1):
                    covered = ", ".join(reading["fields"]) or "未取得"
                    rows = reading["row_count"] if reading["row_count"] is not None else "未取得"
                    pairs = reading["pair_rows"] if reading["pair_rows"] is not None else "未取得"
                    lines.append(
                        f"    - 第 {i} 次 {reading['status']}，主客对阵 {pairs} 行，"
                        f"有开球时间 {rows} 行，字段 {covered}。"
                    )
        lines.append("")
        for check in site["robots"]:
            rules = json.dumps(check.get("rules", []), ensure_ascii=False)
            windows = check.get("utc_windows") or "未公布"
            lines.append(
                f"- robots `{check['url']}` HTTP {check['http_status']}，{check['status']}。"
                f"适用规则 `{rules}`，UTC 时段 `{windows}`，"
                f"最短间隔 `{check.get('min_interval_s', '未核实')}` 秒。"
            )
        for page in site["pages"]:
            evidence = f" [样本]({page['sample_path']})" if page.get("sample_path") else ""
            lines.append(
                f"- {PURPOSE_LABELS[page['purpose']]} `{page['url']}` "
                f"HTTP {page['http_status']}，{page['status']}。{page['reason'] or ''}{evidence}"
            )
            if page.get("status") == "discovery":
                labels = "、".join(_markdown_text(label) for label in page["link_labels"])
                lines.append(f"  - 首页相关链接文字 {labels or '未观察到'}。")
        lines += [
            f"- robots {site['robots_requests']} 次，"
            f"网页 {site['page_requests']}/{site['page_limit']} 次。",
            "",
        ]
    _atomic_write(output / "README.md", "\n".join(lines).encode())
    manifest = "".join(
        f"{hashlib.sha256((output / path).read_bytes()).hexdigest()}  {path}\n"
        for path in ("probe.json", "README.md", *samples)
    )
    _atomic_write(output / "SHA256SUMS", manifest.encode())


def collect_backup_source_probe(*, output):
    output = Path(output)
    (output / "samples").mkdir(parents=True, exist_ok=True)
    report = dict(started_utc=datetime.now(UTC).isoformat(), candidates=[])
    limiter = RateLimiter(MIN_INTERVAL)
    samples = []
    with requests.Session() as session:
        for code, name, targets, home in CANDIDATES:
            probe = _CandidateProbe(session, limiter, output, code, name)
            links = probe.page("home", home).get("links", {}) if home else {}
            checked = []
            for purpose, target in targets.items():
                if target is None:
                    origin = _origin(home)
                    target = next(
                        (
                            url
                            for url in links.get(purpose, [])
                            if _safe_link(url, origin, probe.site_domain)
                        ),
                        None,
                    )
                if target:
                    checked.append(probe.page(purpose, target))
            for page in checked:
                if _has_pairs(page):
                    probe.page(page["purpose"], page["url"])
            probe.result["assessment"] = {
                purpose: _assessment(purpose, probe.result["pages"]) for purpose in targets
            }
            verdicts = {verdict["result"] for verdict in probe.result["assessment"].values()}
            probe.result["result"] = verdicts.pop() if len(verdicts) == 1 else "需所有者决定"
            report["candidates"].append(probe.result)
            samples.extend(probe.samples)
    _write_report(output, report, samples)
    return report
