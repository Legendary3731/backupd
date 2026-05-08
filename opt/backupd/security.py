import json
import hmac
import hashlib
import time
import os
import re

CONFIG_DIR = "/opt/backupd/config"
KEYS_FILE = f"{CONFIG_DIR}/keys.json"
CONFIG_FILE = f"{CONFIG_DIR}/config.json"
NONCE_DIR = "/var/lib/backupd/nonces"


# ---------- LOADERS ----------

def load_keys():
    with open(KEYS_FILE) as f:
        return json.load(f)


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ---------- AUTH ----------

def get_key_data(api_key: str):
    keys = load_keys()
    for vmid, data in keys.items():
        if data["key"] == api_key:
            return build_policy(int(vmid))
    return None


# ---------- POLICY ----------

def build_policy(vmid: int):
    cfg = load_config()

    policy = cfg.get("defaults", {}).copy()

    overrides = cfg.get("overrides", {})
    if str(vmid) in overrides:
        policy.update(overrides[str(vmid)])

    policy["vmid"] = vmid
    return policy


# ---------- HMAC ----------

def canonical(payload):
    parts = [
        f"timestamp={payload['timestamp']}",
        f"nonce={payload['nonce']}",
    ]

    if "backup_id" in payload:
        parts.append(f"backup_id={payload['backup_id']}")

    return "&".join(parts)


def verify_hmac(secret, payload, signature):
    if not signature:
        return False

    try:
        msg = canonical(payload).encode()
    except KeyError:
        return False

    calc = hmac.new(
        secret.encode(),
        msg,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(calc, signature)


# ---------- FILE CHECK ----------


BACKUP_RE = re.compile(
    r"^vzdump-(lxc|qemu)-\d{1,6}-\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2}\.(tar\.zst|vma\.zst)$"
)

def validate_bid(file: str, vmid: int) -> str:
    if not BACKUP_RE.fullmatch(file):
        return False
    if f"-{vmid}-" not in file:
        return False
    return True


# ---------- REPLAY / TIME ----------

def check_nonce(nonce):
    if not nonce or not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", nonce):
        return False

    os.makedirs(NONCE_DIR, exist_ok=True)
    path = f"{NONCE_DIR}/{nonce}"

    if os.path.exists(path):
        return False

    open(path, "w").close()

    # Purger les nonces > 120s
    now = time.time()
    for f in os.scandir(NONCE_DIR):
        if now - f.stat().st_mtime > 120:
            os.unlink(f.path)

    return True


def check_timestamp(ts):
    if not ts:
        return False

    return abs(time.time() - ts) < 60