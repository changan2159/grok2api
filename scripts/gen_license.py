import sys
import os
import json
import base64
import hashlib
from pathlib import Path

# Try to load config safely
try:
    import tomllib
except ImportError:
    import tomli as tomllib

from cryptography.fernet import Fernet

def get_secret():
    """Read license_secret from config.defaults.toml or config.toml"""
    root_dir = Path(__file__).parent.parent
    
    # Try config.toml first
    config_file = root_dir / "config.toml"
    if not config_file.exists():
        config_file = root_dir / "config.defaults.toml"
        
    if not config_file.exists():
        print("Error: Config file not found.")
        sys.exit(1)
        
    with open(config_file, "rb") as f:
        config = tomllib.load(f)
        
    secret = config.get("app", {}).get("license_secret")
    if not secret or secret == "change_me_to_a_random_string_for_security":
        print("Warning: Using default/empty license_secret. Please configure it in config.toml for security.")
        if not secret:
            secret = "default_unsafe_secret_key_grok2api"
            
    # Derive a 32-byte url-safe base64 key for Fernet
    # We use SHA256 of the secret to ensure it's 32 bytes
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return key

import time

def generate_license(ip_address, days=None):
    key = get_secret()
    f = Fernet(key)
    
    payload = {
        "ip": ip_address,
        "type": "standard",
        "created_at": int(time.time())
    }
    
    if days is not None:
        try:
            days = int(days)
            if days > 0:
                # Calculate expiry timestamp
                expiry = int(time.time()) + (days * 86400)
                payload["exp"] = expiry
        except ValueError:
            pass
    
    token = f.encrypt(json.dumps(payload).encode())
    return token.decode()

def renew_license(old_token, days_to_add=None):
    key = get_secret()
    f = Fernet(key)
    
    try:
        # Decrypt old token
        data = f.decrypt(old_token.encode())
        payload = json.loads(data.decode())
        
        print(f"Original License Info: IP={payload.get('ip')}, Created={time.strftime('%Y-%m-%d', time.localtime(payload.get('created_at')))}")
        if 'exp' in payload:
             print(f"Previous Expiry: {time.strftime('%Y-%m-%d', time.localtime(payload['exp']))}")
        else:
             print("Previous Expiry: Permanent")

        # Update Expiration
        if days_to_add is not None:
            try:
                days = int(days_to_add)
                if days > 0:
                    current_exp = payload.get("exp", int(time.time()))
                    # If expired, start from now; if valid, add to existing expiry
                    base_time = max(current_exp, int(time.time()))
                    new_expiry = base_time + (days * 86400)
                    payload["exp"] = new_expiry
            except ValueError:
                pass
        else:
            # If no days specified, make it permanent (remove exp)
            if "exp" in payload:
                del payload["exp"]

        # Re-encrypt
        new_token = f.encrypt(json.dumps(payload).encode())
        return new_token.decode()
        
    except Exception as e:
        print(f"Error renewing license: {e}")
        return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  New License:   python gen_license.py <IP_ADDRESS> [DAYS]")
        print("  Renew License: python gen_license.py renew <OLD_LICENSE_KEY> [DAYS_TO_ADD]")
        sys.exit(1)
        
    if sys.argv[1] == "renew":
        if len(sys.argv) < 3:
             print("Error: Please provide the old license key.")
             sys.exit(1)
             
        old_key = sys.argv[2]
        days = sys.argv[3] if len(sys.argv) > 3 else None
        
        new_key = renew_license(old_key, days)
        if new_key:
            print(f"\nRenewed License Key:")
            print("-" * 50)
            print(new_key)
            print("-" * 50)
    else:
        ip = sys.argv[1]
        days = sys.argv[2] if len(sys.argv) > 2 else None
        
        license_key = generate_license(ip, days)
        print(f"\nLicense Key for IP {ip}:")
        if days:
            print(f"Validity: {days} days")
        else:
            print(f"Validity: Permanent")
        print("-" * 50)
        print(license_key)
        print("-" * 50)
        print("\nCopy this key to your application configuration.")
