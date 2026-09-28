"""命令行入口。

启动服务（启动时自动执行崩溃恢复）::

    python3 -m src.city_tourism_capacity.serve --db tourism.db --port 8080

播种一份演示数据::

    python3 -m src.city_tourism_capacity.seed --db tourism.db
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from .app import build_server
from .security import Actor, DISPATCHER
from .service import Service
from .storage import Storage


def serve(db_path: str, host: str, port: int) -> None:
    storage = Storage(db_path)
    service = Service(storage)
    result = service.recover()
    print(f"[startup] 恢复完成：{result}")
    server = build_server(service, host, port)
    print(f"[startup] 容量调度服务监听 http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[shutdown] 正在退出")
    finally:
        server.server_close()
        storage.close()


def seed(db_path: str) -> None:
    storage = Storage(db_path)
    svc = Service(storage)
    admin = Actor(DISPATCHER, "值班调度员")
    now = datetime.now(timezone.utc)
    start = (now + timedelta(days=1)).replace(microsecond=0)
    end = start + timedelta(hours=2)

    svc.create_venue(admin, "v_park", "城市公园", "东城区")
    svc.create_venue(admin, "v_temple", "古建景区", "西城区")
    svc.create_activity(admin, "a_show", "中秋实景演出", "演出", "市文旅集团")
    svc.create_activity(admin, "a_harvest", "京郊丰收节", "节庆", "密云区旅发委")
    svc.create_session(
        admin, "s_show_1", "a_show", "v_park",
        start.isoformat(), end.isoformat(), 500,
    )
    svc.create_ticket_type(
        admin, "tt_std", "s_show_1", "普通票", 12000, quota=450
    )
    svc.create_ticket_type(
        admin, "tt_free", "s_show_1", "入境团队免费票", 0,
        quota=50, is_free=True, requires_auth=True,
    )
    print("[seed] 演示数据已写入：场地 2、活动 2、场次 s_show_1（500 席）")
    storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="城市文旅活动容量调度服务")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--db", default="tourism.db")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    p_seed = sub.add_parser("seed", help="写入演示数据")
    p_seed.add_argument("--db", default="tourism.db")

    args = parser.parse_args()
    if args.cmd == "serve":
        serve(args.db, args.host, args.port)
    else:
        seed(args.db)


if __name__ == "__main__":
    main()
