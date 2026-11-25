"""Device identity, TLS certificate and pairing token management."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import ssl
import subprocess
import time
import uuid
from pathlib import Path

_LOG = logging.getLogger(__name__)

PAIRING_WINDOW_S = 15 * 60
MAX_FAILURES = 5
LOCKOUT_S = 60.0


def device_id(state_dir: Path, iface_root: str = "/sys/class/net") -> str:
    """Stable id: MAC of the first wired interface, else a stored random id.

    A MAC survives factory reset, so Home Assistant keeps the same device
    (and its automations) after /data is wiped.
    """
    try:
        for name in sorted(os.listdir(iface_root)):
            if name == "lo" or name.startswith(("wl", "docker", "veth", "br")):
                continue
            with open(os.path.join(iface_root, name, "address")) as f:
                mac = f.read().strip().lower()
            if mac and mac != "00:00:00:00:00:00":
                return mac.replace(":", "")
    except OSError:
        pass
    path = state_dir / "device_id"
    if path.exists():
        return path.read_text().strip()
    new = uuid.uuid4().hex[:12]
    _write_private(path, new)
    return new


def ensure_certificate(state_dir: Path, common_name: str) -> tuple[Path, Path, str]:
    """Create (once) a self-signed P-256 certificate; return paths + SHA-256 fingerprint."""
    cert, key = state_dir / "tls.crt", state_dir / "tls.key"
    if not (cert.exists() and key.exists()):
        state_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-nodes", "-days", "36500", "-subj", f"/CN={common_name}",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
        os.chmod(key, 0o600)
        _LOG.info("generated TLS certificate for %s", common_name)
    return cert, key, fingerprint(cert.read_text())


def fingerprint(cert_pem: str) -> str:
    """SHA-256 of the certificate's DER encoding, as hex: what Home Assistant pins at pairing."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest()


def server_ssl_context(cert: Path, key: Path) -> ssl.SSLContext:
    """TLS server context for the agent's port: TLS 1.2 or later, serving ``cert`` with ``key``.

    Raises ``ssl.SSLError`` or ``OSError`` if the pair can't be loaded.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


class Pairing:
    """Long-term token shared with exactly one Home Assistant instance.

    Pairing is accepted when either
      * a factory pairing code exists (printed on the device label, stored in
        ``pairing_code_file``) and the client presents it, or
      * no code is provisioned and the device is unpaired and inside the
        pairing window (first 15 min after the agent starts).
    Failed attempts are rate limited. A factory reset (wiping /data) unpairs.
    """

    def __init__(self, token_file: Path, pairing_code_file: Path | None = None, window_s: float = PAIRING_WINDOW_S) -> None:
        self.token_file = token_file
        self.code_file = pairing_code_file
        self.window_end = time.monotonic() + window_s
        self._failures = 0
        self._locked_until = 0.0

    @property
    def token(self) -> str | None:
        try:
            return self.token_file.read_text().strip() or None
        except OSError:
            return None

    @property
    def paired(self) -> bool:
        return self.token is not None

    def _code(self) -> str | None:
        if self.code_file and self.code_file.exists():
            return self.code_file.read_text().strip() or None
        return None

    def try_pair(self, code: str | None, authenticated: bool = False) -> tuple[str | None, str | None]:
        """Return (token, None) on success or (None, reason)."""
        now = time.monotonic()
        if now < self._locked_until:
            return None, "locked"
        if not authenticated:
            if self.paired:
                return None, "already_paired"
            expected = self._code()
            if expected is not None:
                if code is None or not hmac.compare_digest(expected, str(code)):
                    self._failures += 1
                    if self._failures >= MAX_FAILURES:
                        self._locked_until = now + LOCKOUT_S
                        self._failures = 0
                    return None, "invalid_code"
            elif now > self.window_end:
                return None, "pairing_window_closed"
        token = secrets.token_urlsafe(32)
        _write_private(self.token_file, token)
        self._failures = 0
        return token, None

    def unpair(self) -> None:
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)
