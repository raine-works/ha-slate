SUMMARY = "HA Slate base configuration: /data persistence, networking, watchdog, journald"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://10-slate-watchdog.conf \
    file://10-slate-journald.conf \
    file://10-slate-mdns.conf \
    file://20-wired.network \
    file://slate-data-fallback.service \
    file://slate-net-overrides.service \
    file://slate-tmpfiles.conf \
    file://slate.env \
"
S = "${WORKDIR}"

inherit systemd allarch

SYSTEMD_SERVICE:${PN} = "slate-data-fallback.service slate-net-overrides.service"

do_install() {
    install -d ${D}${sysconfdir}/systemd/system.conf.d ${D}${sysconfdir}/systemd/journald.conf.d \
               ${D}${sysconfdir}/systemd/resolved.conf.d ${D}${sysconfdir}/systemd/network \
               ${D}${systemd_system_unitdir} ${D}${nonarch_libdir}/tmpfiles.d ${D}${sysconfdir}/slate \
               ${D}/data
    install -m 0644 ${S}/10-slate-watchdog.conf ${D}${sysconfdir}/systemd/system.conf.d/
    install -m 0644 ${S}/10-slate-journald.conf ${D}${sysconfdir}/systemd/journald.conf.d/
    install -m 0644 ${S}/10-slate-mdns.conf ${D}${sysconfdir}/systemd/resolved.conf.d/
    install -m 0644 ${S}/20-wired.network ${D}${sysconfdir}/systemd/network/
    install -m 0644 ${S}/slate-data-fallback.service ${S}/slate-net-overrides.service ${D}${systemd_system_unitdir}/
    install -m 0644 ${S}/slate-tmpfiles.conf ${D}${nonarch_libdir}/tmpfiles.d/slate.conf
    install -m 0644 ${S}/slate.env ${D}${sysconfdir}/slate/slate.env
}

FILES:${PN} += "/data ${nonarch_libdir}/tmpfiles.d ${systemd_system_unitdir}"
RDEPENDS:${PN} = "systemd"
