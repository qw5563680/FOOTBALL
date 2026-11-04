"""Additive migration for technical observations, including T38 injuries."""

from sqlalchemy import inspect

from zucai.models import ApifFixtureIndex, Base, MatchEvent, MatchInjury, MatchLineup, MatchStat

STATS_MODELS = (ApifFixtureIndex, MatchStat, MatchEvent, MatchLineup, MatchInjury)
STATS_COLUMNS = {
    model.__tablename__: tuple(column.name for column in model.__table__.columns)
    for model in STATS_MODELS
}


def migrate_stats(engine):
    with engine.begin() as connection:
        if not {"teams", "matches"} <= set(inspect(connection).get_table_names()):
            raise ValueError("技术数据需要已有球队与比赛库")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(connection, tables=[model.__table__ for model in STATS_MODELS])
