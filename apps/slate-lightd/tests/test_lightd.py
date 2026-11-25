import asyncio
import json
import os

import pytest

from slate_lightd import control
from slate_lightd.backends.null import NullBackend
from slate_lightd.backends.sysfs import SysfsLedBackend
from slate_lightd.backends.ws2812_spi import RESET_BYTES, encode
from slate_lightd.effects import EFFECTS
from slate_lightd.engine import Engine
from slate_lightd.lva import EDGE_ID, VoiceBridge, run_client
from slate_lightd.power import PowerBudget


def make_engine(n=8, budget=None):
    return Engine(NullBackend(n), budget or PowerBudget(max_current_ma=1e9), gamma=1.0)


# --- WS2812 SPI encoding ------------------------------------------------------

def test_ws2812_bit_patterns():
    buf = encode([(0x00, 0xFF, 0x80)], "GRB")
    assert len(buf) == 3 * 4 + RESET_BYTES
    assert buf[0:4] == bytes([0xEE] * 4)  # G=0xFF -> every nibble 1110
    assert buf[4:8] == bytes([0x88] * 4)  # R=0x00 -> every nibble 1000
    assert buf[8:12] == bytes([0xE8, 0x88, 0x88, 0x88])  # B=0x80 -> 1110 then 1000s
    assert buf[12:] == bytes(RESET_BYTES)


def test_ws2812_reset_is_long_enough_at_3_2mhz():
    assert RESET_BYTES * 8 / 3.2e6 >= 300e-6


# --- power limiter ------------------------------------------------------------

def test_power_limit_scales_full_white():
    b = PowerBudget(max_current_ma=600, ma_per_channel=20, quiescent_ma_per_led=1)
    frame = [(255, 255, 255)] * 48  # 48 * 60 mA = 2880 mA + 48 quiescent
    out = b.limit(frame)
    assert b.estimate_ma(out) <= 600
    assert out[0][0] > 0


def test_power_limit_passthrough_when_under_budget():
    b = PowerBudget(max_current_ma=600)
    frame = [(10, 0, 0)] * 10
    assert b.limit(frame) == frame


# --- effects ------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(EFFECTS))
@pytest.mark.parametrize("n", [1, 2, 7, 48])
def test_effects_in_range(name, n):
    for t in (0.0, 0.1, 0.77, 5.0):
        frame = EFFECTS[name](t, n, (1.0, 0.5, 0.0), 0.5)
        assert len(frame) == n
        assert all(0.0 <= ch <= 1.0 for px in frame for ch in px)


def test_progress_fraction():
    frame = EFFECTS["progress"](0, 10, (1.0, 1.0, 1.0), 0.3)
    assert sum(px[0] for px in frame) == pytest.approx(3.0)


# --- engine priorities --------------------------------------------------------

def test_priority_and_release_reveals_lower_claim():
    e = make_engine()
    e.claim("user", "solid", (0.0, 1.0, 0.0))
    e.claim("voice", "solid", (0.0, 0.0, 1.0))
    assert e.active().source == "voice"
    e.release("voice")
    assert e.active().source == "user"
    assert e.render()[0] == (0, 255, 0)


def test_ttl_expiry():
    e = make_engine()
    e.claim("error", "flash", (1.0, 0.0, 0.0), ttl=0.0)
    assert e.active() is None


# --- LVA event mapping --------------------------------------------------------

def test_voice_pipeline_sequence():
    e = make_engine()
    user = []
    br = VoiceBridge(e, (0.0, 0.4, 1.0), user.append)
    br.handle({"event": "snapshot", "data": {"muted": False, "ha_connected": True}})
    assert e.active() is None
    br.handle({"event": "wake_word_detected", "data": {"wake_word_id": "x", "slot": 0, "score": 0.9}})
    assert e.active().effect == "grow"
    br.handle({"event": "thinking"})
    assert e.active().effect == "scan"
    br.handle({"event": "pipeline_error", "data": {"reason": "stt"}})
    assert e.active().source == "error"
    br.handle({"event": "light_command", "data": {"object_id": EDGE_ID, "state": True}})
    assert user and user[0]["object_id"] == EDGE_ID


def test_muted_and_disconnect():
    e = make_engine()
    br = VoiceBridge(e, (0.0, 0.4, 1.0), lambda d: None)
    br.handle({"event": "muted", "data": {"muted": True}})
    assert e.active().source == "muted"
    br.handle({"event": "disconnected"})
    assert e.active().source == "network"
    br.handle({"event": "zeroconf", "data": {"status": "connected"}})
    assert e.active().source == "muted"
    br.handle({"event": "muted", "data": {"muted": False}})
    assert e.active() is None


def test_timer_countdown_then_ring_then_idle():
    e = make_engine()
    br = VoiceBridge(e, (0.0, 0.4, 1.0), lambda d: None)
    br.handle({"event": "timer_ticking", "data": {"id": "t1", "name": "", "total_seconds": 60, "seconds_left": 30}})
    c = e.active()
    assert c.source == "timer" and c.value == pytest.approx(0.5, abs=0.01)
    br.handle({"event": "idle"})  # pipeline idle must not cancel the countdown
    assert e.active().source == "timer"
    br.handle({"event": "timer_ringing", "data": {"id": "t1", "name": "", "total_seconds": 60, "seconds_left": 0}})
    assert e.active().source == "alarm"
    br.handle({"event": "idle"})
    assert e.active() is None


def test_user_light_mapping():
    e = make_engine()
    br = VoiceBridge(e, (0.0, 0.4, 1.0), lambda d: None)
    br.apply_user_light({"object_id": EDGE_ID, "state": True, "brightness": 0.5, "red": 1.0, "green": 0.0, "blue": 0.0, "effect": "Breathe"})
    c = e.active()
    assert (c.effect, c.brightness, c.color) == ("breathe", 0.5, (1.0, 0.0, 0.0))
    br.apply_user_light({"object_id": EDGE_ID, "state": False})
    assert e.active() is None


# --- sysfs backend against a fake LED class tree ------------------------------

def test_sysfs_multicolor(tmp_path):
    led = tmp_path / "edge:rgb:0"
    led.mkdir()
    (led / "max_brightness").write_text("255\n")
    (led / "multi_index").write_text("red green blue\n")
    (led / "multi_intensity").write_text("0 0 0\n")
    (led / "brightness").write_text("0\n")
    be = SysfsLedBackend(["edge:rgb:0"], root=str(tmp_path))
    be.write([(255, 128, 0)])
    assert (led / "multi_intensity").read_text() == "255 128 0"
    assert (led / "brightness").read_text() == "255"


# --- control socket + LVA client end-to-end -----------------------------------

def test_control_socket_roundtrip(tmp_path):
    async def go():
        e = make_engine()
        path = str(tmp_path / "c.sock")
        server = await control.serve(e, path)
        r, w = await asyncio.open_unix_connection(path)
        w.write(b'{"claim": "system", "effect": "progress", "color": [0, 80, 255], "value": 0.4}\n')
        assert json.loads(await r.readline())["ok"]
        w.write(b'{"status": true}\n')
        status = json.loads(await r.readline())
        status["mode"] = oct(os.stat(path).st_mode & 0o777)
        w.close()
        server.close()
        return status

    status = asyncio.run(go())
    assert status["active"]["source"] == "system"
    assert status["mode"] == "0o660"


def test_lva_client_registers_and_maps_events():
    websockets = pytest.importorskip("websockets")

    async def go():
        received = []
        e = make_engine()
        br = VoiceBridge(e, (0.0, 0.4, 1.0), lambda d: None, on_display=lambda d: None)

        async def fake_lva(ws, *args):
            await ws.send(json.dumps({"event": "snapshot", "data": {"muted": False, "ha_connected": True}}))
            for _ in range(2):
                received.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"event": "listening"}))
            await asyncio.sleep(0.2)

        async with websockets.serve(fake_lva, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            task = asyncio.create_task(run_client(f"ws://127.0.0.1:{port}", br))
            await asyncio.sleep(0.15)
            active = e.active()
            task.cancel()
        return received, active

    received, active = asyncio.run(go())
    assert [r["data"]["object_id"] for r in received] == ["edge_light", "display"]
    assert all(r["command"] == "register_light" for r in received)
    assert active.source == "voice" and active.effect == "solid"
