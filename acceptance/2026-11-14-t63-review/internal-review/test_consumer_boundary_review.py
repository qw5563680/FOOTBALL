"""Independent cutoffs and view isolation checks; fixture data remains synthetic."""
from copy import deepcopy
from datetime import timedelta
import pytest
from test_t63_stats_retention import engine as engine, add_match
from test_t63_stats_consumers import retained
from zucai.db import session_scope
from zucai.models import Odds
from zucai.features.stats import StatsFeatures
from zucai.predict.experiments.history import unified_history
from zucai.predict.experiments.command import attach_market
from zucai.sources.apif_stats import instant

@pytest.mark.parametrize('reverse',[False,True])
def test_market_rows_each_apply_own_proof_cutoff(engine,reverse):
    with session_scope(engine) as session:
        retained(session,'2026-11-10T09:00Z')
        for selection in 'hda':
            session.add(Odds(match_id='jc:1',pool='had',line=0,selection=selection,price=3,snapshot_utc='2026-11-10T08:00Z'))
        session.flush()
        builder=StatsFeatures(session,provider='apif')
        rows=[dict(match_id='apif:1',actual=0,feature_cutoff=c,variants={}) for c in ('2026-11-10T08:59Z','2026-11-10T09:01Z')]
        if reverse: rows.reverse()
        attach_market(session,rows,builder)
        result={r['feature_cutoff']:r['variants']['market'] is not None for r in rows}
        assert result=={'2026-11-10T08:59Z':False,'2026-11-10T09:01Z':True}

def test_aligned_cache_preserves_original_and_prior_view(engine):
    with session_scope(engine) as session:
        retained(session)
        add_match(session,'jc',key='jc:target',kickoff='2026-11-12T10:00Z')
        late,games=unified_history(session,provider='apif',as_of='2026-11-11T12:01Z')
    original=deepcopy(late._original.matches)
    late_games=deepcopy(games)
    early,early_games=late._view('2026-11-11T11:59Z')
    early_copy=deepcopy(early_games)
    assert len(early_games)==2 and len(games)==1
    for cutoff in ('2026-11-11T11:59Z','2026-11-11T12:01Z','2026-11-11T11:59Z'):
        late.build(late.matches['jc:target'],as_of=cutoff,history_source='aligned',research=False,stat_keys=('shots',),windows=(5,))
    assert late._original.matches==original
    assert games==late_games and early_games==early_copy
