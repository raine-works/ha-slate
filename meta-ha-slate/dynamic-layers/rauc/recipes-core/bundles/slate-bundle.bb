SUMMARY = "HA Slate RAUC update bundle"
LICENSE = "MIT"

inherit bundle

RAUC_BUNDLE_COMPATIBLE = "ha-slate-cm4"
RAUC_BUNDLE_VERSION = "${DISTRO_VERSION}"
RAUC_BUNDLE_FORMAT = "verity"
RAUC_BUNDLE_SLOTS = "rootfs"
RAUC_SLOT_rootfs = "slate-image"
RAUC_SLOT_rootfs[fstype] = "ext4"

# Signing material is never committed. Development defaults come from
# scripts/gen-dev-keys.sh; CI overrides both (RAUC_KEY_FILE may be PKCS#11).
RAUC_KEY_FILE ?= "${SLATE_KEYS_DIR}/slate-ca.key.pem"
RAUC_CERT_FILE ?= "${SLATE_KEYS_DIR}/slate-ca.cert.pem"
