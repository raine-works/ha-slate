SUMMARY = "HA Slate: PipeWire audio + Linux Voice Assistant satellite"
LICENSE = "MIT"
PACKAGE_ARCH = "${MACHINE_ARCH}"

inherit packagegroup

RDEPENDS:${PN} = " \
    slate-audio \
    linux-voice-assistant \
"
