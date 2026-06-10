"""License helpers for deployments that enable license checks."""

import base64
import datetime as _dt
import hashlib
import json
import os
import socket
import time

from cryptography.fernet import Fernet

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger


_DEFAULT_SECRET = "default_unsafe_secret_key_grok2api"


def _license_secret() -> str:
    return (
        os.getenv("LICENSE_SECRET")
        or get_config("app.license_secret", "")
        or _DEFAULT_SECRET
    )


def get_fernet_key() -> bytes:
    """Derive the Fernet key used by scripts/gen_license.py."""
    return base64.urlsafe_b64encode(hashlib.sha256(_license_secret().encode()).digest())


def get_server_ips() -> set[str]:
    """Return locally detectable IP addresses plus configured public IP."""
    ips = {"127.0.0.1", "0.0.0.0", os.getenv("SERVER_HOST", "0.0.0.0")}
    public_ip = os.getenv("PUBLIC_IP")
    if public_ip:
        ips.add(public_ip)

    try:
        hostname = socket.gethostname()
        ips.add(socket.gethostbyname(hostname))
    except Exception as exc:
        logger.debug("license hostname IP detection failed: error={}", exc)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ips.add(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception as exc:
        logger.debug("license outbound IP detection failed: error={}", exc)

    return {ip for ip in ips if ip}


def _decode_license_payload(license_key: str) -> dict:
    fernet = Fernet(get_fernet_key())
    payload_bytes = fernet.decrypt(license_key.encode(), ttl=None)
    payload = json.loads(payload_bytes.decode())
    if not isinstance(payload, dict):
        raise ValueError("license payload is not an object")
    return payload


def verify_license() -> bool:
    """Return whether the configured license key is present and valid."""
    license_key = get_config("app.license_key", "")
    if not license_key:
        logger.warning("license key is not configured")
        return False

    try:
        payload = _decode_license_payload(str(license_key))
        authorized_ip = str(payload.get("ip") or "")
        if not authorized_ip:
            logger.error("license payload missing ip")
            return False

        expires_at = payload.get("exp")
        if expires_at and int(time.time()) > int(expires_at):
            logger.error("license expired: exp={} now={}", expires_at, int(time.time()))
            return False

        server_ips = get_server_ips()
        if authorized_ip != "*" and authorized_ip not in server_ips:
            logger.warning(
                "license IP mismatch: authorized_ip={} server_ips={}",
                authorized_ip,
                sorted(server_ips),
            )
            return False

        return True
    except Exception as exc:
        logger.error("license verification failed: error={}", exc)
        return False


def get_license_status() -> dict:
    """Return a UI-friendly license status payload."""
    license_key = get_config("app.license_key", "")
    if not license_key:
        return {"is_valid": False, "message": "No license configured"}

    try:
        payload = _decode_license_payload(str(license_key))
        authorized_ip = str(payload.get("ip") or "")
        expires_at = payload.get("exp")
        expiry = "Permanent"
        expired = False
        if expires_at:
            exp_int = int(expires_at)
            expiry = _dt.datetime.fromtimestamp(exp_int).strftime("%Y-%m-%d %H:%M:%S")
            expired = int(time.time()) > exp_int

        server_ips = get_server_ips()
        ip_valid = authorized_ip == "*" or authorized_ip in server_ips

        if expired:
            return {
                "is_valid": False,
                "ip": authorized_ip,
                "expiry": expiry,
                "message": f"Expired at {expiry}",
            }
        if not ip_valid:
            return {
                "is_valid": False,
                "ip": authorized_ip,
                "expiry": expiry,
                "message": f"IP mismatch: authorized {authorized_ip}",
            }
        return {
            "is_valid": True,
            "ip": authorized_ip,
            "expiry": expiry,
            "message": "Active",
        }
    except Exception as exc:
        return {"is_valid": False, "message": f"Invalid license: {exc}"}


__all__ = [
    "get_fernet_key",
    "get_license_status",
    "get_server_ips",
    "verify_license",
]
