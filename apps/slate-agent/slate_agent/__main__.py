"""slate-agent: HA Slate device agent (voice satellite + device control)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
from pathlib import Path

from . import __version__, audio
from .agent import Agent, AgentPaths
from .discovery import Advertiser
from .lightd import LightdClient
from .protocol import DEFAULT_PORT
from .security import Pairing, device_id, ensure_certificate, server_ssl_context
from .wakeword import WakeWordStore

_LOG = logging.getLogger("slate_agent")


def _sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.sendto(msg.encode(), addr)


async def run(args: argparse.Namespace) -> None:
    import websockets

    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    dev_id = device_id(state)
    name = args.name or f"HA Slate {dev_id[-6:]}"
    cert, key, fp = ensure_certificate(state, f"ha-slate-{dev_id}")
    _LOG.info("device %s, TLS fingerprint %s", dev_id, fp)

    pairing = Pairing(state / "token", Path(args.pairing_code_file) if args.pairing_code_file else None)
    if not pairing.paired:
        _LOG.warning("not paired: add this device in Home Assistant (Settings > Devices > Add > HA Slate)")

    player: audio.Player = audio.NullPlayer() if args.no_audio else audio.PwCatPlayer(args.playback_target)
    agent = Agent(
        paths=AgentPaths(state, Path(args.kiosk_env) if args.kiosk_env else None,
                         Path(args.runtime_dir) if args.runtime_dir else None),
        device_id=dev_id,
        name=name,
        store=WakeWordStore(state / "wakewords"),
        pairing=pairing,
        source_factory=(audio.QueueSource if args.no_audio else (lambda: audio.PwCatSource(args.capture_target))),
        player=player,
        lightd=LightdClient(args.lightd_socket or None),
    )
    await agent.start()
    server = await websockets.serve(agent.handle_connection, args.host, args.port,
                                    ssl=server_ssl_context(cert, key), max_size=2 * 1024 * 1024)
    adv = None
    if not args.no_zeroconf:
        adv = Advertiser(name, dev_id, args.port, __version__, pairing.paired)
        await adv.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    _sd_notify("READY=1")
    await stop.wait()
    _sd_notify("STOPPING=1")
    server.close()
    if adv:
        await adv.stop()
    await agent.stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="slate-agent", description=__doc__)
    p.add_argument("--state-dir", default="/data/agent")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--name", default=os.environ.get("SLATE_DEVICE_NAME"))
    p.add_argument("--capture-target", default="aec_mic")
    p.add_argument("--playback-target", default="aec_sink")
    p.add_argument("--lightd-socket", default="/run/slate-lightd/control.sock")
    p.add_argument("--kiosk-env", default="/data/agent/kiosk.env")
    p.add_argument("--runtime-dir", default="/run/slate-agent")
    p.add_argument("--pairing-code-file", default="/boot/slate-pairing-code",
                   help="factory pairing code (on the boot partition, survives factory reset)")
    p.add_argument("--no-audio", action="store_true", help="no capture/playback (dev host)")
    p.add_argument("--no-zeroconf", action="store_true")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
