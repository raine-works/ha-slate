SUMMARY = "HA Slate: base system (networking, time, persistence, lighting)"
LICENSE = "MIT"
PACKAGE_ARCH = "${MACHINE_ARCH}"

inherit packagegroup

RDEPENDS:${PN} = " \
    slate-base \
    slate-lightd \
    ca-certificates \
    tzdata \
    e2fsprogs-mke2fs \
    e2fsprogs-resize2fs \
    kernel-modules \
    ${@bb.utils.contains('DISTRO_FEATURES', 'rauc', 'rauc', '', d)} \
"
