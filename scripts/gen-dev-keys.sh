#!/bin/sh
# Create a throw-away RAUC signing CA for development builds in keys/dev/.
# keys/ is gitignored. Production keys live in an HSM / CI secret store and
# are passed to the build via SLATE_KEYS_DIR (cert) and RAUC_KEY_FILE (key or
# PKCS#11 URI); see docs/spec.md "Updates & signing".
set -eu
dir="$(cd "$(dirname "$0")/.." && pwd)/keys/dev"
mkdir -p "$dir"
[ -f "$dir/slate-ca.key.pem" ] && { echo "keys already exist in $dir"; exit 0; }
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
    -keyout "$dir/slate-ca.key.pem" -out "$dir/slate-ca.cert.pem" \
    -subj "/O=HA Slate/CN=HA Slate DEVELOPMENT update CA"
chmod 600 "$dir/slate-ca.key.pem"
echo "development signing CA written to $dir"
