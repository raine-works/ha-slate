SUMMARY = "Linux Voice Assistant: Home Assistant voice satellite (ESPHome protocol)"
DESCRIPTION = "Local wake-word detection (microWakeWord / openWakeWord) and Assist \
pipeline streaming to Home Assistant over the ESPHome native API. Installed as a \
self-contained tree in /opt/lva with hash-pinned upstream wheels for its native deps."
HOMEPAGE = "https://github.com/OHF-Voice/linux-voice-assistant"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE.md;md5=86d3f3a95c324c9479bd8986968f4327"

# Pin to an exact upstream commit; bump together with lva-wheels.inc
# (scripts/gen-lva-wheels.py) and re-run the voice acceptance tests.
SRC_URI = " \
    git://github.com/OHF-Voice/linux-voice-assistant.git;protocol=https;branch=main;name=lva \
    ${LVA_WHEELS} \
    file://slate-voice.service \
    file://slate-voice-launch \
    file://voice.env \
"
SRCREV_lva = "b8e34882da8fa2a7f6b6b34309ef56ca96345dcb"
SRCREV_FORMAT = "lva"
PV = "0+git${SRCPV}"
S = "${WORKDIR}/git"

require lva-wheels.inc

inherit python3native systemd useradd

DEPENDS = "python3-installer-native"

LVA_PREFIX = "/opt/lva"
LVA_SITE = "${LVA_PREFIX}/lib/python${PYTHON_BASEVERSION}/site-packages"

USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--system --home-dir /data/voice --no-create-home --shell /sbin/nologin \
                       --user-group --groups audio,pipewire voice"
GROUPADD_PARAM:${PN} = "-r pipewire"

SYSTEMD_SERVICE:${PN} = "slate-voice.service"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    # 1. Pinned dependency wheels -> private site-packages. No compilation:
    #    every wheel is a manylinux aarch64 build checked by sha256 at fetch.
    for whl in ${WORKDIR}/wheels/*.whl; do
        nativepython3 -m installer --no-compile-bytecode \
            --destdir ${D} --prefix ${LVA_PREFIX} "$whl"
    done
    # installer puts console scripts in ${LVA_PREFIX}/bin; we do not use them.
    rm -rf ${D}${LVA_PREFIX}/bin

    # 2. LVA itself (application tree: package + sounds + bundled wake words).
    install -d ${D}${LVA_PREFIX}/app
    cp -R --no-preserve=ownership ${S}/linux_voice_assistant ${S}/sounds ${S}/wakewords ${D}${LVA_PREFIX}/app/
    cp ${S}/version.txt ${D}${LVA_PREFIX}/app/ 2>/dev/null || true

    # 3. Service integration.
    install -D -m 0644 ${WORKDIR}/slate-voice.service ${D}${systemd_system_unitdir}/slate-voice.service
    install -D -m 0755 ${WORKDIR}/slate-voice-launch ${D}${libexecdir}/slate-voice-launch
    install -D -m 0644 ${WORKDIR}/voice.env ${D}${sysconfdir}/slate/voice.env
    sed -i -e 's,@LVA_PREFIX@,${LVA_PREFIX},g' -e 's,@LVA_SITE@,${LVA_SITE},g' \
        ${D}${libexecdir}/slate-voice-launch
    sed -i -e 's,@LIBEXECDIR@,${libexecdir},g' ${D}${systemd_system_unitdir}/slate-voice.service
}

FILES:${PN} += "${LVA_PREFIX} ${systemd_system_unitdir}"

# Upstream wheels are already stripped, carry their own bundled .so files
# (libtensorflowlite_c, numpy's OpenBLAS) and were not linked with our LDFLAGS.
INSANE_SKIP:${PN} += "already-stripped ldflags file-rdeps dev-so textrel"
# Do not export wheel-internal libraries (libtensorflowlite_c, OpenBLAS) as image-wide shlib providers.
EXCLUDE_FROM_SHLIBS = "1"

RDEPENDS:${PN} = " \
    python3-core \
    python3-modules \
    libpulse \
    libmpv \
    ldconfig \
    slate-audio \
"

COMPATIBLE_HOST = "aarch64.*-linux"
