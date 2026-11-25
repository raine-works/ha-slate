# HA Slate kernel fragments on the Raspberry Pi CM4 reference BSP.
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../../recipes-kernel/linux/files:${THISDIR}/files:"
SRC_URI += " \
    file://slate-system.cfg \
    file://slate-display.cfg \
    file://slate-audio.cfg \
    file://slate-leds.cfg \
    file://slate-hardening.cfg \
    file://slate-rpi.cfg \
"
