SUMMARY = "HA Slate edge light bar + backlight daemon"
DESCRIPTION = "Drives the integrated edge light bar (WS2812 over SPI or kernel LED-class \
drivers) from voice-pipeline events and exposes it, and the display backlight, to \
Home Assistant as light entities via the voice satellite's peripheral API."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# Source lives in the repo at apps/slate-lightd (single source of truth for
# both the image and host-side unit tests).
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../apps:"
SRC_URI = " \
    file://slate-lightd/ \
    file://slate-lightd.service \
    file://lightd.toml \
"
S = "${WORKDIR}/slate-lightd"

inherit python_setuptools_build_meta systemd

SYSTEMD_SERVICE:${PN} = "slate-lightd.service"

# ws2812-spi | sysfs | console (QEMU) -- set per machine.
SLATE_LIGHTD_BACKEND ?= "ws2812-spi"
PACKAGE_ARCH = "${MACHINE_ARCH}"

do_install:append() {
    install -D -m 0644 ${WORKDIR}/slate-lightd.service ${D}${systemd_system_unitdir}/slate-lightd.service
    install -D -m 0644 ${WORKDIR}/lightd.toml ${D}${sysconfdir}/slate/lightd.toml
    sed -i -e 's/^backend *=.*/backend   = "${SLATE_LIGHTD_BACKEND}"/' ${D}${sysconfdir}/slate/lightd.toml
}

RDEPENDS:${PN} = " \
    python3-asyncio \
    python3-core \
    python3-fcntl \
    python3-io \
    python3-json \
    python3-logging \
    python3-tomllib \
    python3-websockets \
"
