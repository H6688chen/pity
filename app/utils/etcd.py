import asyncio
import json

from aioetcd3.client import client
from loguru import logger


class EtcdClient(object):
    client = None
    scheme = "pity"

    def __init__(self, host, **kwargs):
        self.client = client(host)

    async def unregister_service(self, name, addr):
        await self.client.delete("/{}/{}/{}".format(self.scheme, name, addr))

    async def register_service(self, name, addr, ttl):
        while True:
            service = EtcdClient.lower_first(name)
            value, meta = await self.client.get("/{}/{}/{}".format(self.scheme, service, addr))
            if value is None:
                await self.with_alive(service, addr, ttl)
                logger.info(f"注册服务: {service} 成功. 📢")
            await asyncio.sleep(ttl)

    @staticmethod
    def lower_first(s: str):
        if len(s) == 0:
            return ""
        if not s[0].islower():
            return s[0].lower() + s[1:]
        return s

    async def register_api(self, name, instance, cfg):
        version = cfg.get("version")
        methods = cfg.get("method", {})
        for d in dir(instance):
            if d.startswith("_") or d.endswith("_"):
                continue
            if d not in methods.keys():
                logger.info("方法: {}注册失败, 请在service.yml中配置".format(d))
                continue
            info = methods.get(d)
            await self.register_single(version, name, d, info)

    async def register_single(self, version, service, method_name, no_auth=None):
        srv = EtcdClient.lower_first(service)
        md = EtcdClient.lower_first(method_name)
        key = f"{version}.{srv}.{md}"
        info = {"authorization": False if no_auth is None else no_auth.get("authorization"),
                "path": f"/{srv}/{md}"}
        await self.client.put(key, json.dumps(info, ensure_ascii=False))
        logger.info(f"服务: {srv} 方法: {md} 注册成功. 🍦")

    async def with_alive(self, name, addr, ttl):
        lease = await self.client.grant_lease(ttl)
        key = f"/{self.scheme}/{name}/{addr}"
        await self.client.put(key, addr, lease=lease)
        await self.refresh_lease(lease, ttl)

    async def refresh_lease(self, lease, ttl):
        try:
            while True:
                await self.client.refresh_lease(lease)
                logger.info("服务续租成功. 🏆")
                await asyncio.sleep(ttl - 5)
        except Exception as err:
            logger.warning(f"续租失败，可能导致服务无法被发现. 🛠\n详情: {err}")
