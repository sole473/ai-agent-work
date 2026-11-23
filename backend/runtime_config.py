"""读取 Nacos 中可在线调整的搜索参数。"""

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os

import httpx
import yaml

logger = logging.getLogger("uvicorn.error")
DEFAULT_TIMEOUT = 8


class RuntimeConfig:
    search_timeout = DEFAULT_TIMEOUT

    async def refresh(self, client: httpx.AsyncClient):
        headers = {}
        username = os.getenv("NACOS_USERNAME")
        if username:
            login = await client.post("/nacos/v3/auth/user/login", data={
                "username": username,
                "password": os.environ["NACOS_PASSWORD"],
            })
            login.raise_for_status()
            headers["accessToken"] = login.json()["accessToken"]
        response = await client.get("/nacos/v3/client/cs/config", headers=headers, params={
            "namespaceId": os.getenv("NACOS_NAMESPACE", "public"),
            "groupName": os.getenv("NACOS_GROUP", "DEFAULT_GROUP"),
            "dataId": os.getenv("NACOS_DATA_ID", "music-agent.yaml"),
        })
        response.raise_for_status()
        payload = response.json()
        if payload["code"] != 0:
            raise ValueError("Nacos rejected the configuration request.")
        timeout = yaml.safe_load(payload["data"]["content"])["search"]["timeout_seconds"]
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise ValueError("Search timeout must be between 1 and 30 seconds.")
        if self.search_timeout != timeout:
            logger.info("nacos_search_timeout_updated seconds=%s", timeout)
        self.search_timeout = timeout

    async def poll(self, url: str):
        async with httpx.AsyncClient(base_url=url, timeout=5) as client:
            while True:
                try:
                    await self.refresh(client)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                    # 保留最后一次通过校验的值，日志只记录错误类型。
                    logger.warning("nacos_config_refresh_failed reason=%s", type(exc).__name__)
                await asyncio.sleep(30)

    @asynccontextmanager
    async def running(self):
        url = os.getenv("NACOS_URL")
        task = asyncio.create_task(self.poll(url)) if url else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


runtime_config = RuntimeConfig()
