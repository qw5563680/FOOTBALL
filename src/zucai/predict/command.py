"""Training entry point used by the CLI and the standalone public replay."""

from datetime import UTC, datetime
from pathlib import Path

from zucai.db import get_engine, init_db, session_scope
from zucai.predict.backtest import evaluate, read_history, read_market
from zucai.predict.base import availability_sources, utc
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
    strict_availability=False,
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
        training = (
            games.loc[availability_sources(games) != "assumed"] if strict_availability else games
        )
        model = DixonColes(half_life_days).fit(training, as_of=cutoff)
        start = since if since is not None else games.kickoff_utc.min().normalize()
        report = evaluate(games, prices, since=start, as_of=cutoff, half_life_days=half_life_days)
        strict_report = (
            evaluate(
                games,
                prices,
                since=start,
                as_of=cutoff,
                half_life_days=half_life_days,
                strict_availability=True,
            )
            if strict_availability
            else None
        )
        model.save(output)
        return {
            "model_name": "dixon-coles",
            "engine": "penaltyblog 1.13.1",
            "league": model.league,
            "sources": model.sources,
            "training_source_counts": model.training_source_counts,
            "training_rows": model.training_rows,
            "availability_basis": model.availability_basis,
            "assumed_training_share": model.assumed_training_share,
            "strict_availability": strict_availability,
            "cutoff_utc": cutoff.isoformat(),
            "last_available_utc": model.latest_available.isoformat(),
            "half_life_days": model.half_life_days,
            "market_source": "pre-kickoff had snapshots",
            "model_path": str(output),
            "backtest": report,
            "strict_backtest": strict_report,
        }
    finally:
        engine.dispose()
