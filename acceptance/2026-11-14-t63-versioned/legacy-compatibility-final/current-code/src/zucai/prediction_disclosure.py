"""Read-only disclosure of default-strength evidence in frozen predictions."""


def _boolean(value):
    return value if type(value) is bool else None


def _detail(used, home, away, method, evidence):
    if home is True or away is True:
        if used is False:
            evidence = "conflicting_prior_flag"
        used = True
    elif used is True and home is False and away is False:
        home = away = None
        evidence = "conflicting_side_evidence"
    if used is False:
        home = away = False
    return dict(
        used=used,
        home=home,
        away=away,
        method=method if isinstance(method, str) and method.strip() and used is True else None,
        evidence=evidence,
    )


def _from_metadata(metadata):
    used = _boolean(metadata.get("unknown_team_prior"))
    method = metadata.get("prior_method")
    if "home_unknown_team" in metadata or "away_unknown_team" in metadata:
        home, away = (_boolean(metadata.get(f"{side}_unknown_team")) for side in ("home", "away"))
        # Even incomplete new side evidence cannot erase an affirmative signal.
        if home is None or away is None:
            used = True if home is True or away is True else used
            home = away = None
        return _detail(used, home, away, method, "explicit_sides")
    leagues = [metadata.get(f"{side}_training_leagues") for side in ("home", "away")]
    if metadata.get("pooled") is True and all(
        isinstance(value, dict)
        and all(isinstance(k, str) and type(n) is int and n > 0 for k, n in value.items())
        for value in leagues
    ):
        return _detail(used, not leagues[0], not leagues[1], method, "pooled_training_leagues")
    return _detail(used, None, None, method, "flag_only" if used is not None else "not_recorded")


def prior_disclosure(row):
    """Return tri-state use/sides; absent old metadata is not evidence of known teams."""
    metadata = row.get("metadata")
    original = _from_metadata(metadata if isinstance(metadata, dict) else {})
    propagated = row.get("prior_disclosure")
    if not isinstance(propagated, dict):
        return original
    derived = _detail(
        _boolean(propagated.get("used")),
        _boolean(propagated.get("home")),
        _boolean(propagated.get("away")),
        propagated.get("method"),
        propagated.get("evidence") or "propagated",
    )
    # An affirmative original marker always wins over a conflicting derived flag.
    # A derived affirmative marker also stays excluded if raw metadata is absent
    # or contradictory; copying evidence must never reopen candidate eligibility.
    if original["used"] is True or derived["used"] is not True and original["used"] is not None:
        return original
    return derived


def uses_unknown_team_prior(row):
    return prior_disclosure(row)["used"] is True


def prior_label(row):
    """Compact inline label; callers escape it for their output format."""
    detail = prior_disclosure(row)
    if detail["used"] is not True:
        return ""
    sides = [
        f"{label} {row.get(side) or '球队名未记录'}"
        for side, label in (("home", "主队"), ("away", "客队"))
        if detail[side] is True
    ]
    missing = "、".join(sides) if sides else "缺失方未记录"
    return f"默认实力：{missing}"


def prior_export_label(row):
    """Keep the existing CSV disclosure value and fields unchanged."""
    label = prior_label(row)
    if not label:
        return ""
    detail = prior_disclosure(row)
    method = f"先验方法：{detail['method']}" if detail["method"] else "先验方法未记录"
    return (
        f"使用默认实力（{label.removeprefix('默认实力：')}；{method}）；"
        "仅保留研究概率，不进入主候选"
    )


def prior_footnote(rows):
    """One explanation for the affected rows in a table, without inferring evidence."""
    affected = [row for row in rows if uses_unknown_team_prior(row)]
    if not affected:
        return ""
    methods = sorted({prior_disclosure(row)["method"] or "未记录" for row in affected})
    description = "、".join(methods)
    if len(methods) > 1:
        description = "；".join(
            f"{row.get('match_id') or prior_label(row)}="
            f"{prior_disclosure(row)['method'] or '未记录'}"
            for row in affected
        )
    return (
        "默认实力说明：先验方法："
        + description
        + "；仅保留研究概率，不进入主候选、任九模型单选或最高项排序。"
    )
