"""Run the independent T70 experiment against already-published forecasts."""

import argparse
import hashlib
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from zucai import web_research_archive as archive_api
from zucai import web_research_targets as targets
from zucai import web_research_validation as validation
from zucai.web_research_provider import DeepSeek, ProviderError

LOG = logging.getLogger(__name__)
PROMPT_VERSION = "v1"
PROMPT = Path(__file__).resolve().parents[2] / "data/prompts/web_research_v1.md"
RUN_TIME_BUDGET = timedelta(minutes=8)
IDENTITY_FIELDS = ("match_id", "jc_num", "league", "home", "away", "kickoff_utc", "kickoff_beijing")


def _utc_now():
    return datetime.now(UTC)


def _positive_decimal(value):
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("费用估算上限须为正数") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("费用估算上限须为正数")
    return value


class CostBudget:
    def __init__(self, limit):
        self.limit = _positive_decimal(limit)
        self.charged = Decimal(0)
        self.reserved = Decimal(0)
        self.requests = 0

    def request(self, reserve, call):
        if self.charged + self.reserved + reserve > self.limit:
            return None, "cost_limit", None
        self.requests += 1
        try:
            step = call()
        except ProviderError as exc:
            self.reserved += reserve
            error = {"code": exc.code, "http_status": exc.status_code}
            if exc.detail:
                error["detail"] = exc.detail
            return None, "provider_failed", error
        estimate = step.estimated_cost
        if estimate is None:
            self.reserved += reserve
            return step, "unknown_cost", None
        self.charged += estimate
        return step, "reserve_exceeded" if estimate > reserve else None, None


def _step_evidence(step):
    return {
        "usage": step.usage,
        "tool_calls": step.tool_calls,
        "estimated_cost": str(step.estimated_cost) if step.estimated_cost is not None else None,
        "billed_cost": str(step.billed_cost) if step.billed_cost is not None else None,
        "billed_currency": step.billed_currency,
    }


def _freeze_match(candidate, client, prompt, budget, clock, started):
    cutoff = started.isoformat()
    material = {**candidate["material"], "cutoff_utc": cutoff}
    kickoff = datetime.fromisoformat(candidate["kickoff_utc"])
    identity = {field: material[field] for field in IDENTITY_FIELDS}
    identity["date_beijing"] = material["kickoff_beijing"][:10]
    row = {
        key: value
        for key, value in candidate.items()
        if key not in {"material", "verification_recent", "identity_context"}
    }
    row.update(
        identity=identity, cutoff_utc=cutoff, structured=None, checks={}, status="partial", steps={}
    )
    artifacts = {"material": material}
    # The model researches its own market sources. Keep the local raw quotes for the check.
    provider_material = {
        key: value for key, value in material.items() if key not in {"odds", "odds_snapshot_utc"}
    }

    for phase in ("research", "structure"):
        at = clock()
        if at >= kickoff:
            return None, None, "kickoff_passed"
        if at >= started + RUN_TIME_BUDGET:
            if phase == "research":
                return None, None, "time_limit"
            row.update(completed_utc=at.isoformat(), reason="time_limit")
            return row, artifacts, "time_limit"
        if phase == "research":
            reserve = client.research_reserve(prompt, provider_material)
            step, stop, failure = budget.request(
                reserve, lambda: client.research(prompt, provider_material)
            )
        else:
            reserve = client.structure_reserve(artifacts["report_text"], provider_material)
            step, stop, failure = budget.request(
                reserve, lambda: client.structure(artifacts["report_text"], provider_material)
            )
        at = clock()
        if at >= kickoff:
            return None, None, "kickoff_passed"
        row["completed_utc"] = at.isoformat()
        if failure:
            row.update(status="failed", reason=stop, provider_error=failure)
            return row, artifacts, stop
        if step is None:
            if phase == "research":
                return None, None, stop
            row["reason"] = stop
            return row, artifacts, stop
        row["steps"][phase] = _step_evidence(step)
        artifacts[f"{phase}_response"] = step.raw
        if phase == "research":
            artifacts["report_text"] = step.text
            artifacts["citations"] = step.citations
        if stop:
            row["reason"] = stop
            return row, artifacts, stop
        if getattr(step, "finish_reason", "stop") != "stop":
            row.update(status="unparsed", reason="truncated_response")
            return row, artifacts, None

    verification = dict(material)
    for key in ("verification_recent", "identity_context"):
        if key in candidate:
            verification[key] = candidate[key]
    status, structured, checks = validation.parse(
        step.text, verification, artifacts["citations"], report_text=artifacts["report_text"]
    )
    row.update(status=status, structured=structured, checks=checks)
    return row, artifacts, None


def run(
    archive, run_id, *, max_cost, max_matches=5, forecast_run_id=None, clock=_utc_now, client=None
):
    if type(max_matches) is not int or max_matches <= 0:
        raise ValueError("场数上限须为正整数")
    budget = CostBudget(max_cost)
    archive = Path(archive).resolve()
    if (previous := archive_api.read_run(archive, run_id)) is not None:
        return previous

    started = clock()
    candidates = targets.select(archive, started, forecast_run_id=forecast_run_id)
    if candidates and client is None:
        client = DeepSeek()
    prompt_bytes = PROMPT.read_bytes()
    prompt = prompt_bytes.decode("utf-8")
    records, artifacts = [], {}
    stop = None
    for candidate in candidates[:max_matches]:
        row, source, stop = _freeze_match(candidate, client, prompt, budget, clock, started)
        if row is not None:
            records.append(row)
            artifacts[row["match_id"]] = source
        if stop:
            break
    if stop is None and len(candidates) > max_matches:
        stop = "match_limit"

    generated = clock()
    timely = [row for row in records if generated < datetime.fromisoformat(row["kickoff_utc"])]
    if len(timely) != len(records):
        stop = stop or "kickoff_passed"
    document = {
        "run_id": run_id,
        "status": "partial" if stop else "complete",
        "stop_reason": stop,
        "started_utc": started.isoformat(),
        "generated_utc": generated.isoformat(),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "provider": (client or DeepSeek).name,
        "model": (client or DeepSeek).model,
        "candidate_count": len(candidates),
        "max_matches": max_matches,
        "estimated_cost_limit": str(budget.limit),
        "estimated_spend": str(budget.charged),
        "budget_charged": str(budget.charged),
        "budget_reserved": str(budget.reserved),
        "provider_requests": budget.requests,
        "records": timely,
    }
    return archive_api.publish(
        archive,
        document,
        {row["match_id"]: artifacts[row["match_id"]] for row in timely},
        clock=clock,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--forecast-run-id")
    parser.add_argument("--max-matches", type=int, default=5)
    parser.add_argument("--max-cost", required=True)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("联网研究入口只在 GitHub Actions 中运行")
    try:
        result = run(
            args.archive,
            args.run_id,
            max_matches=args.max_matches,
            max_cost=args.max_cost,
            forecast_run_id=args.forecast_run_id,
        )
    except ProviderError as exc:
        LOG.error("联网研究线未启动（%s）", exc.code)
        return 1
    except (OSError, KeyError, ValueError, TypeError):
        LOG.error("联网研究线未冻结，配置或归档校验失败")
        return 1
    message = (
        f"联网研究线（实验）{result['status']} · 候选 {result['candidate_count']} · "
        f"请求 {result['provider_requests']} · 已计量费用估算 {result['estimated_spend']} · "
        f"停止原因 {result['stop_reason'] or '无'}\n"
    )
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(message)
    else:
        LOG.warning(message.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
