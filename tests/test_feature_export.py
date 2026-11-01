import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from test_features import CUTOFF, KICKOFF, game, quotes
from test_features import feature_db as feature_db
from zucai.db import session_scope
from zucai.models import Match


def example_frame(feature_db):
    from zucai.features.build import build_features

    engine, teams = feature_db
    with session_scope(engine) as s:
        game(s, teams, "known", 5, goals=(2, 1))
        return build_features(s, ["jc:target"], as_of=CUTOFF)


def test_parquet_round_trip_keeps_feature_values_evidence_and_hashes(feature_db, tmp_path):
    from zucai.features.command import write_features

    frame = example_frame(feature_db)
    out = tmp_path / "output/features.parquet"
    manifest = write_features(frame, out)
    pd.testing.assert_frame_equal(pd.read_parquet(out), frame)
    metadata = json.loads(out.with_suffix(".metadata.json").read_bytes())
    assert metadata["rows"] == 1
    assert metadata["model_missing_rates"]["home_last5_goals_for_mean"] == 0
    assert metadata["model_missing_rates"]["away_last5_goals_for_mean"] == 1
    assert metadata["market_missing_rates"]["market_p_home"] == 1
    assert metadata["audit"]["jc:target"]["windows"]["home_last5"][0]["match_id"] == "jc:known"
    assert metadata["model_feature_columns"] == frame.attrs["model_feature_columns"]
    assert manifest == json.loads(out.with_suffix(".manifest.json").read_bytes())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((out.parent / name).read_bytes()).hexdigest() == digest


def test_parquet_preserves_nullable_integer_hhad_line_and_schedule_only_rest_evidence(
    feature_db, tmp_path
):
    from zucai.features.build import MARKET_COLUMNS, MODEL_FEATURE_COLUMNS, build_features
    from zucai.features.command import write_features

    engine, teams = feature_db
    stamp = CUTOFF - timedelta(hours=1)
    with session_scope(engine) as s:
        recent = game(s, teams, "cup", 1, league="Cup", goals=None, status="scheduled")
        s.add(
            Match(
                match_id="jc:no-quote",
                source="jc",
                league="L",
                home_team_id=teams["X"],
                away_team_id=teams["Y"],
                kickoff_utc=KICKOFF + timedelta(days=1),
                status="scheduled",
            )
        )
        quotes(s, stamp, {"h": 3.0, "d": 4.0, "a": 5.0}, line=-2)
        frame = build_features(s, ["jc:target", "jc:no-quote"], as_of=CUTOFF)
    out = tmp_path / "v2.parquet"
    manifest = write_features(frame, out)
    read = pd.read_parquet(out)
    pd.testing.assert_frame_equal(read, frame)
    assert read["market_hhad_line"].dtype == pd.Int64Dtype()
    assert read.loc[0, "market_hhad_line"] == -2
    assert pd.isna(read.loc[1, "market_hhad_line"])
    assert read.loc[0, "meta_home_rest_match_id"] == recent
    assert read.loc[0, "home_rest_days"] == 1
    assert set(MARKET_COLUMNS).isdisjoint(MODEL_FEATURE_COLUMNS)
    metadata = json.loads(out.with_suffix(".metadata.json").read_bytes())
    assert metadata["schema_version"] == manifest["schema_version"] == 1
    assert metadata["feature_version"] == "t04-v2"
    assert metadata["market_missing_rates"]["market_hhad_line"] == 0.5
    assert metadata["market_missing_rates"]["market_hhad_p_home"] == 0.5
    assert metadata["market_columns"] == list(MARKET_COLUMNS)
    assert "market_line" not in metadata["columns"]
    hhad = metadata["audit"]["jc:target"]["market_hhad"]
    assert hhad["line"] == -2 and isinstance(hhad["line"], int)
    assert hhad["prices"] == {"h": 3.0, "d": 4.0, "a": 5.0}
    assert pd.Timestamp(hhad["snapshot_utc"]) == stamp
    rest = metadata["audit"]["jc:target"]["windows"]["home_rest"]
    assert len(rest) == 1 and rest[0]["match_id"] == recent
    assert set(rest[0]) == {"match_id", "source", "league", "kickoff_utc", "status", "venue"}
    assert metadata["audit"] == frame.attrs["audit"]
    assert manifest == json.loads(out.with_suffix(".manifest.json").read_bytes())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((out.parent / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("suffix", [".parquet", ".metadata.json", ".manifest.json"])
def test_any_existing_artifact_prevents_overwrite(feature_db, tmp_path, suffix):
    from zucai.features.command import write_features

    frame = example_frame(feature_db)
    out = tmp_path / "features.parquet"
    existing = out.with_suffix(suffix)
    existing.write_bytes(b"older evidence")
    with pytest.raises(FileExistsError):
        write_features(frame, out)
    assert existing.read_bytes() == b"older evidence"
    assert len(list(tmp_path.glob("features.*"))) == 1


def test_partial_export_removes_only_its_own_files(feature_db, tmp_path, monkeypatch):
    from zucai.features.command import write_features

    frame = example_frame(feature_db)
    out = tmp_path / "features.parquet"
    metadata = out.with_suffix(".metadata.json")
    real_open = Path.open

    def race(path, mode="r", *args, **kwargs):
        if path == metadata and mode == "xb":
            with real_open(path, "wb") as stream:
                stream.write(b"created concurrently")
            raise FileExistsError("concurrent output")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", race)
    with pytest.raises(FileExistsError):
        write_features(frame, out)
    assert not out.exists()
    assert metadata.read_bytes() == b"created concurrently"
    assert not out.with_suffix(".manifest.json").exists()


def test_empty_selection_is_exportable_with_null_missing_ratios(feature_db, tmp_path):
    from zucai.features.build import build_features
    from zucai.features.command import write_features

    engine, _ = feature_db
    with session_scope(engine) as s:
        frame = build_features(s, [], as_of=CUTOFF)
    out = tmp_path / "empty.parquet"
    write_features(frame, out)
    read = pd.read_parquet(out)
    assert read.empty and list(read.columns) == list(frame.columns)
    metadata = json.loads(out.with_suffix(".metadata.json").read_bytes())
    assert metadata["rows"] == 0
    assert set(metadata["model_missing_rates"].values()) == {None}


def test_cli_uses_existing_db_read_only_and_logs_columns(feature_db, tmp_path, caplog):
    from zucai.cli import main

    engine, _ = feature_db
    database = Path(engine.url.database)
    before = database.read_bytes()
    output = tmp_path / "cli.parquet"
    with caplog.at_level("INFO"):
        assert (
            main(
                [
                    "features",
                    "build",
                    "--db",
                    str(database),
                    "--match-id",
                    "jc:target",
                    "--as-of",
                    CUTOFF.isoformat(),
                    "--out",
                    str(output),
                ]
            )
            == 0
        )
    assert database.read_bytes() == before
    assert pd.read_parquet(output)["meta_match_id"].tolist() == ["jc:target"]
    assert "home_last5_goals_for_mean" in caplog.text
    assert "market_p_home" in caplog.text


def test_cli_wrong_database_does_not_create_a_new_database(tmp_path):
    from zucai.cli import main

    database = tmp_path / "missing.db"
    output = tmp_path / "features.parquet"
    assert main(["features", "build", "--db", str(database), "--out", str(output)]) == 2
    assert not database.exists() and not output.exists()


def test_frozen_t03_acceptance_has_intact_hashes_and_only_pre_cutoff_evidence():
    from zucai.features.build import MODEL_FEATURE_COLUMNS

    folder = Path(__file__).resolve().parents[1] / "reports/2026-11-02-t04-acceptance"
    manifest = json.loads((folder / "features.manifest.json").read_bytes())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
    frame = pd.read_parquet(folder / "features.parquet")
    metadata = json.loads((folder / "features.metadata.json").read_bytes())
    assert len(frame) == metadata["rows"] == 866
    assert metadata["model_feature_columns"] == list(MODEL_FEATURE_COLUMNS)
    assert set(frame.meta_match_id) == set(metadata["audit"])
    assert (frame.meta_history_n == 0).sum() == 841
    for target_id, audit in metadata["audit"].items():
        cutoff = pd.Timestamp(audit["cutoff_utc"])
        for games in audit["windows"].values():
            for historical in games:
                assert historical["match_id"] != target_id
                assert pd.Timestamp(historical["kickoff_utc"]) < cutoff
                assert pd.Timestamp(historical["available_utc"]) < cutoff
                assert historical["available_source"] == "captured"
        if audit["market"]["snapshot_utc"]:
            assert pd.Timestamp(audit["market"]["snapshot_utc"]) < cutoff
