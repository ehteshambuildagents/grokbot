"""Loopback-only HTTP/CONNECT proxy that every Chromium request goes through.

Why: the page route guard checks a URL's host, but Chromium resolves DNS itself afterwards, so a DNS-rebinding
host could pass the check and then connect to 127.0.0.1 or a metadata address. Here the proxy resolves each
destination exactly once with `resolve_checked` and connects to that validated address, so check and use cannot
diverge. Plain-HTTP requests are forwarded with `Connection: close` (one request per upstream connection).
"""
import asyncio
import contextlib
from urllib.parse import urlsplit

from localcrew.security import resolve_checked

HOP = ("proxy-connection:", "proxy-authorization:", "connection:", "keep-alive:")


class EgressProxy:
    def __init__(self):
        self.port = 0
        self.requests = 0
        self.blocked: list[str] = []
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _pin(self, host: str, port: int) -> str | None:
        try:
            return (await asyncio.to_thread(resolve_checked, host, port))[0]
        except ValueError as e:
            self.blocked = [*self.blocked[-49:], f"{host}:{port} ({e})"]
            return None

    @staticmethod
    async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                w.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            self.requests += 1
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                host, port = host.strip("[]"), int(port)
            else:
                u = urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    raise ValueError("unsupported proxy request")
                host, port = u.hostname, u.port or 80
            ip = await self._pin(host, port)
            if ip is None:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(ip, port), 30)
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                headers = [h for h in lines[1:] if h and not h.lower().startswith(HOP)]
                upgrade = any(h.lower().startswith("upgrade:") for h in headers)
                headers.append("Connection: Upgrade" if upgrade else "Connection: close")
                request = f"{method} {path} {version}\r\n" + "".join(f"{h}\r\n" for h in headers) + "\r\n"
                up_w.write(request.encode("latin-1"))
                await up_w.drain()
            await asyncio.gather(self._pipe(reader, up_w), self._pipe(up_r, writer))
        except (ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                writer.close()
