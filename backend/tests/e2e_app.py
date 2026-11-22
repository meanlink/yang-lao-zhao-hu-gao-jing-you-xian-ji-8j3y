"""本地 E2E 启动垫片：用 fakeredis 顶替 Redis，其余与容器内运行一致。"""
import fakeredis

from src.db import cache as _unused  # noqa: F401  确保模块先载入
import src.db as db

db.cache = fakeredis.FakeRedis(decode_responses=False)

import src.main as main  # noqa: E402

main.cache = db.cache
app = main.app
