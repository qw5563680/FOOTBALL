"""Offline alias review commands; real identity acquisition belongs to Actions."""

import logging


def add_aliases_parser(sub):
    parser = sub.add_parser("aliases", help="离线球队别名审查")
    commands = parser.add_subparsers(dest="aliases_command", required=True)
    proposals = commands.add_parser("proposals", help="生成所有者审批清单，不启用别名")
    proposals.add_argument("--provider", choices=("apif",), default="apif")
    proposals.add_argument(
        "--scope", choices=("first-batch", "cups-history"), default="first-batch"
    )
    proposals.add_argument("--leagues", default="3,848,292,119,188,244,169")
    proposals.add_argument(
        "--archive", default="../cloud-data", help="data/football-feeds 的本地检出目录"
    )
    proposals.add_argument("--out", required=True, help="新 Markdown 文件，不覆盖已有清单")
    evaluate = commands.add_parser("evaluate", help="只读评估双场一致方案，不改变生产规则")
    evaluate.add_argument("--provider", choices=("apif",), default="apif")
    evaluate.add_argument("--leagues", default="3,848,292,119,188,244,169")
    evaluate.add_argument("--archive", default="../cloud-data")
    evaluate.add_argument("--out", required=True, help="新的离线评估 JSON，不写正式别名")


def run_aliases(args):
    if args.aliases_command == "evaluate":
        from zucai.alias_evaluation import write_evaluation

        result = write_evaluation(args.archive, args.leagues, args.out)
        logging.getLogger(__name__).info(
            "离线评估 %s；额外候选 %d 支，生产规则未改变",
            result["status"],
            result["additional_teams"],
        )
        return
    if args.scope == "cups-history":
        from zucai.cups_history_proposals import write_cups_history

        count = write_cups_history(args.archive, args.out)
    else:
        from zucai.alias_proposals import write_proposals

        count = write_proposals(args.archive, args.leagues, args.out)
    logging.getLogger(__name__).info("已生成 %d 条审批清单条目：%s", count, args.out)
