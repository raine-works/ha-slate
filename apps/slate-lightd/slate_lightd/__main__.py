"""slate-lightd: edge light bar + backlight daemon for the HA Slate panel."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import tomllib
from typing import Any

from . import backends, control
from .color import from_bytes
from .display import Backlight, find_backlight
from .engine import Engine
from .bridge import VoiceBridge
from .power import PowerBudget

_LOG = logging.getLogger("slate_lightd")

DEFAULTS: dict[str, Any] = {
    "bar": {"backend": "console", "led_count": 24, "reverse": False, "gamma": 2.2, "fps": 50},
    "power": {"max_current_ma": 600, "ma_per_channel": 20, "quiescent_ma_per_led": 1.0},
    "display": {"backlight": "auto", "min_level": 0.05},
    "theme": {"accent": [0, 110, 255]},
    "control": {"socket": "/run/slate-lightd/control.sock"},
    "state": {"file": "/var/lib/slate-lightd/state.json"},
}


def load_config(path: str | None) -> dict[str, Any]:
    """Return ``DEFAULTS`` overlaid, section by section, with the TOML file at ``path``.

    A missing file (or no ``path``) gives the defaults; a malformed one raises
    ``tomllib.TOMLDecodeError``.
    """
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            for section, values in tomllib.load(f).items():
                cfg.setdefault(section, {}).update(values)
    return cfg


def sd_notify(msg: str) -> None:
    """Send ``msg`` (e.g. ``READY=1``) to systemd's notify socket; does nothing outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.sendto(msg.encode(), addr)


class StateStore:
    """Persists the HA-set user light so it survives reboots and OTA."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, state: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.path)
        except OSError:
            _LOG.warning("could not persist state to %s", self.path)


async def main_async(cfg: dict[str, Any]) -> None:
    """Run the daemon with config ``cfg`` until SIGTERM or SIGINT.

    Opens the backend and backlight, restores the saved lights, starts the control socket,
    reports ready to systemd, and on a signal blanks the bar and stops. Raises what the
    backend raises if its device can't be opened.
    """
    bar = cfg["bar"]
    backend_opts = dict(bar.get(bar["backend"].replace("-", "_"), {}))
    backend = backends.create(bar["backend"], int(bar["led_count"]), backend_opts)
    budget = PowerBudget(**{k: float(v) for k, v in cfg["power"].items()})
    engine = Engine(backend, budget, fps=float(bar["fps"]), gamma=float(bar["gamma"]), reverse=bool(bar["reverse"]))
    accent = from_bytes(cfg["theme"]["accent"])
    store = StateStore(cfg["state"]["file"])
    state = store.load()

    bl_path = find_backlight(str(cfg["display"]["backlight"]))
    backlight = Backlight(bl_path, float(cfg["display"]["min_level"])) if bl_path else None

    bridge = VoiceBridge(engine, accent)
    controller = control.LightController(engine, bridge, backlight, state, store.save)
    # Until slate-agent reports a Home Assistant session, show "not connected".
    bridge.handle({"event": "disconnected"})
    engine.claim("system:boot", "scan", accent, brightness=0.6, ttl=2.5, priority=90)

    os.makedirs(os.path.dirname(cfg["control"]["socket"]), exist_ok=True)
    server = await control.serve(engine, cfg["control"]["socket"], controller)

    async def timer_ticker() -> None:
        while True:
            await asyncio.sleep(0.5)
            if bridge.timers:
                bridge.tick_timers()

    tasks = [asyncio.create_task(engine.run()), asyncio.create_task(timer_ticker())]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    sd_notify("READY=1")
    _LOG.info("running: backend=%s leds=%s backlight=%s", bar["backend"], bar["led_count"], bl_path)
    await stop.wait()

    sd_notify("STOPPING=1")
    for t in tasks:
        t.cancel()
    server.close()
    backend.close()


def main() -> None:
    """The ``slate-lightd`` command: parse the arguments, load the config and run the daemon."""
    p = argparse.ArgumentParser(prog="slate-lightd", description=__doc__)
    p.add_argument("-c", "--config", default="/etc/slate/lightd.toml")
    p.add_argument("--backend", help="override [bar].backend (console, null, ws2812-spi, sysfs)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.backend:
        cfg["bar"]["backend"] = args.backend
    asyncio.run(main_async(cfg))


if __name__ == "__main__":
    main()
