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
        if "match_injuries" in inspect(connection).get_table_names():
            columns = {
                column["name"] for column in inspect(connection).get_columns("match_injuries")
            }
            if not set(STATS_COLUMNS["match_injuries"]) <= columns:
                raise ValueError(
                    "未部署的 T38 伤停草稿表不支持迁移；从既有 v1/v2 feeds 归档恢复后重新回放原件"
                )
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(connection, tables=[model.__table__ for model in STATS_MODELS])
