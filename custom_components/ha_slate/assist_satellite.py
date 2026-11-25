"""Assist satellite entity: the panel as a first-class Home Assistant voice device.

Audio path:
  * Wake word on the panel (default): the panel detects, sends ``run-pipeline``
    with ``start_stage=stt`` and streams 16 kHz PCM until HA's VAD ends the turn.
  * Wake word in HA (option): the panel streams continuously with
    ``start_stage=wake_word``; the pipeline's wake word engine decides.
  * TTS and announcements are converted here (wav / ffmpeg -> raw PCM) and
    streamed to the panel, so the panel needs no codecs and no route back to HA.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from collections.abc import AsyncGenerator
from typing import Any

from homeassistant.components import ffmpeg, intent, tts
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType, PipelineStage
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityDescription,
    AssistSatelliteEntityFeature,
    AssistSatelliteWakeWord,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SlateConfigEntry
from .client import SlateError
from .const import (
    AUDIO_CHUNK_BYTES, CH_MIC, CH_SPK, DOMAIN, EVENT_WAKE_WORD_DETECTED, TTS_SAMPLE_RATE, TTS_TIMEOUT_EXTRA,
)
from .device import SlateDevice
from .entity import SlateEntity

_LOGGER = logging.getLogger(__name__)

# Only what the panel needs from each pipeline event (keeps transcripts and
# intent payloads off the wire unless the panel uses them).
_FORWARD = {
    PipelineEventType.RUN_START, PipelineEventType.WAKE_WORD_END, PipelineEventType.STT_START,
    PipelineEventType.STT_VAD_START, PipelineEventType.STT_VAD_END, PipelineEventType.STT_END,
    PipelineEventType.INTENT_START, PipelineEventType.INTENT_END, PipelineEventType.TTS_START,
    PipelineEventType.TTS_END, PipelineEventType.RUN_END, PipelineEventType.ERROR,
}


async def async_setup_entry(hass: HomeAssistant, entry: SlateConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Add the panel's Assist satellite entity."""
    async_add_entities([SlateAssistSatellite(entry.runtime_data)])


class SlateAssistSatellite(SlateEntity, AssistSatelliteEntity):
    """The panel as an Assist satellite: runs its voice turns, announcements and timers.

    ``SlateDevice`` routes the panel's pipeline messages and microphone audio here
    (``async_handle_message`` / ``async_handle_binary``); the entity streams TTS
    and announcement audio back as raw PCM (see the module docstring).
    """

    entity_description = AssistSatelliteEntityDescription(key="assist_satellite")
    _attr_translation_key = "assist_satellite"
    _attr_name = None
    _attr_supported_features = AssistSatelliteEntityFeature.ANNOUNCE | AssistSatelliteEntityFeature.START_CONVERSATION

    def __init__(self, device: SlateDevice) -> None:
        """Create the satellite for ``device``; it registers itself as ``device.satellite`` once added."""
        SlateEntity.__init__(self, device, "assist_satellite")
        AssistSatelliteEntity.__init__(self)
        self._audio_queue: asyncio.Queue[bytes | None] | None = None
        self._run_id: int | None = None
        self._tts_token: str | None = None
        self._continue_conversation = False
        self._played: asyncio.Event = asyncio.Event()
        self._tts_deadline_task: asyncio.Task | None = None
        self._ffmpeg: ffmpeg.FFmpegManager | None = None

    # -- HA plumbing -----------------------------------------------------------
    def _select_entity(self, key: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(Platform.SELECT, DOMAIN, f"{self.device.device_id}-{key}")

    @property
    def pipeline_entity_id(self) -> str | None:
        """Entity ID of this panel's pipeline select, or None if it isn't registered."""
        return self._select_entity("pipeline")

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        """Entity ID of this panel's VAD sensitivity select, or None if it isn't registered."""
        return self._select_entity("vad_sensitivity")

    @property
    def tts_options(self) -> dict[str, Any] | None:
        """Ask TTS for mono 16-bit WAV at the panel's rate, so it can be streamed without conversion."""
        # Plain keys: stable across HA releases (2026.12 wraps them in an enum).
        return {
            "preferred_format": "wav",
            "preferred_sample_rate": TTS_SAMPLE_RATE,
            "preferred_sample_channels": 1,
            "preferred_sample_bytes": 2,
        }

    async def async_added_to_hass(self) -> None:
        """Register with the device and as the timer handler for its HA device."""
        await super().async_added_to_hass()
        self.device.satellite = self
        assert self.registry_entry is not None and self.registry_entry.device_id is not None
        self.async_on_remove(intent.async_register_timer_handler(
            self.hass, self.registry_entry.device_id, self._handle_timer))

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the device and end any pipeline's audio stream."""
        self.device.satellite = None
        self._end_audio()
        await super().async_will_remove_from_hass()

    # -- configuration: the wake word picker in HA's voice settings ------------
    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        """The wake words the panel has, which are active and how many may be, from its last state."""
        state = self.device.state
        return AssistSatelliteConfiguration(
            available_wake_words=[
                AssistSatelliteWakeWord(id=w["id"], wake_word=w["wake_word"],
                                        trained_languages=list(w.get("trained_languages", [])))
                for w in state.get("wake_words", [])
            ],
            active_wake_words=list(state.get("settings", {}).get("active_wake_words", [])),
            max_active_wake_words=int(state.get("max_active_wake_words", 1)),
        )

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        """Set the panel's active wake words; raises ``SlateError`` if it refuses them or isn't connected."""
        await self.device.async_set_settings(active_wake_words=list(config.active_wake_words))

    @callback
    def async_device_state_changed(self) -> None:
        """Called by the device when the panel pushes new state."""
        self.async_write_ha_state()

    # -- panel -> HA -----------------------------------------------------------
    @callback
    def async_handle_message(self, msg: dict[str, Any]) -> None:
        """Handle a voice message from the panel; unknown types are ignored."""
        typ = msg.get("type")
        if typ == "run-pipeline":
            self._start_pipeline(msg)
        elif typ == "audio-end":
            if msg.get("run_id") == self._run_id:
                self._end_audio()
        elif typ == "played":
            self._on_played(msg)
        elif typ == "wake-word-detected":
            self.hass.bus.async_fire(EVENT_WAKE_WORD_DETECTED, {
                "device_id": self.registry_entry.device_id if self.registry_entry else None,
                "entity_id": self.entity_id,
                "wake_word_id": msg.get("wake_word_id"),
                "wake_word_phrase": msg.get("wake_word_phrase"),
                "probability": msg.get("probability"),
            })

    @callback
    def async_handle_binary(self, channel: int, payload: bytes) -> None:
        """Feed microphone audio to the running pipeline; other channels, or audio with none running, are dropped."""
        if channel == CH_MIC and self._audio_queue is not None:
            self._audio_queue.put_nowait(payload)

    @callback
    def async_device_disconnected(self) -> None:
        """End the running turn and any wait for playback, so the entity doesn't stay busy."""
        self._end_audio()
        self._played.set()
        if self.state != "idle":
            self.tts_response_finished()

    def _start_pipeline(self, msg: dict[str, Any]) -> None:
        self._end_audio()
        self._run_id = msg.get("run_id")
        self._audio_queue = asyncio.Queue()
        self._continue_conversation = False
        stage = PipelineStage.WAKE_WORD if msg.get("start_stage") == "wake_word" else PipelineStage.STT
        self.device.entry.async_create_background_task(
            self.hass,
            self.async_accept_pipeline_from_satellite(
                audio_stream=self._stream(self._audio_queue),
                start_stage=stage,
                end_stage=PipelineStage.TTS,
                wake_word_phrase=msg.get("wake_word_phrase"),
            ),
            f"{self.entity_id} pipeline {self._run_id}",
        )

    @staticmethod
    async def _stream(queue: asyncio.Queue[bytes | None]) -> AsyncGenerator[bytes]:
        while (chunk := await queue.get()) is not None:
            yield chunk

    def _end_audio(self) -> None:
        if self._audio_queue is not None:
            self._audio_queue.put_nowait(None)
            self._audio_queue = None

    def _on_played(self, msg: dict[str, Any]) -> None:
        self._played.set()
        if msg.get("purpose") != "tts":
            return
        if self._tts_deadline_task:
            self._tts_deadline_task.cancel()
        self.tts_response_finished()
        if self._continue_conversation:
            # Assistant asked a follow-up question: reopen the mic, no wake word.
            self._continue_conversation = False
            self._send({"type": "start-listening", "conversation": True})

    # -- HA -> panel -----------------------------------------------------------
    def _send(self, msg: dict[str, Any]) -> None:
        async def _go() -> None:
            try:
                await self.device.async_send(msg)
            except SlateError as err:
                _LOGGER.debug("Dropped %s: %s", msg.get("type"), err)
        self.device.entry.async_create_background_task(self.hass, _go(), f"{self.entity_id} send")

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """Track the turn (TTS token, follow-up question), start TTS streaming, and forward the event to the panel."""
        data = event.data or {}
        out: dict[str, Any] = {}
        if event.type is PipelineEventType.RUN_START:
            self._tts_token = (data.get("tts_output") or {}).get("token")
        elif event.type is PipelineEventType.WAKE_WORD_END:
            out = {"wake_word_output": data.get("wake_word_output") or {}}
        elif event.type is PipelineEventType.INTENT_END:
            intent_output = data.get("intent_output") or {}
            self._continue_conversation = bool(intent_output.get("continue_conversation"))
        elif event.type is PipelineEventType.TTS_END:
            tts_output = data.get("tts_output") or {}
            token = tts_output.get("token") or self._tts_token
            stream = tts.async_get_stream(self.hass, token) if token else None
            out = {"has_audio": stream is not None}
            if stream is not None:
                self.device.entry.async_create_background_task(
                    self.hass, self._stream_tts(stream, self._run_id), f"{self.entity_id} tts")
        elif event.type is PipelineEventType.ERROR:
            out = {"code": data.get("code"), "message": data.get("message")}
        elif event.type is PipelineEventType.RUN_END:
            self._end_audio()
        if event.type in _FORWARD:
            self._send({"type": "pipeline-event", "run_id": self._run_id, "event": str(event.type), "data": out})

    async def _stream_tts(self, stream: tts.ResultStream, run_id: int | None) -> None:
        if stream.extension != "wav":
            _LOGGER.warning("TTS returned %s, expected wav", stream.extension)
            return
        header = b""
        started = time.monotonic()
        seconds = 0.0
        rate = width = channels = 0
        try:
            async for chunk in stream.async_stream_result():
                if not rate:
                    header += chunk
                    parsed = _parse_wav(header)
                    if parsed is None:
                        continue
                    rate, width, channels, chunk = parsed
                    await self.device.async_send({"type": "audio-start", "purpose": "tts", "rate": rate,
                                                  "width": width, "channels": channels, "run_id": run_id})
                for i in range(0, len(chunk), AUDIO_CHUNK_BYTES):
                    await self.device.async_send_binary(CH_SPK, chunk[i:i + AUDIO_CHUNK_BYTES])
                seconds += len(chunk) / (rate * width * channels)
            if rate:
                await self.device.async_send({"type": "audio-stop", "purpose": "tts", "run_id": run_id})
        except SlateError as err:
            _LOGGER.debug("TTS stream aborted: %s", err)
        # If the panel never confirms playback, don't leave the entity "responding".
        remaining = max(0.0, seconds - (time.monotonic() - started)) + TTS_TIMEOUT_EXTRA
        self._tts_deadline_task = self.device.entry.async_create_background_task(
            self.hass, self._tts_deadline(remaining), f"{self.entity_id} tts deadline")

    async def _tts_deadline(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.state == "responding":
            self.tts_response_finished()

    # -- announcements / start conversation ------------------------------------
    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        """Play the announcement and wait for the panel to confirm it (or a timeout); raises ``SlateError`` if disconnected."""
        await self._play_media("announce", announcement)

    async def async_start_conversation(self, start_announcement: AssistSatelliteAnnouncement) -> None:
        """Play the announcement, then open the panel's microphone without a wake word."""
        await self._play_media("announce", start_announcement)
        await self.device.async_command("start-listening", conversation=True)

    async def _play_media(self, purpose: str, announcement: AssistSatelliteAnnouncement) -> None:
        if self._ffmpeg is None:
            self._ffmpeg = ffmpeg.get_ffmpeg_manager(self.hass)
        sources = [m for m in (announcement.preannounce_media_id, announcement.media_id) if m]
        self._played.clear()
        await self.device.async_send({"type": "audio-start", "purpose": purpose, "rate": TTS_SAMPLE_RATE,
                                      "width": 2, "channels": 1})
        total = 0
        try:
            for media in sources:
                proc = await asyncio.create_subprocess_exec(
                    self._ffmpeg.binary, "-protocol_whitelist", "http,https,file,tcp,tls", "-i", media,
                    "-f", "s16le", "-ac", "1", "-ar", str(TTS_SAMPLE_RATE), "-nostats", "pipe:",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, close_fds=False)
                assert proc.stdout is not None
                while chunk := await proc.stdout.read(AUDIO_CHUNK_BYTES):
                    await self.device.async_send_binary(CH_SPK, chunk)
                    total += len(chunk)
                await proc.wait()
        finally:
            await self.device.async_send({"type": "audio-stop", "purpose": purpose})
        try:
            async with asyncio.timeout(total / (TTS_SAMPLE_RATE * 2) + TTS_TIMEOUT_EXTRA):
                await self._played.wait()
        except TimeoutError:
            _LOGGER.debug("No played confirmation for announcement")

    # -- timers ----------------------------------------------------------------
    @callback
    def _handle_timer(self, event_type: intent.TimerEventType, timer: intent.TimerInfo) -> None:
        self._send({
            "type": "timer", "event": str(event_type), "timer_id": timer.id, "name": timer.name,
            "total_seconds": getattr(timer, "created_seconds", timer.seconds),
            "seconds_left": timer.seconds_left, "is_active": timer.is_active,
        })


def _parse_wav(data: bytes) -> tuple[int, int, int, bytes] | None:
    if len(data) < 44:
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getframerate(), w.getsampwidth(), w.getnchannels(), w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None
