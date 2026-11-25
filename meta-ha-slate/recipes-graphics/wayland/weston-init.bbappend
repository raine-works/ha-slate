# Replace poky's sample weston.ini with the kiosk configuration.
FILESEXTRAPATHS:prepend := "${THISDIR}/weston-init:"

# Per-board display orientation (SLATE_DISPLAY_* in the distro / kas machine).
# Rotating here, not in the DT overlay, keeps touch coordinates consistent:
# Weston maps touch through the same output transform.
do_install:append() {
    if [ -n "${SLATE_DISPLAY_OUTPUT}" ]; then
        printf '\n[output]\nname=%s\ntransform=%s\n' "${SLATE_DISPLAY_OUTPUT}" "${SLATE_DISPLAY_TRANSFORM}" \
            >> ${D}${sysconfdir}/xdg/weston/weston.ini
    fi
}
