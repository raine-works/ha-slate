SUMMARY = "HA Slate production image: Weston kiosk + voice satellite + edge light"
LICENSE = "MIT"

inherit core-image

IMAGE_FEATURES += "read-only-rootfs"

IMAGE_INSTALL = " \
    packagegroup-core-boot \
    packagegroup-slate-base \
    packagegroup-slate-kiosk \
    packagegroup-slate-voice \
    ${CORE_IMAGE_EXTRA_INSTALL} \
"

IMAGE_LINGUAS = ""
SYSTEMD_DEFAULT_TARGET = "graphical.target"

# /data: persistent, label-mounted, grown to fill the medium on first boot.
# nofail + short timeout so a missing partition (QEMU) falls back to tmpfs
# via slate-data-fallback.service instead of hanging boot.
add_data_fstab() {
    echo "LABEL=slate-data  /data  ext4  noatime,nodev,nosuid,nofail,x-systemd.device-timeout=5s,x-systemd.growfs  0  2" \
        >> ${IMAGE_ROOTFS}${sysconfdir}/fstab
}
ROOTFS_POSTPROCESS_COMMAND += "add_data_fstab; "
