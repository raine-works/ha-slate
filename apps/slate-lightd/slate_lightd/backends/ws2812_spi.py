"""WS2812/SK6812 addressable LEDs driven from an SPI MOSI line.

Bit-banging a single-wire 800 kHz protocol from Linux userspace is not viable
(scheduler jitter > 1 us). Instead each LED data bit is encoded as 4 SPI bits
at 3.2 MHz (312.5 ns per SPI bit), so the SPI controller's DMA generates the
waveform:

    data 0 -> 1000  (T0H 312 ns, T0L 938 ns)
    data 1 -> 1110  (T1H 938 ns, T1L 312 ns)

These timings satisfy both the original WS2812B datasheet and the WS2812B-V5 /
SK6812 tables. A trailing run of zero bytes (>= 300 us low) latches the frame.

Hardware requirements (see docs/SPEC.md "Edge lighting"):
  * MOSI must idle LOW between transfers (true for most controllers in mode 0).
  * 3.3 V -> 5 V level shifter (74AHCT1G125 or similar) on the data line.
  * spidev.bufsiz >= led_count * 12 + 128 bytes.
"""

from __future__ import annotations

import fcntl
import os
import struct

from .base import Backend

# <linux/spi/spidev.h>: _IOW('k', nr, size)
_SPI_IOC_WR_MODE = 0x40016B01
_SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
_SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

_NIBBLE = (0b1000, 0b1110)
RESET_BYTES = 128  # 128 * 8 bits / 3.2 MHz = 320 us


def _byte_table() -> list[bytes]:
    """Pre-compute the 4-byte SPI pattern for every possible colour byte."""
    table = []
    for value in range(256):
        word = 0
        for bit in range(7, -1, -1):
            word = (word << 4) | _NIBBLE[(value >> bit) & 1]
        table.append(word.to_bytes(4, "big"))
    return table


_TABLE = _byte_table()


def encode(frame: list[tuple[int, int, int]], color_order: str = "GRB") -> bytes:
    idx = ["RGB".index(c) for c in color_order.upper()]
    out = bytearray()
    for px in frame:
        for i in idx:
            out += _TABLE[px[i]]
    out += bytes(RESET_BYTES)
    return bytes(out)


class Ws2812SpiBackend(Backend):
    def __init__(self, led_count: int, device: str, speed_hz: int = 3_200_000, color_order: str = "GRB") -> None:
        if sorted(color_order.upper()) != ["B", "G", "R"]:
            raise ValueError("color_order must be a permutation of RGB")
        self.led_count = led_count
        self.color_order = color_order
        self.fd = os.open(device, os.O_WRONLY)
        fcntl.ioctl(self.fd, _SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.fd, _SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self.fd, _SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", speed_hz))

    def write(self, frame: list[tuple[int, int, int]]) -> None:
        os.write(self.fd, encode(frame, self.color_order))

    def close(self) -> None:
        try:
            self.write([(0, 0, 0)] * self.led_count)
        finally:
            os.close(self.fd)
