from math import prod

from zucai.report.sfc import pick_nine_lines


def report():
    rows = [
        dict(
            seq=n,
            home="H",
            away="A",
            kickoff_utc="2026-11-08T12:00:00Z",
            probabilities={"h": 0.15, "d": 0.85 - n / 50, "a": n / 50},
        )
        for n in range(1, 15)
    ]
    return dict(
        rows=rows,
        sale_state="published_before_deadline",
        mode="prematch",
        published_utc="2026-11-08T10:00:00Z",
    )


def test_single_ticket_in_original_order_uses_only_timely_verified_matches():
    r = report()
    r["rows"][0]["probabilities"] = None
    r["rows"][1]["kickoff_basis"] = "placeholder"
    r["rows"][2]["kickoff_utc"] = r["published_utc"]
    r["rows"].reverse()
    lines = pick_nine_lines(r)
    picks = [line for line in lines if "1 平" in line]
    assert [int(line.split("|")[1]) for line in picks] == list(range(4, 13))
    assert f"{prod(0.85 - n / 50 for n in range(4, 13)):.2%}" in "".join(lines)


def test_no_ticket_with_eight_or_unconfirmed_publication():
    r = report()
    for row in r["rows"][:6]:
        row["probabilities"] = None
    assert "只有 8 场" in "".join(pick_nine_lines(r))
    r = report()
    r["sale_state"] = "deadline_unconfirmed"
    assert "主胜" not in "".join(pick_nine_lines(r))
