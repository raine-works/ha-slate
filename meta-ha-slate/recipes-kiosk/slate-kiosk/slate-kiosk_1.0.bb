SUMMARY = "HA Slate kiosk browser service (Cog / WPE WebKit on Weston)"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://slate-kiosk.service \
    file://slate-kiosk-launch \
    file://kiosk.env \
"
S = "${WORKDIR}"

inherit systemd useradd allarch

USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--system --home-dir /data/kiosk --no-create-home --shell /sbin/nologin \
                       --user-group --groups wayland,render,video kiosk"
GROUPADD_PARAM:${PN} = "-r wayland; -r render"

SYSTEMD_SERVICE:${PN} = "slate-kiosk.service"

do_install() {
    install -D -m 0644 ${S}/slate-kiosk.service ${D}${systemd_system_unitdir}/slate-kiosk.service
    install -D -m 0755 ${S}/slate-kiosk-launch ${D}${libexecdir}/slate-kiosk-launch
    install -D -m 0644 ${S}/kiosk.env ${D}${sysconfdir}/slate/kiosk.env
    sed -i -e 's,@LIBEXECDIR@,${libexecdir},g' ${D}${systemd_system_unitdir}/slate-kiosk.service
}

RDEPENDS:${PN} = "cog weston-init"
