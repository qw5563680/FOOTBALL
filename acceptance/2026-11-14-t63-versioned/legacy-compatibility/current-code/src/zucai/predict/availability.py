"""In-memory availability assumptions for development-stage historical replay."""

from datetime import timedelta

import pandas as pd

from zucai.predict.base import availability_sources


def replay_history(history: pd.DataFrame, delay: timedelta) -> pd.DataFrame:
    """Copy a history, advancing only late observed labels; never infer missing times."""
    delay = pd.Timedelta(delay)
    if pd.isna(delay) or delay <= pd.Timedelta(0):
        raise ValueError("重放延时必须是正数")
    replayed = history.copy(deep=True)
    provenance = availability_sources(history)
    mask = provenance.isin(("published", "captured")) & (
        history.available_utc - history.kickoff_utc > delay
    )
    replayed.loc[mask, "available_utc"] = history.loc[mask, "kickoff_utc"] + delay
    replayed.loc[mask, "available_source"] = "replayed"
    replayed.attrs["replay_delay"] = str(delay)
    return replayed
