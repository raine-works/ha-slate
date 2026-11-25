"""slate-lightctl: drive slate-lightd from shell scripts (RAUC hooks, units).

    slate-lightctl claim system progress --color 0,80,255 --value 0.4
    slate-lightctl release system
    slate-lightctl status
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

DEFAULT_SOCKET = "/run/slate-lightd/control.sock"


def send(path: str, req: dict) -> dict:
    """Send one request to the control socket at ``path`` and return slate-lightd's reply.

    Raises ``OSError`` if the daemon isn't reachable or doesn't answer within 2 s.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(path)
        s.sendall((json.dumps(req) + "\n").encode())
        return json.loads(s.makefile().readline())


def main() -> None:
    """The ``slate-lightctl`` command: prints the reply as JSON.

    Exits 0 on success, 1 when the daemon refused the request, 2 when it couldn't be reached.
    """
    p = argparse.ArgumentParser(prog="slate-lightctl")
    p.add_argument("--socket", default=DEFAULT_SOCKET)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("source")
    c.add_argument("effect")
    c.add_argument("--color", default="255,255,255")
    c.add_argument("--brightness", type=float, default=1.0)
    c.add_argument("--value", type=float, default=0.0)
    c.add_argument("--ttl", type=float)
    r = sub.add_parser("release")
    r.add_argument("source")
    sub.add_parser("status")
    a = p.parse_args()

    if a.cmd == "claim":
        req = {"claim": a.source, "effect": a.effect, "color": [int(x) for x in a.color.split(",")],
               "brightness": a.brightness, "value": a.value, "ttl": a.ttl}
    elif a.cmd == "release":
        req = {"release": a.source}
    else:
        req = {"status": True}
    try:
        resp = send(a.socket, req)
    except OSError as exc:
        print(f"slate-lightctl: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(resp))
    sys.exit(0 if resp.get("ok", True) else 1)


if __name__ == "__main__":
    main()
