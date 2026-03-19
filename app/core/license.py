import os
import json
import base64
import hashlib
import socket
import logging
from cryptography.fernet import Fernet
from app.core.config import get_config

logger = logging.getLogger("grok2api.license")

def get_fernet_key():
    """Derive Fernet key from config secret"""
    secret = get_config("app.license_secret")
    if not secret:
        # Fallback for when config isn't fully loaded yet or empty
        secret = "default_unsafe_secret_key_grok2api"
        
    # Must match the generation script logic
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())

def get_server_ips():
    """Get all IP addresses associated with the server"""
    ips = set()
    try:
        # Hostname IPs
        hostname = socket.gethostname()
        ips.add(socket.gethostbyname(hostname))
        
        # Try to get all interface IPs
        # This is platform dependent, detailed implementation might require netifaces or psutil
        # For simplicity/standard lib, we try DNS resolution of hostname
        # and standard localhost
        ips.add("127.0.0.1")
        ips.add("0.0.0.0")
        
        # In a real production deployment inside Docker, the internal IP might be 172.x.x.x
        # Users might want to authorize the EXTERNAL IP. 
        # But the app only knows its local interfaces.
        # Strict IP binding usually implies binding to the interface IP.
        
        # If the user provides an IP in license, we check if:
        # 1. It matches one of local interfaces
        # 2. OR we just trust it (if we can't reliably detect all interfaces without deps)
        
        # Let's try to get more IPs via socket connection trick (doesn't send data)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
            
    except Exception as e:
        logger.warning(f"Failed to detect server IPs: {e}")
        
    return ips

def verify_license() -> bool:
    """
    Verify the current license key.
    
    Returns:
        True if valid, False otherwise.
    """
    license_key = get_config("app.license_key")
    if not license_key:
        logger.warning("No license key found.")
        return False
        
    try:
        key = get_fernet_key()
        f = Fernet(key)
        
        # Decrypt
        payload_bytes = f.decrypt(license_key.encode(), ttl=None)
        payload = json.loads(payload_bytes.decode())
        
        authorized_ip = payload.get("ip")
        if not authorized_ip:
            logger.error("License payload missing IP.")
            return False

        # Check Expiry
        import time
        exp = payload.get("exp")
        if exp:
            now = int(time.time())
            if now > exp:
                logger.error(f"License expired. Expiry: {exp}, Now: {now}")
                return False
            
        # Verify IP
        server_ips = get_server_ips()
        
        # Also allow verification if the configured 'host' matches
        # e.g. if started with --host 1.2.3.4
        configured_host = os.getenv("SERVER_HOST", "0.0.0.0")
        server_ips.add(configured_host)
        
        # Support manual Public IP specification via env var (for Docker/NAT)
        public_ip = os.getenv("PUBLIC_IP")
        if public_ip:
            server_ips.add(public_ip)

        if authorized_ip not in server_ips and authorized_ip != "*":
            # If authorized IP is 0.0.0.0, we might allow it as "any match" or "generic" depending on policy.
            # Here we enforce strict match.
            # But let's be generous: if authorized_ip is "127.0.0.1", it only runs correctly locally.
            # If authorized_ip is public IP, but server is behind NAT (docker), server sees internal IP.
            # This is a common issue. 
            # Strategy: We check if the authorized_ip is present in the detected local IPs.
            
            logger.warning(f"License IP mismatch. License for: {authorized_ip}, Server has: {server_ips}")
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"License verification failed: {e}")
        return False

def get_license_status() -> dict:
    """
    Get detailed license status for UI display.
    """
    license_key = get_config("app.license_key")
    if not license_key:
        return {"is_valid": False, "message": "无许可证 (No License Found)"}
        
    try:
        key = get_fernet_key()
        f = Fernet(key)
        
        # Decrypt
        payload_bytes = f.decrypt(license_key.encode(), ttl=None)
        payload = json.loads(payload_bytes.decode())
        
        authorized_ip = payload.get("ip")
        exp = payload.get("exp")
        
        # Check Expiry
        import time
        import datetime
        
        expiry_str = "永久 (Permanent)"
        is_expired = False
        if exp:
            now = int(time.time())
            expiry_dt = datetime.datetime.fromtimestamp(exp)
            expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S")
            if now > exp:
                is_expired = True

        # Check IP
        server_ips = get_server_ips()
        configured_host = os.getenv("SERVER_HOST", "0.0.0.0")
        server_ips.add(configured_host)
        public_ip = os.getenv("PUBLIC_IP")
        if public_ip:
            server_ips.add(public_ip)
            
        ip_auth = True
        if authorized_ip not in server_ips and authorized_ip != "*":
           ip_auth = False
           
        if is_expired:
            return {
                "is_valid": False,
                "ip": authorized_ip,
                "expiry": expiry_str,
                "message": f"已过期 (Expired at {expiry_str})"
            }
            
        if not ip_auth:
             return {
                "is_valid": False,
                "ip": authorized_ip,
                "expiry": expiry_str,
                "message": f"IP 不匹配 (Authorized: {authorized_ip})"
            }
            
        return {
            "is_valid": True,
            "ip": authorized_ip,
            "expiry": expiry_str,
            "message": "已激活 (Active)"
        }

    except Exception as e:
        return {"is_valid": False, "message": f"无效许可证 ({str(e)})"}
