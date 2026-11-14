"""The offline CLI consumes archived response bytes without opening a transport."""

import json
from datetime import datetime

import pytest
import responses

from test_t65_jc_catalogue import CAPTURED, REPLAYED, archive_at, files_sha256
from zucai import catalogue_replay, cloud_jc_catalogue


class ReplayClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return REPLAYED.astimezone(tz)


@responses.activate
def test_cli_writes_new_run_with_original_capture_and_no_latest_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_jc_catalogue, "datetime", ReplayClock)
    source = archive_at(tmp_path / "source")
    before = files_sha256(source)
    output = tmp_path / "output"
    report = catalogue_replay.main(
        ["--source-dir", str(source), "--out-dir", str(output), "--run-id", "cli-replay"]
    )
    target = output / "state/catalogue/cli-replay"
    assert report["status"] == "complete" and report["requests"] == 0
    assert report["replayed_pages"] == 365
    assert report["captured_utc"] == CAPTURED
    assert report["replayed_utc"] == REPLAYED.isoformat()
    assert json.loads((target / "jc.json").read_bytes()) == report
    assert len(json.loads((target / "jc-leagues.json").read_bytes())["leagues"]) == 113
    assert not (output / "state/catalogue/latest.json").exists()
    assert files_sha256(source) == before
    assert not responses.calls


def test_cli_rejects_path_traversal_before_writing(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        catalogue_replay.main(
            [
                "--source-dir",
                str(tmp_path / "source"),
                "--out-dir",
                str(tmp_path / "output"),
                "--run-id",
                "../../overwrite",
            ]
        )
    assert not list(tmp_path.iterdir())
