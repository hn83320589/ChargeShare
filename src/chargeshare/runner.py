"""在同一個 event loop 內啟動伺服器（供加速模擬與端對端測試共用時鐘）。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI


@asynccontextmanager
async def serving(app: FastAPI, host: str = "127.0.0.1", port: int = 0) -> AsyncIterator[int]:
    """啟動 uvicorn 並回傳實際監聽的 port；離開時關閉伺服器。"""
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # 啟動失敗時拋出原本的例外
        await asyncio.sleep(0.01)
    actual_port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield actual_port
    finally:
        server.should_exit = True
        await task
