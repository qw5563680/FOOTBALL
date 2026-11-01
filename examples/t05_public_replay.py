"""Run from the repo root: python examples/t05_public_replay.py [--csv /path/to/E0.csv]."""

import argparse
import hashlib
import io
import json
from math import log
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
import requests
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Prediction, Result, Team
from zucai.predict.backtest import evaluate, read_history
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes

URL = "https://www.football-data.co.uk/mmz4281/1920/E0.csv"
SHA256 = "100037618b94f94057400bb02bf6bac4ef74ddaa58cde4b38370839c39caee61"
CUTOFF = pd.Timestamp("2020-01-01T00:00:00Z")
END = pd.Timestamp("2020-08-01T00:00:00Z")


def replay(csv_file=None):
    if csv_file is None:
        response = requests.get(URL, timeout=(5, 20))
        response.raise_for_status()
        raw = response.content
    else:
        raw = Path(csv_file).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("样例内容与已核验的 2019/20 英超 E0.csv 不一致")
    games = pd.read_csv(
        io.BytesIO(raw), usecols=["Date", "Time", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    )
    # The publisher labels these Bet365 closing prices; the file gives no quote timestamps.
    # Keep them outside the model input and outside the official timestamped odds table.
    archive = pd.read_csv(io.BytesIO(raw), usecols=["B365CH", "B365CD", "B365CA"])
    closing = {}
    for idx, h, d, a in archive.itertuples():
        prices = np.array([h, d, a], dtype=float)
        if np.isfinite(prices).all() and (prices > 1).all():
            inverse = 1 / prices
            closing[f"demo:{idx}"] = tuple(inverse / inverse.sum())
    local = pd.to_datetime(games.Date + " " + games.Time, format="%d/%m/%Y %H:%M")
    games["kickoff"] = local.dt.tz_localize("Europe/London").dt.tz_convert("UTC")
    target = games.loc[
        (games.kickoff.dt.date == CUTOFF.date())
        & (games.HomeTeam == "Arsenal")
        & (games.AwayTeam == "Man United")
    ].iloc[0]
    with TemporaryDirectory() as temp:
        db = Path(temp) / "example.db"
        engine = get_engine(db)
        try:
            init_db(engine)
            with session_scope(engine) as session:
                clubs = {
                    name: Team(name=name)
                    for name in sorted(set(games.HomeTeam) | set(games.AwayTeam))
                }
                session.add_all(clubs.values())
                session.flush()
                results = []
                for game in games.itertuples():
                    key = f"demo:{game.Index}"
                    session.add(
                        Match(
                            match_id=key,
                            source="demo",
                            league="EPL",
                            kickoff_utc=game.kickoff,
                            home_team_id=clubs[game.HomeTeam].id,
                            away_team_id=clubs[game.AwayTeam].id,
                            status="finished",
                        )
                    )
                    # This CSV has no first-publication timestamps. Only this demo assumes
                    # scores were available 3 hours after kickoff.
                    available = game.kickoff + pd.Timedelta(hours=3)
                    results.append(
                        Result(
                            match_id=key,
                            home_goals=game.FTHG,
                            away_goals=game.FTAG,
                            available_utc=available,
                            available_source="assumed",
                        )
                    )
                session.flush()
                session.add_all(results)
            with session_scope(engine) as session:
                history = read_history(session)
            model = DixonColes().fit(history, as_of=CUTOFF)
            model_path = Path(temp) / "model.pkl"
            model.save(model_path)
            restored = DixonColes.load(model_path)
            grid, provenance = restored.predict_with_metadata(
                {"home": target.HomeTeam, "away": target.AwayTeam, "kickoff_utc": target.kickoff}
            )
            p = had(grid)
            with session_scope(engine) as session:
                session.add(
                    Prediction(
                        match_id=f"demo:{target.name}",
                        model_name="dixon-coles",
                        model_version="penaltyblog-1.13.1-demo",
                        p_home=p["h"],
                        p_draw=p["d"],
                        p_away=p["a"],
                        score_matrix=grid.tolist(),
                    )
                )
            with session_scope(engine) as session:
                saved = session.scalars(select(Prediction)).one()
                probabilities = {"home": saved.p_home, "draw": saved.p_draw, "away": saved.p_away}
            report = evaluate(history, closing, since=CUTOFF, as_of=END)
            slower = evaluate(
                history, closing, since=CUTOFF, as_of=END, half_life_days=log(2) / 0.001
            )
        finally:
            engine.dispose()
    return {
        "fixture": "Arsenal - Man United",
        "kickoff_utc": target.kickoff.isoformat(),
        "training_cutoff": CUTOFF.isoformat(),
        "training_games": model.training_rows,
        "probabilities": probabilities,
        "provenance": provenance,
        "weekly_backtest": report,
        "comparison_xi_0_001": {k: slower["model"][k] for k in ("n", "log_loss", "brier")},
        "source_sha256": SHA256,
        "availability": "Demo assumes score known 3h after kickoff; publication time unverified.",
        "market_source": (
            "Football-Data Bet365 closing prices B365CH/B365CD/B365CA; "
            "quote timestamps unverified; no official JC snapshots."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="2019/20 英超公开回放，临时库自动释放")
    parser.add_argument("--csv", type=Path, help="已下载的原始 2019/20 英超 E0.csv")
    options = parser.parse_args()
    try:
        result = replay(options.csv)
    except (OSError, ValueError, requests.RequestException) as exc:
        parser.exit(2, f"公开样例未完成：{exc}\n可下载 {URL} 后通过 --csv 指定原文件。\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
