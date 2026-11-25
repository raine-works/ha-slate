"""Audio capture/playback through PipeWire's ``pw-cat`` (raw PCM on pipes).

Using pw-cat keeps the agent free of native audio bindings: capture reads
s16le 16 kHz mono from the echo-cancelled source, playback writes PCM into the
echo-cancel sink so the canceller always has the reference signal.
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import sys
from typing import Protocol
from collections.abc import AsyncIterator

from .protocol import MIC_RATE

_LOG = logging.getLogger(__name__)

CHUNK_SAMPLES = 320  # 20 ms at 16 kHz
CHUNK_BYTES = CHUNK_SAMPLES * 2


class AudioSource(Protocol):
    def chunks(self) -> AsyncIterator[bytes]: ...
    async def close(self) -> None: ...


class Player(Protocol):
    async def start(self, rate: int, width: int, channels: int, volume: float) -> None: ...
    async def write(self, pcm: bytes) -> None: ...
    async def finish(self) -> None: ...   # drain, then return
    async def abort(self) -> None: ...


class PwCatSource:
    def __init__(self, target: str | None) -> None:
        self.target = target
        self.proc: asyncio.subprocess.Process | None = None

    async def chunks(self) -> AsyncIterator[bytes]:
        args = ["pw-cat", "--record", "--rate", str(MIC_RATE), "--channels", "1",
                "--format", "s16", "--latency", "20ms"]
        if self.target:
            args += ["--target", self.target]
        self.proc = await asyncio.create_subprocess_exec(
            *args, "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        assert self.proc.stdout is not None
        try:
            while True:
                chunk = await self.proc.stdout.readexactly(CHUNK_BYTES)
                yield chunk
        except asyncio.IncompleteReadError:
            _LOG.warning("capture stream ended (pw-cat exit %s)", self.proc.returncode)
        finally:
            await self.close()

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()


class PwCatPlayer:
    def __init__(self, target: str | None) -> None:
        self.target = target
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self, rate: int, width: int, channels: int, volume: float) -> None:
        await self.abort()
        fmt = {1: "u8", 2: "s16", 4: "s32"}[width]
        args = ["pw-cat", "--playback", "--rate", str(rate), "--channels", str(channels),
                "--format", fmt, "--volume", f"{volume:.3f}", "--media-role", "Assistant"]
        if self.target:
            args += ["--target", self.target]
        self.proc = await asyncio.create_subprocess_exec(
            *args, "-", stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def write(self, pcm: bytes) -> None:
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.write(pcm)
            await self.proc.stdin.drain()

    async def finish(self) -> None:
        if self.proc is None:
            return
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        await self.proc.wait()  # pw-cat exits once its buffer has played out
        self.proc = None

    async def abort(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None


class NullPlayer:
    """Records what would have been played (tests, headless dev hosts)."""

    def __init__(self) -> None:
        self.played: list[tuple[int, bytes]] = []
        self._buf = bytearray()
        self._rate = 0

    async def start(self, rate: int, width: int, channels: int, volume: float) -> None:
        self._buf.clear()
        self._rate = rate

    async def write(self, pcm: bytes) -> None:
        self._buf += pcm

    async def finish(self) -> None:
        self.played.append((self._rate, bytes(self._buf)))
        self._buf.clear()

    async def abort(self) -> None:
        self._buf.clear()


class QueueSource:
    """Audio source fed by a test (or a replay file)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def chunks(self) -> AsyncIterator[bytes]:
        while (chunk := await self.queue.get()) is not None:
            yield chunk

    async def close(self) -> None:
        self.queue.put_nowait(None)


def chime(kind: str = "wake", rate: int = 22050) -> bytes:
    """Short synthesized earcons so the image ships no audio assets."""
    notes = {"wake": [(880.0, 0.07), (1318.5, 0.11)],
             "error": [(392.0, 0.12), (261.6, 0.18)],
             "done": [(1318.5, 0.06), (880.0, 0.09)]}[kind]
    out = array.array("h")
    for freq, dur in notes:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / (0.005 * rate), (n - i) / (0.02 * rate))
            out.append(int(0.35 * 32767 * env * math.sin(2 * math.pi * freq * i / rate)))
    if sys.byteorder != "little":  # PCM on the wire is little-endian
        out.byteswap()
    return out.tobytes()
