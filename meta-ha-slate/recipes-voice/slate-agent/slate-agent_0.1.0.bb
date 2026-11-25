SUMMARY = "HA Slate device agent: Home Assistant voice satellite, custom wake words, device control"
DESCRIPTION = "Runs on-device wake word detection (microWakeWord / openWakeWord, including \
custom models pushed from Home Assistant), streams the Assist pipeline, plays TTS, and \
exposes the panel to the ha_slate Home Assistant integration over a TLS WebSocket."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# Source: apps/slate-agent in this repository. Dependencies: hash-pinned
# aarch64 wheels (scripts/gen-wheels.py) installed into a private prefix.
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../apps:"
SRC_URI = " \
    file://slate-agent/ \
    ${SLATE_WHEELS} \
    file://slate-agent.service \
    file://slate-agent-launch \
    file://slate-kiosk-reload.path \
    file://slate-kiosk-reload.service \
    file://slate-reboot.path \
    file://slate-reboot.service \
"
require slate-agent-wheels.inc

S = "${WORKDIR}/slate-agent"

inherit python3native systemd useradd

DEPENDS = "python3-installer-native"

AGENT_PREFIX = "/opt/slate-agent"
AGENT_SITE = "${AGENT_PREFIX}/lib/python${PYTHON_BASEVERSION}/site-packages"

USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--system --home-dir /data/agent --no-create-home --shell /sbin/nologin \
                       --gid slate --groups audio,pipewire slate"
GROUPADD_PARAM:${PN} = "-r slate; -r pipewire"

SYSTEMD_SERVICE:${PN} = "slate-agent.service slate-kiosk-reload.path slate-reboot.path"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    for whl in ${WORKDIR}/wheels/*.whl; do
        nativepython3 -m installer --no-compile-bytecode --destdir ${D} --prefix ${AGENT_PREFIX} "$whl"
    done
    rm -rf ${D}${AGENT_PREFIX}/bin
    install -d ${D}${AGENT_SITE}
    cp -R --no-preserve=ownership ${S}/slate_agent ${D}${AGENT_SITE}/

    install -D -m 0755 ${WORKDIR}/slate-agent-launch ${D}${libexecdir}/slate-agent-launch
    sed -i -e 's,@AGENT_SITE@,${AGENT_SITE},g' ${D}${libexecdir}/slate-agent-launch
    for unit in slate-agent.service slate-kiosk-reload.path slate-kiosk-reload.service \
                slate-reboot.path slate-reboot.service; do
        install -D -m 0644 ${WORKDIR}/$unit ${D}${systemd_system_unitdir}/$unit
        sed -i -e 's,@LIBEXECDIR@,${libexecdir},g' ${D}${systemd_system_unitdir}/$unit
    done
}

FILES:${PN} += "${AGENT_PREFIX} ${systemd_system_unitdir}"

# Upstream wheels ship stripped binaries plus private libs (TFLite C, OpenBLAS).
INSANE_SKIP:${PN} += "already-stripped ldflags file-rdeps dev-so textrel"
EXCLUDE_FROM_SHLIBS = "1"

RDEPENDS:${PN} = " \
    python3-core \
    python3-modules \
    openssl-bin \
    pipewire-tools \
    slate-audio \
    slate-lightd \
"

COMPATIBLE_HOST = "aarch64.*-linux"
