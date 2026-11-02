"""Freeze previously available prospective evidence into new reports only."""

import math

from zucai.cloud_feeds import digest
from zucai.scoreboard.score import instant, summarize_scores


def read_evidence(archive, as_of):
    from zucai.scoreboard.command import read_state

    scored, _, summary, _ = read_state(archive)
    if summary is None or any(
        instant(summary[k]) > instant(as_of) for k in ("generated_utc", "as_of_utc")
    ):
        return None
    probability = summarize_scores(
        scored, summary["as_of_utc"], active_keys=summary["audit"]["active_keys"]
    )
    return {
        "summary_sha256": digest(archive / "scoreboard/summary.json"),
        "as_of_utc": summary["as_of_utc"],
        "generated_utc": summary["generated_utc"],
        "leagues": {
            league: {
                "n": values["n"],
                "model_ll": values["model"]["log_loss"],
                "uniform_ll": values["uniform"]["log_loss"],
            }
            for league, values in probability["leagues"].items()
        },
    }


def annotate_batch(batch, evidence):
    if evidence and (
        batch["mode"] != "prematch"
        or any(
            instant(evidence[k]) > instant(batch["as_of_utc"])
            for k in ("generated_utc", "as_of_utc")
        )
    ):
        evidence = None
    batch["prospective_evidence"] = evidence
    for row in batch["predictions"]:
        if row["status"] != "predicted":
            continue
        entry = (evidence or {}).get("leagues", {}).get(row["league"], {})
        n, model, uniform = entry.get("n", 0), entry.get("model_ll"), entry.get("uniform_ll")
        valid = type(n) is int and n >= 0
        row["prospective_n"] = n if valid else 0
        row["prospective_verified"] = bool(
            valid
            and n >= 200
            and all(type(v) in (float, int) and math.isfinite(v) for v in (model, uniform))
            and model <= uniform
        )
