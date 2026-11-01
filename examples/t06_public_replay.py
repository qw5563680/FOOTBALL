"""Public replay: python examples/t06_public_replay.py --out reports/public-replay."""

import argparse
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import requests

from t05_public_replay import CUTOFF, SHA256, URL
from zucai.cli import main
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.backtest import read_history
from zucai.predict.dixon_coles import DixonColes
from zucai.report.render import load_batch


def replay(output, csv_file=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Output directory exists; choose a new directory.")
    if csv_file:
        raw = Path(csv_file).read_bytes()
    else:
        response = requests.get(URL, timeout=(5, 20))
        response.raise_for_status()
        raw = response.content
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("Public 2019/20 E0.csv SHA256 does not match the verified source.")
    games = pd.read_csv(
        io.BytesIO(raw), usecols=["Date", "Time", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    )
    local = pd.to_datetime(games.Date + " " + games.Time, format="%d/%m/%Y %H:%M")
    games["kickoff"] = local.dt.tz_localize("Europe/London").dt.tz_convert("UTC")
    target = games.loc[
        (games.kickoff.dt.date == CUTOFF.date())
        & (games.HomeTeam == "Arsenal")
        & (games.AwayTeam == "Man United")
    ].iloc[0]
    with TemporaryDirectory() as directory:
        temp = Path(directory)
        db, artifact = temp / "public.db", temp / "model.pkl"
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
                    match = Match(match_id=key, source="demo", league="EPL", status="finished")
                    match.kickoff_utc = game.kickoff
                    match.home_team_id, match.away_team_id = (
                        clubs[game.HomeTeam].id,
                        clubs[game.AwayTeam].id,
                    )
                    session.add(match)
                    result = Result(match_id=key, home_goals=game.FTHG, away_goals=game.FTAG)
                    result.available_utc = game.kickoff + pd.Timedelta(hours=3)
                    result.available_source = "assumed"
                    results.append(result)
                session.flush()
                session.add_all(results)
            with session_scope(engine) as session:
                DixonColes().fit(read_history(session), as_of=CUTOFF).save(artifact)
        finally:
            engine.dispose()
        frozen = temp / "prediction"
        predict = ["predict", "--db", str(db), "--model-file", str(artifact)]
        predict += ["--match-id", f"demo:{target.name}", "--replay", "--as-of", CUTOFF.isoformat()]
        predict += ["--out", str(frozen)]
        if main(predict) or main(
            ["report", "--batch", str(frozen / "forecast.json"), "--out", str(output)]
        ):
            raise ValueError("Public replay predict/report CLI did not finish.")
    batch = load_batch(output / "forecast.json")
    return {
        "source_url": URL,
        "source_sha256": SHA256,
        "source_games": len(games),
        "generated_utc": batch["generated_utc"],
        "as_of_utc": batch["as_of_utc"],
        "models": batch["models"],
        "model_probabilities": batch["predictions"][0]["model_probabilities"],
        "market": batch["predictions"][0]["market"],
        "report_directory": str(output),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="T06 public replay with assumed 3h availability")
    parser.add_argument("--csv", type=Path, help="Original verified E0.csv for offline replay")
    parser.add_argument("--out", type=Path, required=True, help="New persistent report directory")
    args = parser.parse_args()
    try:
        result = replay(args.out, args.csv)
    except (OSError, ValueError, requests.RequestException) as exc:
        parser.exit(2, f"Public replay failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
