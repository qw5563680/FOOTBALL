"""Extract current-fixture statements from the original T70 research report."""

import re
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

_BEIJING = timezone(timedelta(hours=8))
_SECTION = re.compile(
    r"^(?:比赛身份核对|比赛身份|赛程核对|本场(?:比赛|赛程|对阵)(?:信息)?|比赛信息)"
    r"(?=[:\s]|$)[:\s]*"
)
_MISMATCH = re.compile(r"^比赛身份(?:对不上|不一致)(?=[\s。:，,]|$)")
_HISTORY = re.compile(
    r"^(?:此前|上[一个]?(?:场|回合)|前一(?:次|场)|最近|近期|历史|过往|双方(?:近|上))"
)
_OUTSIDE_IDENTITY = re.compile(
    r"^(?:数据截止时间|关键伤停与首发|伤停|首发|主要依据|反证|市场最高概率项|研究判断(?:的概率)?|"
    r"依据类型|与我们模型的差别|引用列表|交锋)(?=[:\s]|$)"
)
_SIDE = re.compile(r"^(主队|客队|home(?: team)?|away(?: team)?)\s*[:=]\s*", re.I)
_LEAGUE = re.compile(r"^(?:赛事|联赛|比赛赛事)\s*:\s*")
_KICKOFF = re.compile(
    r"^(?:本场)?(?:北京时间|北京(?:时间)?开球时间|开球时间|比赛日期|开赛时间)\s*:\s*"
)
_BEIJING_TAG = re.compile(r"北京时间|北京开球时间|\b(?:BJT|Beijing time)\b", re.I)
_KICKOFF_PREFIX = re.compile(r"^(?:本场)?(?:北京时间|北京开球时间|UTC|BJT)\s*[:=]?\s*$", re.I)
_ROUND = re.compile(r"\s*第\s*\d+\s*(?:轮|回合)$")
_COMPETITION = re.compile(r"^[\w ·-]{1,50}(?:联赛|超|冠|甲|乙|杯)$")
_MATCH_ID = re.compile(r"^(?:比赛|赛程|来源比赛)(?:编号|ID)\s*:\s*([a-z][\w-]*:[\w-]+)$", re.I)
_JC_NUMBER = re.compile(r"^(?:竞彩|竞彩比赛)编号\s*:\s*(周[一二三四五六日]\d{3})$")
_SOURCE = re.compile(r"^(?:来源|出处)\s*:")
_VERSUS = re.compile(r"(?<![A-Za-z])vs\.?(?![A-Za-z])|对阵", re.I)
_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d\d)(?:[-/.]|年)(?P<month>\d\d?)(?:[-/.]|月)(?P<day>\d\d?)日?"
    r"(?:[ T]+(?P<hour>\d\d?):(?P<minute>\d\d)(?::(?P<second>\d\d))?"
    r"(?:\s*(?P<zone>Z|UTC|[+-]\d\d:?\d\d))?)?(?!\d)",
    re.I,
)
_URL = re.compile(r"https?://[^\s<>\"'()（）【】、，。]+", re.I)
_LINK_PARENS = re.compile(rf"[(（]\s*{_URL.pattern}\s*[)）]", re.I)
_LINE = re.compile(r"[^\r\n]+")
_CLAUSE = re.compile(r"[^，,。；;]+")
_MARKDOWN = re.compile(r"^(?:#{1,6}\s+|[-*+]\s+|\d{1,2}[.)、]\s*)+")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t\r]*\n")


def _clean(text):
    text = unicodedata.normalize("NFKC", text).strip()
    return _MARKDOWN.sub("", text).replace("**", "").replace("__", "").strip()


def _without_urls(text):
    return _URL.sub("", _LINK_PARENS.sub("", text)).strip()


def _name(text):
    return text.strip(" \t:。'\"‘’“”「」『』")


def _pair(text):
    text = re.sub(r"^(?:本场(?:比赛|对阵|交锋)?(?:为|是)?|对阵)[:\s]*", "", text)
    if match := _DATE.search(text):
        text = re.sub(r"\s*(?:于\s*)?(?:北京时间)?\s*$", "", text[: match.start()])
    # This form belongs to the report's ordered fixture statement, not a history section.
    parts = _VERSUS.split(text)
    if len(parts) != 2:
        return None
    home = _name(_SIDE.sub("", parts[0].strip()))
    away = _name(_SIDE.sub("", parts[1].strip()))
    return (home, away) if 0 < len(home) <= 120 and 0 < len(away) <= 120 else None


def _ordered_pair(text):
    # The compact wording uses a clearly ordered pair followed by Beijing kickoff.
    match = re.fullmatch(r"(.{1,120})与(.{1,120})于\s*北京时间\s*(20\d\d.*)\s*开(?:球|赛)", text)
    return (_name(match[1]), _name(match[2])) if match else _pair(text)


def _kickoff(text):
    match = _DATE.search(text)
    if match is None or not (
        not text[: match.start()].strip()
        or _KICKOFF.match(text)
        or _KICKOFF_PREFIX.fullmatch(text[: match.start()].strip())
        or _ordered_pair(text)
    ):
        return None
    try:
        pieces = match.groupdict()
        hour = pieces["hour"]
        day = datetime(int(pieces["year"]), int(pieces["month"]), int(pieces["day"]))
        if hour is None:
            return day.date().isoformat(), None
        clock = day.replace(
            hour=int(hour), minute=int(pieces["minute"]), second=int(pieces["second"] or 0)
        )
        zone = pieces["zone"]
        if zone:
            stamp = (
                clock.replace(tzinfo=UTC)
                if zone.upper() in {"Z", "UTC"}
                else datetime.fromisoformat(clock.isoformat() + zone)
            )
        elif _BEIJING_TAG.search(text):
            stamp = clock.replace(tzinfo=_BEIJING)
        elif re.search(r"\bUTC\b", text, re.I):
            stamp = clock.replace(tzinfo=UTC)
        else:
            return day.date().isoformat(), None
        return stamp.astimezone(_BEIJING).date().isoformat(), stamp.astimezone(UTC).isoformat()
    except ValueError:
        return None


def _continuation(text):
    without_urls = _without_urls(text)
    return (
        _SIDE.match(without_urls)
        or _LEAGUE.match(without_urls)
        or _KICKOFF.match(without_urls)
        or _SOURCE.match(without_urls)
        or _MATCH_ID.fullmatch(without_urls)
        or _JC_NUMBER.fullmatch(without_urls)
        or _kickoff(without_urls)
        or _ordered_pair(without_urls)
    )


def _blocks(report):
    block = []
    for line in _LINE.finditer(report):
        text = _clean(line[0])
        if not text:
            continue
        if _SECTION.match(text) or _MISMATCH.match(text):
            if block:
                yield block
            block = [line]
        elif (
            block
            and not _HISTORY.match(text)
            and not _OUTSIDE_IDENTITY.match(text)
            and not line[0].lstrip().startswith("#")
            and (
                not _PARAGRAPH_BREAK.search(report[block[-1].end() : line.start()])
                or (len(block) == 1 and not _SECTION.sub("", _clean(block[0][0])))
            )
            and _continuation(text)
        ):
            block.append(line)
        elif block:
            yield block
            block = []
    if block:
        yield block


def _clauses(block):
    for line in block:
        # Retain character offsets for provider annotations, even when a URL contains commas.
        masked = _URL.sub(lambda match: " " * len(match[0]), line[0])
        for segment in _CLAUSE.finditer(masked):
            start, end = segment.span()
            clause = _SECTION.sub("", _clean(_without_urls(line[0][start:end])))
            if _OUTSIDE_IDENTITY.match(clause):
                break
            if not _HISTORY.match(clause):
                yield clause, line.start() + start, line.start() + end


def _links(report, citations, spans):
    urls = []
    for start, end in spans:
        urls.extend(match.rstrip(".,;)]}") for match in _URL.findall(report[start:end]))
    for citation in citations:
        data = citation.get("url_citation") if isinstance(citation, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("url"), str):
            continue
        pos, stop = data.get("start_index"), data.get("end_index")
        if (
            type(pos) is int
            and type(stop) is int
            and 0 <= pos < stop <= len(report)
            and any(start <= pos < end for start, end in spans)
        ):
            urls.append(data["url"])
    return list(dict.fromkeys(urls))


def _observations(report, citations, block):
    pairs, dates, sides, side_spans, metadata = [], [], {}, {}, {}
    spans = []
    for clause, start, end in _clauses(block):
        spans.append((start, end))
        stamp = _kickoff(clause)
        if stamp and stamp not in dates:
            dates.append(stamp)
        if clause.startswith("不一致") or _MISMATCH.match(clause):
            metadata["explicit_mismatch"] = True
        if side := _SIDE.match(clause):
            key = "home" if side[1].lower().startswith(("主", "home")) else "away"
            value = _name(clause[side.end() :])
            if value and key in sides and sides[key] != value:
                pairs.append(({key: sides[key]}, None, side_spans[key]))
            if value:
                sides[key], side_spans[key] = value, [(start, end)]
        elif pair := _ordered_pair(clause):
            pairs.append((dict(zip(("home", "away"), pair, strict=True)), stamp, [(start, end)]))
        if league := _LEAGUE.match(clause):
            metadata["league"] = _ROUND.sub("", clause[league.end() :]).strip()
        elif _ROUND.search(clause) or _COMPETITION.fullmatch(clause):
            metadata["league"] = _ROUND.sub("", clause).strip()
        elif identifier := _MATCH_ID.fullmatch(clause):
            metadata["claimed_match_id"] = identifier[1]
        elif identifier := _JC_NUMBER.fullmatch(clause):
            metadata["claimed_jc_num"] = identifier[1]

    if sides or not pairs:
        pairs.append((sides, None, [span for side in side_spans.values() for span in side]))
    team_links = {side: _links(report, citations, values) for side, values in side_spans.items()}
    for pair, date, pair_spans in pairs:
        # Separate competing pair statements rather than piecing together a new fixture.
        available_dates = dates if len(pairs) == 1 else [date] if date else []
        links = _links(report, citations, spans if len(pairs) == 1 else pair_spans)
        for stamp in available_dates or [None]:
            evidence = {
                **metadata,
                **pair,
                "text": report[block[0].start() : block[-1].end()],
                "urls": links,
            }
            if stamp:
                evidence["date_beijing"] = stamp[0]
                if stamp[1] is not None:
                    evidence["kickoff_utc"] = stamp[1]
            if pair is sides and any(team_links.values()):
                evidence["team_urls"] = team_links
            yield evidence


def extract_identity_evidence(report, citations=()):
    """Return ordered current-fixture claims and locally attached source links."""
    if not isinstance(report, str):
        return []
    return [row for block in _blocks(report) for row in _observations(report, citations, block)]
