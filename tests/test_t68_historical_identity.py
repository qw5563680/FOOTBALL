"""T68 第八节第 11 项：旧联赛历史赛季（2021-2024）的身份核验范围。"""

import pytest


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
