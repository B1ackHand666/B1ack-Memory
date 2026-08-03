from __future__ import annotations

import argparse
import json
import webbrowser
from typing import Any

from .plugin import get_service


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(prog="b1ack-memory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="显示健康状态")
    search = commands.add_parser("search", help="检索记忆")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    remember = commands.add_parser("remember", help="手工保存记忆")
    remember.add_argument("content")
    remember.add_argument("--kind", default="fact")
    dream = commands.add_parser("dream", help="立即运行 Dream")
    dream.add_argument("--dry-run", action="store_true")
    ui = commands.add_parser("ui", help="启动本地 WebUI")
    ui.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"])
    ui.add_argument("--port", type=int, default=7788)
    ui.add_argument("--no-open", action="store_true")
    commands.add_parser("backup", help="创建数据库备份")
    maintenance = commands.add_parser("maintenance", help="执行维护")
    maintenance.add_argument("--vacuum", action="store_true")
    maintenance.add_argument("--cleanup", action="store_true")
    return parser


def run(args: argparse.Namespace) -> int:
    service = get_service(start_background=args.command == "ui")
    if args.command == "status":
        _print(service.status())
    elif args.command == "search":
        _print([item.to_dict() for item in service.search(args.query, limit=args.limit)])
    elif args.command == "remember":
        _print(service.remember(args.content, kind=args.kind))
    elif args.command == "dream":
        _print(service.run_dream(dry_run=args.dry_run))
    elif args.command == "backup":
        _print({"name": service.create_backup().name})
    elif args.command == "maintenance":
        _print(service.maintenance(vacuum=args.vacuum, cleanup=args.cleanup))
    elif args.command == "ui":
        import uvicorn

        from .web import create_app

        url = f"http://{args.host}:{args.port}/api/ui/"
        if not args.no_open:
            webbrowser.open(url)
        uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="info")
    return 0


def main() -> int:
    return run(build_parser().parse_args())


def register_cli(subparsers: Any) -> None:
    build_parser(subparsers)
    subparsers.set_defaults(func=run)


if __name__ == "__main__":
    raise SystemExit(main())
