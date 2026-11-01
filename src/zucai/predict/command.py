"""Training entry point used by the CLI and the standalone public replay."""

from datetime import UTC, datetime
from pathlib import Path

from zucai.db import get_engine, init_db, session_scope
from zucai.predict.backtest import evaluate, read_history, read_market
from zucai.predict.base import utc
from zucai.predict.dixon_coles import DixonColes


def parse_cutoff(value):
    if len(value) == 10:
        value += "T00:00:00+00:00"
    return utc(value)


def train(
    *,
    db=None,
    league=None,
    source=None,
    since=None,
    as_of=None,
    half_life_days=90,
    output=Path("models/dixon-coles.pkl"),
):
    cutoff = utc(as_of if as_of is not None else datetime.now(UTC))
    engine = get_engine(db)
    try:
        database = engine.url.database
        if database != ":memory:" and Path(database).resolve() == Path(output).resolve():
            raise ValueError("模型输出文件与数据库路径相同")
        init_db(engine)
        with session_scope(engine) as session:
            games = read_history(session, league, source)
            prices = read_market(session, games)
        model = DixonColes(half_life_days).fit(games, as_of=cutoff)
        start = since if since is not None else games.kickoff_utc.min().normalize()
        report = evaluate(games, prices, since=start, as_of=cutoff, half_life_days=half_life_days)
        model.save(output)
        return {
            "model_name": "dixon-coles",
            "engine": "penaltyblog 1.13.1",
            "league": model.league,
            "source": model.source,
            "training_rows": model.training_rows,
            "cutoff_utc": cutoff.isoformat(),
            "last_available_utc": model.latest_available.isoformat(),
            "half_life_days": model.half_life_days,
            "market_source": "pre-kickoff had snapshots",
            "model_path": str(output),
            "backtest": report,
        }
    finally:
        engine.dispose()
