import csv
import hashlib
import io
from pathlib import Path

import pytest
import responses

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/t08"


def config(code="E0", **overrides):
    return {
        code: dict(
            div_code=code,
            league="英超" if code == "E0" else "日职",
            time_basis="Europe/London",
            status="verified",
            verified_utc="2026-11-01T00:00:00Z",
            verified_by=2,
            **overrides,
        )
    }


def csv_bytes(**changes):
    row = dict(
        Div="E0",
        Date="21/08/2026",
        Time="20:00",
        HomeTeam="Arsenal",
        AwayTeam="Coventry",
        FTHG="2",
        FTAG="1",
        FTR="H",
        HTHG="1",
        HTAG="0",
        HTR="H",
        PSCH="2.0",
        PSCD="3.0",
        PSCA="4.0",
        B365CH="1.5",
        B365CD="",
        B365CA="6",
        AvgCH="2.2",
        AvgCD="3.2",
        AvgCA="4.2",
        MaxCH="2.5",
        MaxCD="3.5",
        MaxCA="4.5",
        B365H="99",
        B365D="99",
        B365A="99",
        HxG="99",
        AxG="99",
    )
    row.update(changes)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=row)
    writer.writeheader()
    writer.writerow(row)
    return out.getvalue().encode()


def feed(code="E0", **kwargs):
    from zucai.sources.football_data import FootballDataCoUk

    return FootballDataCoUk(leagues=config(code), div_code=code, season="2627", **kwargs)


def test_actual_main_fixture_and_blank_time_are_auditable():
    source = feed()
    raw = (FIXTURES / "main.csv").read_bytes()
    games = source.parse(raw)
    assert len(games) == 29
    assert games[0].home_raw == "Arsenal" and games[0].away_raw == "Coventry"
    assert games[0].kickoff_utc.isoformat() == "2026-08-21T19:00:00+00:00"
    assert games[0].div_code == "E0" and games[0].season == "2627"
    assert source.rejected[0]["row_number"] == 31
    assert "Time" in source.rejected[0]["reason"]
    assert source.rejected[0]["raw_sha256"] == hashlib.sha256(raw).hexdigest()


def test_actual_combined_fixture_retains_split_season():
    source = feed("JPN")
    games = source.parse((FIXTURES / "combined.csv").read_bytes())
    assert len(games) == 29 and len(source.rejected) == 1
    assert {g.season for g in games} == {"2026/2027"}
    assert all(g.div_code == "JPN" and g.half is None for g in games)


def test_only_complete_finite_closing_groups_are_parsed():
    source = feed()
    game = source.parse(csv_bytes())[0]
    assert game.full == (2, 1) and game.half == (1, 0)
    assert game.closing == {"PS": (2, 3, 4), "Avg": (2.2, 3.2, 4.2), "Max": (2.5, 3.5, 4.5)}
    assert not hasattr(game, "HxG")
    assert "PS" not in source.parse(csv_bytes(PSCH="nan"))[0].closing


@pytest.mark.parametrize(
    "changes",
    [
        {"Time": ""},
        {"FTHG": ""},
        {"FTHG": "-1"},
        {"FTHG": "1.5"},
        {"HTHG": "3"},
        {"FTR": "A"},
        {"Time": "25:00"},
        {"Date": "29/03/2026", "Time": "01:30"},
        {"Date": "25/10/2026", "Time": "01:30"},
    ],
)
def test_bad_rows_and_dst_ambiguity_are_rejected(changes):
    source = feed()
    assert source.parse(csv_bytes(**changes)) == []
    assert len(source.rejected) == 1


def test_unverified_timezone_is_rejected_before_network():
    from zucai.sources.football_data import FootballDataCoUk

    leagues = config()
    leagues["E0"]["status"] = "unverified"
    source = FootballDataCoUk(leagues=leagues)
    with pytest.raises(ValueError, match="时区"):
        source.fetch_raw(div="E0", season="2627")


def test_wrong_division_or_missing_identity_header_is_not_a_valid_file():
    with pytest.raises(ValueError):
        feed().parse(csv_bytes(Div="I1"))
    with pytest.raises(ValueError):
        feed().parse(b"Date,Time\n21/08/2026,20:00\n")


def test_download_archives_original_bytes_and_sha(tmp_path):
    from zucai.sources.base import RawStore

    raw = csv_bytes()
    url = "https://www.football-data.co.uk/mmz4281/2627/E0.csv"
    source = feed(store=RawStore(tmp_path))
    with responses.RequestsMock() as mocked:
        mocked.get(url, body=raw, status=200)
        path = source.fetch_raw(div="E0", season="2627")
    assert path.read_bytes() == raw
    meta = source.store.load_metadata(path)
    assert meta["url"] == url and meta["status_code"] == 200
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()


def test_redirect_hops_are_individually_rate_limited_and_archived(tmp_path):
    from zucai.sources.base import RawStore

    events = []
    source = feed(store=RawStore(tmp_path))
    source.limiter = type("Limiter", (), {"wait": lambda self: events.append("wait")})()
    first = "https://www.football-data.co.uk/mmz4281/2627/E0.csv"
    final = first.replace("//www.", "//")

    def redirect(request):
        events.append("get")
        return 301, {"Location": final}, b""

    def complete(request):
        events.append("get")
        return 200, {}, csv_bytes()

    with responses.RequestsMock() as mocked:
        mocked.add_callback(responses.GET, first, callback=redirect)
        mocked.add_callback(responses.GET, final, callback=complete)
        path = source.fetch_raw(div="E0", season="2627")
    assert events == ["wait", "get", "wait", "get"]
    assert source.store.load_metadata(path)["url"] == final
    assert len(list(tmp_path.rglob("*.meta.json"))) == 2


def test_redirect_outside_source_is_refused(tmp_path):
    from zucai.sources.base import RawStore

    source = feed(store=RawStore(tmp_path))
    with responses.RequestsMock() as mocked:
        mocked.get(
            "https://www.football-data.co.uk/mmz4281/2627/E0.csv",
            status=302,
            headers={"Location": "https://example.org/other.csv"},
        )
        with pytest.raises(ValueError, match="重定向"):
            source.fetch_raw(div="E0", season="2627")
        assert len(mocked.calls) == 1


def test_conflicting_duplicate_rows_are_both_excluded():
    first = csv_bytes().decode().splitlines()
    second = csv_bytes(FTHG="3").decode().splitlines()
    source = feed()
    assert source.parse(("\n".join(first + second[1:]) + "\n").encode()) == []
    assert len(source.rejected) == 2


def test_equivalent_time_bases_must_still_agree_on_imported_row():
    source = feed()
    source.leagues["E0"]["equivalent_time_bases_in_observed_sample"] = [
        "Europe/London",
        "Europe/Madrid",
    ]
    assert source.parse(csv_bytes()) == []
    assert "等价" in source.rejected[0]["reason"]


def test_nonconsecutive_season_is_rejected_on_replay_too():
    source = feed()
    source.season = "2427"
    with pytest.raises(ValueError, match="连续"):
        source.parse(csv_bytes())
