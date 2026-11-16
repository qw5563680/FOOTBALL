"""T68 第八节第 11 项：旧联赛历史赛季（2021-2024）的身份核验范围。"""

import csv
import sqlite3

import pytest
import responses

from test_cloud_apis import environment as environment
from test_cloud_identity import fixture_doc
from test_cloud_identity import identity_database as identity_database
from test_identity_probe import actions as actions


def test_historical_league_and_season_scopes_are_accepted():
    from zucai.cloud_identity import identity_scopes

    selected, years = identity_scopes("39", "2021")
    assert years == [2021]
    assert "39" in selected
    selected, years = identity_scopes("39,78", "2021,2024")
    assert years == [2021, 2024]
    assert {"39", "78"} <= set(selected)


def test_seasons_outside_2021_2026_are_rejected():
    from zucai.cloud_identity import identity_scopes

    with pytest.raises(ValueError, match="2021 至 2026"):
        identity_scopes("39", "2020")
    with pytest.raises(ValueError, match="2021 至 2026"):
        identity_scopes("39", "2027")


def test_historical_leagues_only_when_explicitly_requested():
    from zucai.cloud_identity_probe import identity_codes

    default = identity_codes()
    # 未配置接口赛季探测的联赛（英冠 40 等）只在显式给出且已有配置时允许；默认范围不变。
    assert "40" not in default
    explicit = identity_codes("39,78")
    assert set(explicit) == {"39", "78"}


def test_unknown_league_is_rejected():
    from zucai.cloud_identity_probe import identity_codes

    with pytest.raises(ValueError):
        identity_codes("999999")


def test_old_thirteen_column_audit_is_upgraded_and_historical_season_fills_six_years(
    identity_database, actions
):
    from test_identity_probe import FIXTURE, QUOTA
    from zucai.cloud_identity import FIELDS, collect_identity

    database = identity_database
    league = FIXTURE["leagues"][0]  # 英超 39
    audit = database.parent / "state/audit/apif_identity.csv"
    audit.parent.mkdir(parents=True, exist_ok=True)
    legacy = ",".join(
        (
            "run_id",
            "league_id",
            "league",
            "team_id",
            "name",
            "country",
            "canonical",
            "status",
            "appearances_2025",
            "appearances_2026",
            "jc_references",
            "reason",
            "evidence",
        )
    )
    audit.write_text(
        legacy
        + "\n"
        + 'old-run,39,英超,9101,Synthetic Alpha,England,合成甲,auto_verified,5,5,"[]",,\n'
        + 'old-run,39,英超,9102,Synthetic Beta,England,合成乙,auto_verified,4,4,"[]",,\n',
        encoding="utf-8",
    )
    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES(?,?,?,3,4,?,'scheduled')",
            (
                f"jc:{league['fd_code']}:2021",
                "jc",
                league["league"],
                "2021" + FIXTURE["kickoff_suffix"],
            ),
        )
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures?league=39&season=2021",
            json=fixture_doc(league, 2021),
            headers=QUOTA["paid_headers"],
        )
        report = collect_identity(database, "hist-1", codes="39", seasons="2021")
    assert report["status"] == "complete" and report["requests"] == 1
    assert report["auto_verified"] == 2
    with audit.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        assert next(reader) == list(FIELDS)
        rows = list(reader)
    assert all(len(row) == len(FIELDS) for row in rows)
    assert len(rows) == 4  # 2 条旧行保留 + 2 条新行
    records = list(csv.DictReader(audit.open(encoding="utf-8", newline="")))
    old_rows = [row for row in records if row["run_id"] == "old-run"]
    new_rows = [row for row in records if row["run_id"] == "hist-1"]
    assert len(old_rows) == 2 and len(new_rows) == 2
    assert old_rows[0]["team_id"] == "9101" and old_rows[0]["canonical"] == "合成甲"
    assert old_rows[0]["appearances_2025"] == "5"
    assert all(old_rows[index][column] == "" for index in (0, 1) for column in FIELDS[8:12])
    assert all(row["appearances_2021"] == "1" for row in new_rows)
    assert all(
        row["appearances_2025"] == "0" and row["appearances_2026"] == "0" for row in new_rows
    )
