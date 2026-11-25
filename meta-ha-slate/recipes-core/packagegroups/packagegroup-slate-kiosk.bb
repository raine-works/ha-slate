SUMMARY = "HA Slate: Weston kiosk-shell + Cog/WPE WebKit browser"
LICENSE = "MIT"
PACKAGE_ARCH = "${MACHINE_ARCH}"

inherit packagegroup

RDEPENDS:${PN} = " \
    weston \
    weston-init \
    cog \
    slate-kiosk \
    liberation-fonts \
    ttf-dejavu-sans \
    ttf-noto-emoji-color \
"
