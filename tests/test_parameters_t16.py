"""Offline regression coverage for parameter safety and the A1 P2 policy."""

import copy
import json
import logging
from types import SimpleNamespace

import pandas as pd
import pytest

from zucai.predict import parameters, tuning


def _entry(**changes):
    entry = {
        "half_life_days": 365,
        "selection_as_of_utc": "2026-07-01T00:00Z",
        "generated_utc": "2026-07-01T00:00Z",
        "database_sha256": "a" * 64,
        "training_policy": {
            "history_scope": "cumulative",
            "strict_availability": False,
            "replay_known_after": None,
        },
        "sources": ["jc", "fd"],
        "unknown_team_prior": "weakest_quartile_half_shrinkage",
        "draw_calibration": False,
        "p2_ready": True,
        "p2_evidence": {
            "as_of_utc": "2026-07-01T00:00Z",
            "seasons": ["2425", "2526"],
        },
    }
    entry.update(changes)
    return entry


def _write(path, entry, league="英超"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "leagues": {league: entry}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _model(**changes):
    attrs = {
        "league": "英超",
        "as_of": "2026-07-02T00:00Z",
        "half_life_days": 365,
        "sources": ["fd", "jc"],
        "database_sha256": "b" * 64,
        "training_policy": {
            "history_scope": "cumulative",
            "strict_availability": False,
            "replay_known_after": None,
        },
        "availability_basis": "mixed",
        "replayed_training_share": 0,
        "unknown_team_prior": True,
        "prior_parameters": {"method": "weakest_quartile_half_shrinkage"},
        "draw_calibration": False,
    }
    attrs.update(changes)
    return SimpleNamespace(**attrs)


@pytest.mark.parametrize(
    "field",
    [
        "half_life_days",
        "selection_as_of_utc",
        "generated_utc",
        "database_sha256",
        "training_policy",
        "sources",
        "unknown_team_prior",
        "draw_calibration",
        "p2_evidence",
        "p2_evidence.as_of_utc",
        "p2_evidence.seasons",
    ],
)
def test_missing_parameter_fields_name_the_league_and_field(tmp_path, field):
    entry = _entry()
    parent, _, leaf = field.rpartition(".")
    del (entry[parent] if parent else entry)[leaf or field]
    config = _write(tmp_path / "params.json", entry, league="德甲")
    with pytest.raises(ValueError, match=rf"德甲.*{field}"):
        parameters.read_parameters(config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("half_life_days", True),
        ("half_life_days", 0),
        ("half_life_days", -1),
        ("half_life_days", float("inf")),
        ("half_life_days", float("nan")),
        ("half_life_days", "365"),
        ("selection_as_of_utc", None),
        ("selection_as_of_utc", 42),
        ("selection_as_of_utc", "2026-07-01"),
        ("selection_as_of_utc", "invalid"),
        ("generated_utc", []),
        ("generated_utc", "2026-07-01"),
        ("database_sha256", 123),
        ("database_sha256", ""),
        ("training_policy", []),
        ("training_policy", {"strict_availability": "false"}),
        ("sources", "fd,jc"),
        ("sources", ["fd", {}]),
        ("sources", []),
        ("sources", ["fd", "fd"]),
        ("unknown_team_prior", True),
        ("draw_calibration", "false"),
        ("p2_ready", "false"),
        ("p2_evidence", []),
        ("p2_evidence.as_of_utc", "2026-07-01"),
        ("p2_evidence.seasons", "2425,2526"),
        ("p2_evidence.seasons", [2425, 2526]),
    ],
)
def test_invalid_parameter_types_raise_contextual_value_error(tmp_path, field, value):
    entry = _entry()
    parent, _, leaf = field.rpartition(".")
    (entry[parent] if parent else entry)[leaf or field] = value
    config = _write(tmp_path / "params.json", entry, league="德甲")
    with pytest.raises(ValueError, match=rf"德甲.*{field}"):
        parameters.read_parameters(config)


@pytest.mark.parametrize("consumer", ["resolve", "readiness"])
def test_every_league_is_validated_before_consumption(tmp_path, consumer):
    config = _write(tmp_path / "params.json", _entry())
    document = json.loads(config.read_text(encoding="utf-8"))
    document["leagues"]["德甲"] = {"half_life_days": 90}
    config.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="德甲.*selection_as_of_utc"):
        if consumer == "resolve":
            parameters.resolve_parameters("英超", as_of="2026-11-01T00:00Z", params_file=config)
        else:
            parameters.model_readiness(_model(), as_of="2026-11-01T00:00Z", params_file=config)


@pytest.mark.parametrize("entry", [None, [], 1, "invalid"])
def test_non_object_league_entries_raise_value_error(tmp_path, entry):
    config = _write(tmp_path / "params.json", entry, league="德甲")
    with pytest.raises(ValueError, match="德甲"):
        parameters.read_parameters(config)


@pytest.mark.parametrize("document", [[], None, 1, {"schema_version": 1, "leagues": []}])
def test_non_object_parameter_documents_raise_value_error(tmp_path, document):
    config = tmp_path / "params.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        parameters.read_parameters(config)


@pytest.mark.parametrize(
    ("window", "field"),
    [
        (None, "evaluation_windows"),
        ({}, "evaluation_windows"),
        ([None], "evaluation_windows"),
        ([{}], "validation_season"),
        ([{"validation_season": "2526"}], "half_life_days"),
        ([{"validation_season": "2526", "half_life_days": 90}], "selection_as_of_utc"),
        (
            [{"validation_season": [], "half_life_days": 90, "selection_as_of_utc": "2025-07-01Z"}],
            "validation_season",
        ),
        (
            [
                {
                    "validation_season": "2526",
                    "half_life_days": False,
                    "selection_as_of_utc": "2025-07-01T00:00Z",
                }
            ],
            "half_life_days",
        ),
        (
            [
                {
                    "validation_season": "2526",
                    "half_life_days": 90,
                    "selection_as_of_utc": "2025-07-01",
                }
            ],
            "selection_as_of_utc",
        ),
        (
            [
                {
                    "validation_season": "2526",
                    "half_life_days": 90,
                    "selection_as_of_utc": "2025-07-01T00:00Z",
                    "draw_calibration": 1,
                }
            ],
            "draw_calibration",
        ),
    ],
)
def test_evaluation_window_fields_are_validated(tmp_path, window, field):
    config = _write(tmp_path / "params.json", _entry(evaluation_windows=window))
    with pytest.raises(ValueError, match=rf"英超.*{field}"):
        parameters.read_parameters(config)


def test_non_ready_minimal_entries_remain_supported(tmp_path):
    entry = {"half_life_days": 90, "selection_as_of_utc": "2026-07-01T00:00Z"}
    config = _write(tmp_path / "params.json", entry)
    result = parameters.resolve_parameters("英超", as_of="2026-11-01T00:00Z", params_file=config)
    assert result["half_life_days"] == 90
    assert result["params_path"] == str(config.resolve())
    assert parameters.read_parameters(config)[0]["leagues"]["英超"] == entry


def test_default_parameter_search_uses_environment_then_cwd_then_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = _write(tmp_path / "environment.json", _entry(half_life_days=180))
    local = _write(tmp_path / "data/model/params.json", _entry(half_life_days=270))
    fallback = _write(tmp_path / "source/data/model/params.json", _entry(half_life_days=365))
    monkeypatch.setenv("ZUCAI_PARAMS", str(env))
    monkeypatch.setattr(parameters, "PARAMS_PATH", fallback)
    for path, days in [(env, 180), (local, 270), (fallback, 365)]:
        result = parameters.resolve_parameters("英超", as_of="2026-11-01T00:00Z")
        assert result["half_life_days"] == days
        assert result["params_path"] == str(path.resolve())
        path.unlink()


def test_missing_parameters_warn_with_every_attempted_path(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    env = tmp_path / "environment.json"
    local = tmp_path / "data/model/params.json"
    fallback = tmp_path / "source/data/model/params.json"
    monkeypatch.setenv("ZUCAI_PARAMS", str(env))
    monkeypatch.setattr(parameters, "PARAMS_PATH", fallback)
    with caplog.at_level(logging.WARNING):
        result = parameters.resolve_parameters("英超", as_of="2026-11-01T00:00Z")
    assert result["half_life_days"] == 90
    assert result["source"] == "default"
    assert result["params_path"] == str(fallback.resolve())
    assert len(caplog.records) == 1
    assert all(str(path.resolve()) in caplog.text for path in (env, local, fallback))


def test_explicit_missing_parameter_path_does_not_fall_through(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "data/model/params.json", _entry())
    missing = tmp_path / "missing.json"
    with caplog.at_level(logging.WARNING):
        result = parameters.resolve_parameters(
            "英超", as_of="2026-11-01T00:00Z", params_file=missing
        )
    assert result["half_life_days"] == 90
    assert result["params_path"] == str(missing.resolve())
    assert str(missing.resolve()) in caplog.text


def test_a1_allows_another_training_database_and_discloses_both_hashes(tmp_path):
    config = _write(tmp_path / "params.json", _entry())
    result = parameters.model_readiness(_model(), as_of="2026-11-01T00:00Z", params_file=config)
    assert result["p2_ready"] is True
    assert result["training_database_sha256"] == "b" * 64
    assert result["evaluation_database_sha256"] == "a" * 64
    assert result["params_path"] == str(config.resolve())
    assert result["bet_allowed"] is False


def test_readiness_prefers_explicit_training_hash_over_legacy_hash(tmp_path):
    config = _write(tmp_path / "params.json", _entry())
    result = parameters.model_readiness(
        _model(training_database_sha256="c" * 64),
        as_of="2026-11-01T00:00Z",
        params_file=config,
    )
    assert result["training_database_sha256"] == "c" * 64


@pytest.mark.parametrize(
    "changes",
    [
        {"sources": ["fd"]},
        {"training_policy": {"history_scope": "rolling"}},
        {"half_life_days": 90},
        {"unknown_team_prior": False},
        {"prior_parameters": {"method": "another_method"}},
        {"draw_calibration": True},
        {"as_of": "2026-06-30T00:00Z"},
        {"replayed_training_share": 0.1},
        {"availability_basis": "replayed"},
    ],
)
def test_a1_keeps_model_policy_checks(tmp_path, changes):
    config = _write(tmp_path / "params.json", _entry())
    result = parameters.model_readiness(
        _model(**changes), as_of="2026-11-01T00:00Z", params_file=config
    )
    assert result["p2_ready"] is False


@pytest.mark.parametrize("as_of", ["2026-06-30T00:00Z", "2027-08-01T00:00Z"])
def test_a1_keeps_evidence_availability_and_recency(tmp_path, as_of):
    config = _write(tmp_path / "params.json", _entry())
    assert not parameters.model_readiness(_model(), as_of=as_of, params_file=config)["p2_ready"]


@pytest.mark.parametrize("calibrate", [True, False])
@pytest.mark.parametrize("output_kind", ["implicit", "absolute", "relative", "symlink"])
def test_calibration_diagnostic_refuses_default_path_before_database_read(
    tmp_path, monkeypatch, calibrate, output_kind
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZUCAI_PARAMS", raising=False)
    default = _write(tmp_path / "data/model/params.json", _entry())
    monkeypatch.setattr(parameters, "PARAMS_PATH", default)
    before = default.read_bytes()
    paths = {"implicit": None, "absolute": default, "relative": "data/model/../model/params.json"}
    alias = tmp_path / "alias.json"
    alias.symlink_to(default)
    paths["symlink"] = alias
    with pytest.raises(ValueError, match="诊断模式不能写入默认参数文件，请指定 --params-file"):
        tuning.tune(
            db=tmp_path / "missing.db",
            league="英超",
            params_file=paths[output_kind],
            draw_calibration=calibrate,
        )
    assert default.read_bytes() == before


@pytest.mark.parametrize("calibrate", [None, True, False])
def test_tune_normal_default_and_explicit_diagnostics_write_parameters(
    tmp_path, monkeypatch, calibrate
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZUCAI_PARAMS", raising=False)
    default = _write(tmp_path / "data/model/params.json", _entry())
    monkeypatch.setattr(parameters, "PARAMS_PATH", default)
    before = default.read_bytes()
    output = None if calibrate is None else tmp_path / "diagnostic.json"
    history = pd.DataFrame({"league": ["英超"]})
    history.attrs["sources"] = ["fd", "jc"]
    monkeypatch.setattr(
        "zucai.predict.command.load_corpus", lambda *args: (history, {}, {}, "d" * 64)
    )
    selected = {
        "half_life_days": 180,
        "selection_basis": "fixture selection",
        "calibration_policy": "fixture policy",
        "windows": [
            {
                "selection_season": "2425",
                "validation_season": "2526",
                "selection_as_of_utc": "2025-07-01T00:00Z",
                "half_life_days": 180,
                "draw_calibration": calibrate is True,
                "selection_draw_bias": 0.04,
            }
        ],
    }
    monkeypatch.setattr(tuning, "rolling_origin", lambda *args, **kwargs: copy.deepcopy(selected))
    monkeypatch.setattr(
        tuning, "evaluate", lambda *args, **kwargs: {"fold": "weekly", "seasons": {}}
    )
    result = tuning.tune(league="英超", params_file=output, draw_calibration=calibrate)
    saved, _ = parameters.read_parameters(output)
    assert saved["leagues"]["英超"]["half_life_days"] == 180
    assert saved["leagues"]["英超"]["draw_calibration"] is (calibrate is True)
    assert result["parameters"] == saved["leagues"]["英超"]
    if output is not None:
        assert default.read_bytes() == before


def test_rolling_origin_rejects_transition_as_selection_season():
    history = pd.DataFrame({"league": ["日职"]})
    with pytest.raises(ValueError, match="2026T.*完整"):
        tuning.rolling_origin(history, {}, grid=[90], seasons=["2026T"], as_of="2027-01-01T00:00Z")


def test_rolling_origin_defaults_skip_jleague_transition(monkeypatch):
    history = pd.DataFrame(
        {
            "league": ["日职"],
            "kickoff_utc": pd.to_datetime(["2021-01-01T00:00Z"]),
            "available_utc": pd.to_datetime(["2021-01-01T03:00Z"]),
        }
    )
    monkeypatch.setattr(
        tuning,
        "evaluate",
        lambda *args, **kwargs: {
            "model": {"n": 1, "log_loss": 1.0, "draw_bias": 0.0},
            "in_period": 1,
            "unavailable_labels": 0,
            "skipped": {},
            "folds": [],
        },
    )
    result = tuning.rolling_origin(history, {}, grid=[90], as_of="2027-01-01T00:00Z")
    assert [window["selection_season"] for window in result["windows"]] == ["2023", "2024", "2025"]
    assert result["windows"][-1]["validation_season"] == "2627"


@pytest.mark.parametrize(
    "as_of",
    ["2026-01-02T00:00Z", "2026-07-31T23:59:59.999999Z", "2026-08-01T00:00Z"],
)
def test_jleague_selection_requires_the_next_eligible_validation_season_to_start(
    monkeypatch, as_of
):
    history = pd.DataFrame(
        {
            "league": ["日职"],
            "kickoff_utc": pd.to_datetime(["2025-01-01T00:00Z"]),
            "available_utc": pd.to_datetime(["2025-01-01T03:00Z"]),
        }
    )
    monkeypatch.setattr(
        tuning,
        "evaluate",
        lambda *args, **kwargs: {
            "model": {"n": 1, "log_loss": 1.0, "draw_bias": 0.0},
            "in_period": 1,
            "unavailable_labels": 0,
            "skipped": {},
            "folds": [],
        },
    )
    with pytest.raises(ValueError, match="2627.*验证.*尚未开始"):
        tuning.rolling_origin(history, {}, grid=[90], seasons=["2025"], as_of=as_of)


@pytest.mark.parametrize("as_of", ["2026-08-01T00:00:00.000001Z", "2027-08-01T00:00Z"])
def test_jleague_2025_parameters_validate_2627_without_using_transition_for_selection(
    monkeypatch, as_of
):
    kickoff = pd.to_datetime(
        ["2025-12-20T00:00Z", "2026-01-01T00:00Z", "2026-03-01T00:00Z", "2026-08-15T00:00Z"]
    )
    history = pd.DataFrame(
        {
            "league": ["日职"] * 4,
            "kickoff_utc": kickoff,
            "available_utc": kickoff + pd.Timedelta(hours=3),
        }
    )
    evaluations = []

    def evaluate(corpus, market, **options):
        evaluations.append((corpus.copy(), options))
        return {
            "model": {"n": 1, "log_loss": 1.0, "draw_bias": 0.0},
            "in_period": 1,
            "unavailable_labels": 0,
            "skipped": {},
            "folds": [],
        }

    monkeypatch.setattr(tuning, "evaluate", evaluate)
    result = tuning.rolling_origin(history, {}, grid=[90, 180], seasons=["2025"], as_of=as_of)
    window = result["windows"][0]
    assert window["validation_season"] == "2627"
    assert window["selection_as_of_utc"] == "2026-01-01T00:00:00+00:00"
    assert window["half_life_days"] == 90
    for selection_corpus, options in evaluations[:-1]:
        assert list(selection_corpus.kickoff_utc) == [pd.Timestamp("2025-12-20T00:00Z")]
        assert options["as_of"] == pd.Timestamp("2026-01-01T00:00Z")
        assert options["seasons"] == ["2025"]
    validation_options = evaluations[-1][1]
    assert validation_options["seasons"] == ["2627"]
    assert validation_options["since"] == pd.Timestamp("2026-08-01T00:00Z")
    assert validation_options["as_of"] == pd.Timestamp(as_of)
