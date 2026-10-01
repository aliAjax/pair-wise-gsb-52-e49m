"""应用入口：参数解析、依赖组装与HTTP服务生命周期。"""
import argparse
from pathlib import Path

from src.audit import AuditRecorder
from src.domain import Actor
from src.http_api import create_server
from src.repository import Repository
from src.rules import DomainRules, MonthlyRules
from src.service import Service


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "special-education.db"
DEFAULT_PORT = 8328


def build_service(db_path: str) -> Service:
    repository = Repository(db_path)
    audit = AuditRecorder(repository)
    return Service(repository, DomainRules(), audit, MonthlyRules())


def backfill_legacy(service: Service) -> None:
    """启动时为旧数据缺批次号的完成月份回填复核批次，幂等可重复执行。"""
    try:
        service.backfill_legacy_batches(Actor("system", "admin"))
    except Exception as exc:  # 回填失败不影响服务启动
        print("旧数据批次回填跳过: %s" % exc, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="特殊教育支持计划合规")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite数据库路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    service = build_service(args.db)
    backfill_legacy(service)
    server = create_server(args.host, args.port, service, BASE_DIR / "static")
    print("特殊教育支持计划合规 listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
