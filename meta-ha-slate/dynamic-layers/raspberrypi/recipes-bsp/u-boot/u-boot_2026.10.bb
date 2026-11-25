# U-Boot for Compute Module 5 (BCM2712).
#
# poky scarthgap ships U-Boot 2024.01, which predates BCM2712 support (first
# in v2024.04). This recipe provides a current release for CM5 only:
# DEFAULT_PREFERENCE = -1 keeps CM4 on the release's 2024.01, and
# kas/machine/cm5.yml selects this one with PREFERRED_VERSION_u-boot.
#
# Production may replace U-Boot on CM5 with the Raspberry Pi bootloader's
# native "tryboot" A/B (docs/spec.md, roadmap R14).

require recipes-bsp/u-boot/u-boot-common.inc
require recipes-bsp/u-boot/u-boot.inc

DEFAULT_PREFERENCE = "-1"

# v2026.10. Replaces u-boot-common.inc's SRC_URI: its CVE backports target
# 2024.01 and are already contained in this release.
SRCREV = "5508406582f6f7120dce0c4b3059819a41788db2"
# U-Boot develops on "main" since v2026.07 ("master" is frozen there), so a
# later release is only reachable from main.
SRC_URI = "git://source.denx.de/u-boot/u-boot.git;protocol=https;branch=main"
# Licenses/README is unchanged since 2024.01 (md5 verified for v2026.10).
LIC_FILES_CHKSUM = "file://Licenses/README;md5=2ca5f2c35c8cc335f0a19756634782f1"

# meta-raspberrypi's fdt_addr patch is upstream in this release; applying it
# again fails do_patch ("Reversed (or previously applied) patch detected").
SRC_URI:remove = "file://0001-rpi-always-set-fdt_addr-with-firmware-provided-FDT-address.patch"

# Host tools built alongside the target binary (mkeficapsule needs gnutls).
DEPENDS += "bc-native dtc-native python3-pyelftools-native gnutls-native util-linux-native"

COMPATIBLE_MACHINE = "^(raspberrypi5)$"
