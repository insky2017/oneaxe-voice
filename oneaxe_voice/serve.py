"""One application and model lifecycle, with local and optional Tailnet TLS sockets."""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import time
from urllib.parse import urlsplit

import uvicorn

from .config import Settings

LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class MobileListener:
    hostname: str
    bind: str
    port: int
    cert_file: Path
    key_file: Path
    renew: bool = True
    docker_image: str | None = None

    @classmethod
    def read(cls, runtime: Path):
        path = runtime / "mobile-listener.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        required = {"hostname", "bind", "port", "cert_file", "key_file"}
        if (not isinstance(value, dict) or not required <= value.keys()
                or value.keys() - required - {"renew", "docker_image"}):
            raise ValueError("移动监听配置字段无效")
        address = ipaddress.ip_address(value["bind"])
        tailnet = (address in ipaddress.ip_network("100.64.0.0/10") if address.version == 4
                   else address in ipaddress.ip_network("fd7a:115c:a1e0::/48"))
        if not tailnet:
            raise ValueError("移动监听必须绑定明确的 Tailnet 地址")
        hostname = value["hostname"]
        if (not isinstance(hostname, str) or not hostname.endswith(".ts.net")
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in hostname)):
            raise ValueError("移动入口必须使用完整 Tailnet DNS 名")
        port = value["port"]
        if type(port) is not int or not 1 <= port <= 65535 or type(value.get("renew", True)) is not bool:
            raise ValueError("移动端口或证书续期配置无效")
        docker_image = value.get("docker_image")
        if "docker_image" in value and (not isinstance(docker_image, str)
                                       or not re.fullmatch(r"sha256:[0-9a-f]{64}", docker_image)):
            raise ValueError("Docker 证书续期须使用固定 sha256 镜像 ID")
        paths = [Path(value[k]).expanduser().resolve() for k in ("cert_file", "key_file")]
        if not all(p.is_file() for p in paths):
            raise ValueError("移动 TLS 证书或私钥不存在")
        if paths[1].stat().st_mode & 0o077:
            raise ValueError("移动 TLS 私钥权限须为 0600")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(paths[0]), str(paths[1]))
        return cls(hostname, str(address), port, *paths, value.get("renew", True), docker_image)


class SharedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # One signal owner supervises every listener; Uvicorn must not replace it.
        yield


def listen(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(2048)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


async def renew_certificates(listener: MobileListener, config: uvicorn.Config):
    """Refresh TLS for new connections without restarting active dictation."""
    from .setup_mobile import issue_certificate
    stamp = (listener.cert_file.stat().st_mtime_ns, listener.key_file.stat().st_mtime_ns)
    next_renew = time.monotonic() + 12 * 3600
    while True:
        await asyncio.sleep(60)
        try:
            if listener.renew and time.monotonic() >= next_renew:
                next_renew = time.monotonic() + 12 * 3600
                await asyncio.to_thread(issue_certificate, listener.hostname,
                                        listener.cert_file, listener.key_file,
                                        docker_image=listener.docker_image)
            current = (listener.cert_file.stat().st_mtime_ns, listener.key_file.stat().st_mtime_ns)
            if current != stamp:
                config.ssl.load_cert_chain(str(listener.cert_file), str(listener.key_file))
                stamp = current
                LOGGER.info("mobile_tls_refreshed")
        except Exception as exc:
            LOGGER.error("mobile_tls_refresh_failed kind=%s", type(exc).__name__)


async def run(settings: Settings | None = None):
    from .server import create_app

    settings = settings or Settings.from_env()
    mobile = MobileListener.read(settings.runtime_dir)
    parsed = urlsplit(settings.api_url)
    local_host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
    app = create_app(settings)
    common = dict(lifespan="off", access_log=False, proxy_headers=False,
                  timeout_graceful_shutdown=180, ws_max_size=2 * 1024 * 1024,
                  ws_max_queue=16)
    configurations = [uvicorn.Config(app, host=local_host, port=parsed.port or 8097, **common)]
    if mobile:
        configurations.append(uvicorn.Config(
            app, host=mobile.bind, port=mobile.port, ssl_certfile=str(mobile.cert_file),
            ssl_keyfile=str(mobile.key_file), **common))
    sockets, servers, tasks = [], [], []
    renewal = None
    loop = asyncio.get_running_loop()

    def stop():
        for server in servers:
            server.should_exit = True

    try:
        for config in configurations:
            sockets.append(listen(config.host, config.port))
            config.load()
            servers.append(SharedServer(config))
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop)
        async with app.router.lifespan_context(app):
            tasks = [asyncio.create_task(server.serve(sockets=[sock]))
                     for server, sock in zip(servers, sockets)]
            if mobile:
                renewal = asyncio.create_task(renew_certificates(mobile, configurations[1]))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            stop()
            # Drain both listeners before the shared lifespan releases weights.
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in tasks:
                task.result()
    finally:
        stop()
        if renewal:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for sock in sockets:
            sock.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    os.umask(0o077)
    asyncio.run(run())


if __name__ == "__main__":
    main()
