# HA Slate kernel fragments (QEMU / any linux-yocto based BSP).
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"
SRC_URI += " \
    file://slate-system.cfg \
    file://slate-display.cfg \
    file://slate-audio.cfg \
    file://slate-leds.cfg \
    file://slate-hardening.cfg \
"
