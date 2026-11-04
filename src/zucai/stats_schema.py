"""T31 migration deliberately creates only the four attached-data tables."""

from sqlalchemy import inspect

from zucai.models import ApifFixtureIndex, Base, MatchEvent, MatchLineup, MatchStat

STATS_MODELS = (ApifFixtureIndex, MatchStat, MatchEvent, MatchLineup)
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
