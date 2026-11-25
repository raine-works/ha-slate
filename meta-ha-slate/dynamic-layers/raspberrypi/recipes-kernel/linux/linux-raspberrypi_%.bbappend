# HA Slate kernel fragments on the Raspberry Pi BSP (CM4 and CM5).
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../../recipes-kernel/linux/files:${THISDIR}/files:"
SRC_URI += " \
    file://slate-system.cfg \
    file://slate-display.cfg \
    file://slate-audio.cfg \
    file://slate-leds.cfg \
    file://slate-hardening.cfg \
"
SRC_URI:append:raspberrypi4-64 = " file://slate-bcm2711.cfg"
SRC_URI:append:raspberrypi5 = " file://slate-bcm2712.cfg"
