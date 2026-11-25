SUMMARY = "HA Slate audio stack: system-wide PipeWire + WirePlumber + echo cancellation"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://50-slate-aec.conf \
    file://50-slate-system.conf \
    file://slate-audio.target \
"
S = "${WORKDIR}"

inherit systemd allarch

SYSTEMD_SERVICE:${PN} = "slate-audio.target"

do_install() {
    install -D -m 0644 ${S}/50-slate-aec.conf ${D}${sysconfdir}/pipewire/pipewire.conf.d/50-slate-aec.conf
    install -D -m 0644 ${S}/50-slate-system.conf ${D}${sysconfdir}/wireplumber/wireplumber.conf.d/50-slate-system.conf
    install -D -m 0644 ${S}/slate-audio.target ${D}${systemd_system_unitdir}/slate-audio.target
    # WirePlumber's recipe installs its system unit but does not enable it.
    install -d ${D}${sysconfdir}/systemd/system/slate-audio.target.wants
    ln -sf ${systemd_system_unitdir}/wireplumber.service \
        ${D}${sysconfdir}/systemd/system/slate-audio.target.wants/wireplumber.service
}

FILES:${PN} += "${systemd_system_unitdir}"

RDEPENDS:${PN} = " \
    pipewire \
    pipewire-alsa \
    pipewire-modules-echo-cancel \
    pipewire-spa-plugins-aec-webrtc \
    pipewire-spa-plugins-alsa \
    pipewire-spa-plugins-support \
    pipewire-spa-plugins-audioconvert \
    pipewire-spa-plugins-audiomixer \
    pipewire-tools \
    wireplumber \
"
