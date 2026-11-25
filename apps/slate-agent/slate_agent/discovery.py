"""Advertise the agent as ``_ha-slate._tcp`` so Home Assistant discovers it."""

from __future__ import annotations

import logging
import socket

from .protocol import PROTOCOL_VERSION, ZEROCONF_TYPE

_LOG = logging.getLogger(__name__)


class Advertiser:
    def __init__(self, name: str, device_id: str, port: int, version: str, paired: bool) -> None:
        self.name, self.device_id, self.port = name, device_id, port
        self.version, self.paired = version, paired
        self._zc = None
        self._info = None

    async def start(self) -> None:
        import ifaddr
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        addresses = [
            socket.inet_aton(ip.ip)
            for adapter in ifaddr.get_adapters()
            for ip in adapter.ips
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254."))
        ]
        host = f"ha-slate-{self.device_id}"
        self._info = ServiceInfo(
            ZEROCONF_TYPE,
            f"{host}.{ZEROCONF_TYPE}",
            port=self.port,
            addresses=addresses,
            server=f"{host}.local.",
            properties={"id": self.device_id, "name": self.name, "version": self.version,
                        "protocol": str(PROTOCOL_VERSION), "paired": "1" if self.paired else "0"},
        )
        self._zc = AsyncZeroconf()
        await self._zc.async_register_service(self._info)
        _LOG.info("advertising %s on port %d", self._info.name, self.port)

    async def stop(self) -> None:
        if self._zc and self._info:
            await self._zc.async_unregister_service(self._info)
            await self._zc.async_close()
