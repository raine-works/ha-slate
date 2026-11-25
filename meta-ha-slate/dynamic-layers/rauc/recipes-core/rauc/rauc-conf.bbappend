# HA Slate RAUC system configuration + update-signing CA.
# The update-signing CA certificate is not in the layer: it comes from
# SLATE_KEYS_DIR (development: keys/dev from scripts/gen-dev-keys.sh;
# production: the release CA, injected by CI).
FILESEXTRAPATHS:prepend := "${SLATE_KEYS_DIR}:${THISDIR}/files:"
RAUC_KEYRING_FILE = "slate-ca.cert.pem"

# /etc/fw_env.config comes from the BSP's u-boot-env package (meta-raspberrypi
# ships "/boot/uboot.env 0x0000 0x4000"); installing our own copy would collide.
SRC_URI += " \
    file://slate-rauc-hook \
    file://10-slate-mark-good.conf \
"

do_install:append() {
    install -D -m 0755 ${WORKDIR}/slate-rauc-hook ${D}${libexecdir}/slate-rauc-hook
    install -D -m 0644 ${WORKDIR}/10-slate-mark-good.conf \
        ${D}${sysconfdir}/systemd/system/rauc-mark-good.service.d/10-slate.conf
}

FILES:${PN} += "${libexecdir} ${sysconfdir}/systemd/system/rauc-mark-good.service.d"
RDEPENDS:${PN} += "libubootenv-bin u-boot-env"
