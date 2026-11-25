"""Setup, entities, wake word sync and a full voice turn: HA <-> real agent."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from unittest.mock import patch

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType, PipelineStage
from homeassistant.components.assist_satellite import AssistSatelliteConfiguration
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_slate.const import CONF_DEVICE_ID, CONF_FINGERPRINT, CONF_TOKEN, DOMAIN

from .conftest import FAKE_TFLITE, WAKE, micro_config


async def _setup(hass: HomeAssistant, rig, custom_wake_words: dict | None = None) -> MockConfigEntry:
    token, err = rig.agent.pairing.try_pair(None)
    assert token, err
    if custom_wake_words:
        folder = hass.config.path("custom_wake_words")
        await hass.async_add_executor_job(_write_models, folder, custom_wake_words)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="02aabbccddee", title="Kitchen Slate", data={
        CONF_HOST: "127.0.0.1", CONF_PORT: rig.port, CONF_FINGERPRINT: rig.fingerprint,
        CONF_TOKEN: token, CONF_DEVICE_ID: "02aabbccddee"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await _wait(lambda: entry.runtime_data.state)
    return entry


def _write_models(folder: str, models: dict) -> None:
    import os
    os.makedirs(folder, exist_ok=True)
    for mid, phrase in models.items():
        with open(f"{folder}/{mid}.tflite", "wb") as f:
            f.write(FAKE_TFLITE)
        with open(f"{folder}/{mid}.json", "w") as f:
            json.dump(micro_config(phrase), f)


async def _wait(predicate, timeout: float = 3.0):
    async with asyncio.timeout(timeout):
        while not (value := predicate()):
            await asyncio.sleep(0.02)
    return value


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    eid = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"02aabbccddee-{key}")
    assert eid, f"{platform}.{key} missing"
    return eid


async def test_entities_created(hass: HomeAssistant, rig) -> None:
    await _setup(hass, rig)
    for platform, key in [("assist_satellite", "assist_satellite"), ("light", "edge_light"), ("light", "display"),
                          ("select", "pipeline"), ("select", "vad_sensitivity"), ("select", "wake_word_sensitivity"),
                          ("select", "wake_word_location"), ("switch", "muted"), ("switch", "wake_sound"),
                          ("number", "volume"), ("text", "kiosk_url"), ("button", "reboot"),
                          ("button", "reload_kiosk"), ("event", "wake_word_event")]:
        state = hass.states.get(_eid(hass, platform, key))
        assert state is not None and state.state != "unavailable", (platform, key)
    assert hass.states.get(_eid(hass, "number", "volume")).state == "80"
    assert hass.states.get(_eid(hass, "assist_satellite", "assist_satellite")).state == "idle"


async def test_custom_wake_words_synced_and_selectable(hass: HomeAssistant, rig) -> None:
    entry = await _setup(hass, rig, custom_wake_words={"hey_slate": "Hey Slate"})
    # Uploaded to the panel during setup and listed back to HA
    assert (rig.tmp / "state" / "wakewords" / "hey_slate.tflite").read_bytes() == FAKE_TFLITE
    sat_id = _eid(hass, "assist_satellite", "assist_satellite")
    sat = hass.data["entity_components"]["assist_satellite"].get_entity(sat_id)
    config = sat.async_get_configuration()
    assert {w.id for w in config.available_wake_words} == {"okay_nabu", "hey_jarvis", "hey_slate"}
    assert config.active_wake_words == ["okay_nabu"] and config.max_active_wake_words == 2

    # HA's voice settings UI calls this when the user picks wake words
    await sat.async_set_configuration(AssistSatelliteConfiguration(
        available_wake_words=config.available_wake_words, active_wake_words=["hey_slate", "okay_nabu"],
        max_active_wake_words=2))
    assert rig.agent.settings.active_wake_words == ["hey_slate", "okay_nabu"]
    assert [m.id for m in rig.agent._engine.models] == ["hey_slate", "okay_nabu"]

    # Model deleted from the folder -> service removes it from the panel
    await hass.async_add_executor_job(_remove, hass.config.path("custom_wake_words"), "hey_slate")
    resp = await hass.services.async_call(DOMAIN, "sync_wake_words", blocking=True, return_response=True)
    assert resp["Kitchen Slate"]["removed"] == ["hey_slate"]
    await _wait(lambda: "hey_slate" not in {w["id"] for w in entry.runtime_data.state["wake_words"]})


def _remove(folder: str, mid: str) -> None:
    import os
    for suffix in (".json", ".tflite"):
        os.remove(f"{folder}/{mid}{suffix}")


async def test_controls_reach_panel(hass: HomeAssistant, rig) -> None:
    await _setup(hass, rig)
    await hass.services.async_call("switch", "turn_on", {"entity_id": _eid(hass, "switch", "muted")}, blocking=True)
    assert rig.agent.settings.muted is True
    await _wait(lambda: hass.states.get(_eid(hass, "switch", "muted")).state == "on")

    await hass.services.async_call("number", "set_value", {"entity_id": _eid(hass, "number", "volume"), "value": 35},
                                   blocking=True)
    assert rig.agent.settings.volume == 0.35

    await hass.services.async_call("select", "select_option", {
        "entity_id": _eid(hass, "select", "wake_word_location"), "option": "home_assistant"}, blocking=True)
    assert rig.agent.settings.wake_word_location == "home_assistant"

    edge = _eid(hass, "light", "edge_light")
    await hass.services.async_call("light", "turn_on", {"entity_id": edge, "rgb_color": [255, 0, 0],
                                                         "brightness": 128, "effect": "Breathe"}, blocking=True)
    assert rig.lightd.state["edge"] == {"state": True, "brightness": 128 / 255, "red": 1.0, "green": 0.0,
                                        "blue": 0.0, "effect": "Breathe"}
    await _wait(lambda: hass.states.get(edge).attributes.get("effect") == "Breathe")
    assert hass.states.get(edge).attributes["rgb_color"] == (255, 0, 0)

    await hass.services.async_call("text", "set_value", {"entity_id": _eid(hass, "text", "kiosk_url"),
                                                         "value": "http://ha.local:8123/wall"}, blocking=True)
    assert (rig.tmp / "kiosk.env").read_text() == "KIOSK_URL=http://ha.local:8123/wall\n"

    await hass.services.async_call("button", "press", {"entity_id": _eid(hass, "button", "reboot")}, blocking=True)
    assert (rig.tmp / "run" / "reboot").exists()


def _wav(pcm: bytes, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class FakeTtsStream:
    extension = "wav"

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def async_stream_result(self):
        yield self.data[:30]   # header split across chunks, like a real stream
        yield self.data[30:]


async def test_voice_turn_end_to_end(hass: HomeAssistant, rig) -> None:
    await _setup(hass, rig)
    sat_id = _eid(hass, "assist_satellite", "assist_satellite")
    received = {}
    tts_pcm = b"\x11\x22" * 2000
    wake_events = []
    hass.bus.async_listen("ha_slate_wake_word_detected", lambda e: wake_events.append(e.data))

    async def fake_pipeline(hass, *, event_callback, stt_stream, start_stage, wake_word_phrase, **kwargs):
        received.update(start_stage=start_stage, wake_word_phrase=wake_word_phrase, audio=b"")
        event_callback(PipelineEvent(PipelineEventType.RUN_START, {"tts_output": {"token": "tok"}}))
        event_callback(PipelineEvent(PipelineEventType.STT_START, {}))
        async for chunk in stt_stream:
            received["audio"] += chunk
            if len(received["audio"]) >= 1280:
                break
        event_callback(PipelineEvent(PipelineEventType.STT_VAD_END, {}))
        event_callback(PipelineEvent(PipelineEventType.STT_END, {"stt_output": {"text": "lights on"}}))
        event_callback(PipelineEvent(PipelineEventType.INTENT_START, {}))
        event_callback(PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": {"continue_conversation": False}}))
        event_callback(PipelineEvent(PipelineEventType.TTS_START, {}))
        event_callback(PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"token": "tok"}}))
        event_callback(PipelineEvent(PipelineEventType.RUN_END))

    with patch("homeassistant.components.assist_satellite.entity.async_pipeline_from_audio_stream", fake_pipeline), \
         patch("custom_components.ha_slate.assist_satellite.tts.async_get_stream",
               return_value=FakeTtsStream(_wav(tts_pcm))):
        await rig.feed(WAKE, b"\x01" * 640, b"\x02" * 640, b"\x03" * 640)
        await _wait(lambda: received.get("audio") and len(received["audio"]) >= 1280)
        await _wait(lambda: any(p == (22050, tts_pcm) for p in rig.player.played))
        await _wait(lambda: hass.states.get(sat_id).state == "idle")

    assert received["start_stage"] is PipelineStage.STT
    assert received["wake_word_phrase"] == "Okay Nabu"
    assert received["audio"][:1280] == b"\x01" * 640 + b"\x02" * 640
    assert wake_events and wake_events[0]["wake_word_id"] == "okay_nabu"
    event_state = hass.states.get(_eid(hass, "event", "wake_word_event"))
    assert event_state.attributes["event_type"] == "detected"
    ev = rig.lightd.events
    assert ev.index("listening") < ev.index("thinking") < ev.index("tts_speaking") < ev.index("tts_finished")


async def test_continue_conversation_reopens_mic(hass: HomeAssistant, rig) -> None:
    await _setup(hass, rig)
    runs = []

    async def fake_pipeline(hass, *, event_callback, stt_stream, start_stage, wake_word_phrase, **kwargs):
        runs.append((start_stage, wake_word_phrase))
        event_callback(PipelineEvent(PipelineEventType.RUN_START, {"tts_output": {"token": "tok"}}))
        async for _ in stt_stream:
            break
        event_callback(PipelineEvent(PipelineEventType.STT_VAD_END, {}))
        follow_up = len(runs) == 1
        event_callback(PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": {"continue_conversation": follow_up}}))
        event_callback(PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"token": "tok"}}))
        event_callback(PipelineEvent(PipelineEventType.RUN_END))

    with patch("homeassistant.components.assist_satellite.entity.async_pipeline_from_audio_stream", fake_pipeline), \
         patch("custom_components.ha_slate.assist_satellite.tts.async_get_stream",
               side_effect=lambda h, t: FakeTtsStream(_wav(b"\x00\x01" * 100))):
        await rig.feed(WAKE, b"\x01" * 640)
        await _wait(lambda: len(runs) >= 1)
        await rig.feed(b"\x02" * 640, b"\x03" * 640)  # follow-up answer, no wake word
        await _wait(lambda: len(runs) >= 2)
    assert runs[0] == (PipelineStage.STT, "Okay Nabu")
    assert runs[1] == (PipelineStage.STT, None)


async def test_timer_forwarded(hass: HomeAssistant, rig) -> None:
    from homeassistant.components import intent
    await _setup(hass, rig)
    sat = hass.data["entity_components"]["assist_satellite"].get_entity(_eid(hass, "assist_satellite", "assist_satellite"))
    timer = intent.TimerInfo(id="t1", name="pasta", seconds=300, device_id=sat.registry_entry.device_id,
                             start_hours=None, start_minutes=5, start_seconds=None,
                             created_at=0, updated_at=0, language="en")
    sat._handle_timer(intent.TimerEventType.STARTED, timer)
    await _wait(lambda: "timer_ticking" in rig.lightd.events)
    sat._handle_timer(intent.TimerEventType.FINISHED, timer)
    await _wait(lambda: "timer_ringing" in rig.lightd.events)


async def test_reconnect_after_panel_restart(hass: HomeAssistant, rig) -> None:
    entry = await _setup(hass, rig)
    sat_id = _eid(hass, "assist_satellite", "assist_satellite")
    with patch("custom_components.ha_slate.device.RECONNECT_MIN", 0.05):
        await rig.drop_session()
        await _wait(lambda: hass.states.get(sat_id).state == "unavailable")
        await _wait(lambda: entry.runtime_data.connected, timeout=5)
    await _wait(lambda: hass.states.get(sat_id).state == "idle")


async def test_factory_reset_panel_triggers_reauth(hass: HomeAssistant, rig) -> None:
    entry = await _setup(hass, rig)
    rig.agent.pairing.unpair()           # panel factory reset
    with patch("custom_components.ha_slate.device.RECONNECT_MIN", 0.05):
        await rig.drop_session()
        await _wait(lambda: any(f["context"]["source"] == "reauth"
                                for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)), timeout=5)
    assert entry.state is ConfigEntryState.LOADED


async def test_announce_converts_and_streams(hass: HomeAssistant, rig, tmp_path) -> None:
    import shutil

    import pytest
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    from homeassistant.components.assist_satellite import AssistSatelliteAnnouncement
    from homeassistant.components.ffmpeg import DOMAIN as FFMPEG_DOMAIN
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, FFMPEG_DOMAIN, {})
    await _setup(hass, rig)
    media = tmp_path / "chime.wav"
    pcm = b"\x00\x10" * 11025  # 0.5 s at 22.05 kHz
    media.write_bytes(_wav(pcm))
    sat = hass.data["entity_components"]["assist_satellite"].get_entity(_eid(hass, "assist_satellite", "assist_satellite"))
    await sat.async_announce(AssistSatelliteAnnouncement(
        message="hello", media_id=str(media), original_media_id=str(media), tts_token=None,
        media_id_source="url"))
    rate, played = rig.player.played[-1]
    assert rate == 22050 and played == pcm


async def test_home_assistant_side_wake_word(hass: HomeAssistant, rig) -> None:
    await _setup(hass, rig)
    await hass.services.async_call("select", "select_option", {
        "entity_id": _eid(hass, "select", "wake_word_location"), "option": "home_assistant"}, blocking=True)
    runs = []

    async def fake_pipeline(hass, *, event_callback, stt_stream, start_stage, wake_word_phrase, **kwargs):
        audio = b""
        async for chunk in stt_stream:
            audio += chunk
            if len(audio) >= 1280:
                break
        runs.append((start_stage, audio))
        event_callback(PipelineEvent(PipelineEventType.WAKE_WORD_END,
                                     {"wake_word_output": {"wake_word_id": "computer", "timestamp": 0}}))
        event_callback(PipelineEvent(PipelineEventType.STT_VAD_END, {}))
        event_callback(PipelineEvent(PipelineEventType.RUN_END))

    with patch("homeassistant.components.assist_satellite.entity.async_pipeline_from_audio_stream", fake_pipeline):
        await rig.feed(b"\x09" * 640, b"\x08" * 640)  # no on-device wake word: plain room audio
        await _wait(lambda: runs)
    assert runs[0] == (PipelineStage.WAKE_WORD, b"\x09" * 640 + b"\x08" * 640)
    await _wait(lambda: "wake_word_detected" in rig.lightd.events)
