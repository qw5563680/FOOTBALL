# T01 · 数据库与抓取基础设施

## 目标
搭起后续所有任务依赖的地基：ORM 表、数据库初始化命令、抓取器基类和原始响应落盘/重放。

## 范围
1. `src/zucai/models.py`：按 `docs/ARCHITECTURE.md` 的「数据模型」定义 `teams`、`matches`、`odds`、
   `results`、`sfc_issues`、`sfc_issue_matches`、`predictions` 七张表（SQLAlchemy 2.0 风格）。
2. `src/zucai/db.py`：`get_engine(path=None)`，路径优先级：参数 > 环境变量 `ZUCAI_DB` > `data/zucai.db`；
   `init_db(engine)` 建表；`session_scope()` 上下文管理器。
3. `zucai db init` 子命令，重复执行幂等。
4. `src/zucai/sources/base.py`：
   - `make_session()`：带浏览器 UA、超时 15s、3 次指数退避重试的 `requests.Session`。
   - `RawStore`：`save(source, name, content, ext) -> Path` 落盘到 `data/raw/<source>/<YYYY-MM-DD>/`，
     `load(path)` 读回。
   - `Source` 抽象类：`name` 属性、`fetch_raw(**kw) -> Path`、`parse(raw: bytes) -> list[...]`。
   - `RateLimiter(min_interval_s)`。
5. `src/zucai/sources/teams.py`：`normalize_team(name) -> str` 与 `upsert_team(session, name) -> Team`，
   别名表先做最简单的全角/半角、空白、常见后缀（「队」「FC」）清理，未知队名追加到 `data/unmatched_teams.csv`。
6. 测试：建表幂等、`RawStore` 读写、`normalize_team` 若干用例、`make_session` 的重试（用 `responses` 库模拟）。

## 不做
- 不实现任何真实数据源的抓取。
- 不做 Alembic 迁移，现阶段表结构变化直接改模型并在文档注明。

## 验收标准
- `pip install -e ".[dev]" && zucai db init && pytest -q && ruff check src tests` 全部通过。
- `docs/ARCHITECTURE.md` 的数据模型与代码一致；如有调整，同步修改文档。
