SUMMARY = "HA Slate: PipeWire audio + slate-agent voice satellite"
LICENSE = "MIT"
PACKAGE_ARCH = "${MACHINE_ARCH}"

inherit packagegroup

RDEPENDS:${PN} = " \
    slate-audio \
    slate-agent \
"
