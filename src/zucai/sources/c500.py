"""500 竞彩比分玩法的已结算选项。页面缺少精确半场比分。"""

import logging
from datetime import date, datetime
from pathlib import Path

from lxml import html

from zucai.sources.base import RateLimiter, RawStore, Source, make_session
from zucai.sources.results import VOID, GameResult, score
from zucai.sources.sporttery import BEIJING, beijing_kickoff

URL = "https://trade.500.com/jczq/"
LOGGER = logging.getLogger(__name__)


class C500JcHistory(Source[GameResult]):
    name = "c500-results"

    def __init__(self, store=None, session=None):
        self.store = store if store is not None else RawStore()
        self.session = session if session is not None else make_session()
        self.limiter = RateLimiter(1)

    def fetch_raw(self, *, day: date) -> Path:
        self.limiter.wait()
        r = self.session.get(URL, params={"playid": 271, "g": 2, "date": day.isoformat()})
        path = self.store.save_response(self.name, f"scores-{day}", r, "html")
        r.raise_for_status()
        return path

    def parse(self, raw: bytes) -> list[GameResult]:
        doc = html.fromstring(raw, parser=html.HTMLParser(encoding="gbk"))
        rows = doc.xpath("//tr[@data-fixtureid and @data-matchdate]")
        if not rows and not doc.xpath("//*[@id='betTbThead']"):
            raise ValueError("500页面没有竞彩比赛表")
        out = []
        for row in rows:
            score_text = row.xpath("string(.//a[@class='score'])").strip()
            void = score_text in VOID
            sister = row.getnext()
            winners = (
                []
                if sister is None or sister.get("data-fixtureid")
                else sister.xpath(
                    ".//*[@data-type='bf' and "
                    "contains(concat(' ',normalize-space(@class),' '),' sbetbtn-ok ')]/@data-value"
                )
            )
            full = score(winners[0]) if len(winners) == 1 else None
            if full is None and not void:
                if winners:
                    LOGGER.warning(
                        "500 %s 没有精确的已结算比分，保留未知", row.get("data-fixtureid")
                    )
                continue
            when = beijing_kickoff(f"{row.attrib['data-matchdate']} {row.attrib['data-matchtime']}")
            sid = row.get("data-id", "")
            out.append(
                GameResult(
                    f"c500:{int(row.attrib['data-fixtureid'])}",
                    row.attrib["data-simpleleague"],
                    row.attrib["data-matchnum"],
                    row.attrib["data-homesxname"],
                    row.attrib["data-awaysxname"],
                    when.astimezone(BEIJING).date(),
                    "cancelled" if void else "finished",
                    None if void else full,
                    None,
                    kickoff_utc=when,
                    official_id=f"jc:{sid}" if sid.isdecimal() else None,
                )
            )
        return out

    def day_results(self, day: date) -> list[tuple[GameResult, datetime]]:
        path = self.fetch_raw(day=day)
        raw = self.store.load(path)
        # Historical pages can include links and matches from other number dates.
        doc = html.fromstring(raw, parser=html.HTMLParser(encoding="gbk"))
        ids = set(doc.xpath("//tr[@data-processdate=$day]/@data-fixtureid", day=day.isoformat()))
        captured = datetime.fromisoformat(self.store.load_metadata(path)["fetched_at_utc"])
        return [(g, captured) for g in self.parse(raw) if g.match_id.split(":")[1] in ids]
