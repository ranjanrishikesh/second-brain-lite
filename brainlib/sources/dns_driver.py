"""Fixed, isolated DNS worker; the capture owner bounds, kills, and reaps it."""

from __future__ import annotations

import json
import socket
import sys


def main() -> None:
    hostname, port = sys.argv[1:]
    addresses = sorted(
        {
            answer[4][0]
            for answer in socket.getaddrinfo(
                hostname, int(port), type=socket.SOCK_STREAM
            )
        }
    )
    payload = json.dumps(addresses).encode("utf-8")
    if len(payload) > 65536:
        raise ValueError("DNS response exceeds its byte limit")
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    main()
