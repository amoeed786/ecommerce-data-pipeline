"""Run the Raunaqi web app on localhost over BOTH IPv6 and IPv4 - local only, never on the network.

Why: on Windows, "localhost" resolves to IPv6 (::1) first. Plain `uvicorn --port 8000` listens on IPv4
(127.0.0.1) only, so every new connection to http://localhost waits for the IPv6 attempt to fail
(about 2 s for some clients, ~0.3 s per connection in browsers). Listening on ::1 and 127.0.0.1 answers both
instantly, and still only accepts connections from this computer.

Usage (start.py uses this):
    python -m shop_app.serve --port 8000
"""
import argparse
import socket

import uvicorn


def local_sockets(port: int) -> list[socket.socket]:
    socks = []
    for family, addr in [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]:
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: refuse to share the port with another server
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((addr, port))
            socks.append(s)
        except OSError:
            if family == socket.AF_INET:
                raise  # IPv4 must work; IPv6 is a bonus (some machines have it disabled)
    return socks


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Raunaqi on localhost (IPv4 + IPv6)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    config = uvicorn.Config("shop_app.main:app", log_level="info")
    uvicorn.Server(config).run(sockets=local_sockets(args.port))


if __name__ == "__main__":
    main()
