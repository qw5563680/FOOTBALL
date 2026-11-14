"""A local batch cap is distinct from transport, quota and validation failures."""


class DeferredRequestBudget(ValueError):
    reason_code = "deferred_request_budget"


def defer_scope(report, item, exc, *, requests_started):
    """Only a wholly unrequested scope is deferred; interrupted pages/retries fail."""
    if not isinstance(exc, DeferredRequestBudget) or requests_started:
        return False
    item.update(status="deferred", reason=exc.reason_code)
    report["status"] = "partial"
    report.setdefault("deferred_scopes", []).append(
        dict(scope=item["scope"], reason=exc.reason_code)
    )
    return True
