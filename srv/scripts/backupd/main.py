from fastapi import FastAPI, Header, HTTPException, Request
import time
import os
import threading
import ipaddress
from glob import glob

from security import (
    get_key_data as get_key,
    check_timestamp,
    check_nonce,
    verify_hmac,
    validate_bid,
)

from state import load, save
from worker import job_queue, start_worker
from logger import get_logger

app = FastAPI()

log = get_logger("api")

LAST_ACTION = {}
COOLDOWN_LOCK = threading.Lock()

@app.on_event("startup")
def start_worker_event():
    start_worker()


def get_ip(request: Request) -> str:
    # Supporte X-Forwarded-For si derrière un reverse proxy
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host


def check_cooldown(vmid: int, cooldown: int):
    now = time.time()
    with COOLDOWN_LOCK:
        last = LAST_ACTION.get(vmid, 0)
        remaining = int(cooldown - (now - last))
        if remaining > 0:
            return False, remaining
        LAST_ACTION[vmid] = now
    return True, 0


# ============================
# AUTH HELPERS
# ============================

def auth_read(x_key: str, timestamp: int, request: Request):
    ip = get_ip(request)

    if not x_key:
        log.warning(f"ip={ip} auth=fail reason=missing_key")
        raise HTTPException(403, "missing key")

    key_data = get_key(x_key)
    if not key_data:
        log.warning(f"ip={ip} auth=fail reason=invalid_key")
        raise HTTPException(403, "invalid key")

    if not check_timestamp(timestamp):
        log.warning(f"ip={ip} vmid={key_data['vmid']} auth=fail reason=timestamp")
        raise HTTPException(403, "timestamp")

    allowed_networks = key_data.get("allowed_networks", [])
    if allowed_networks:
        try:
            client_ip = ipaddress.ip_address(ip)
        except ValueError:
            log.warning(f"ip={ip} vmid={key_data['vmid']} auth=fail reason=invalid_ip")
            raise HTTPException(403, "invalid ip")

        allowed = False
        for net in allowed_networks:
            try:
                if client_ip in ipaddress.ip_network(net):
                    allowed = True
                    break
            except ValueError:
                continue

        if not allowed:
            log.warning(f"ip={ip} vmid={key_data['vmid']} auth=fail reason=network_restricted")
            raise HTTPException(403, "network restricted")

    return key_data


def auth_write(x_key: str, payload: dict, request: Request):
    ip = get_ip(request)
    key_data = auth_read(x_key, payload.get("timestamp"), request)
    vmid = key_data["vmid"]

    if not check_nonce(payload.get("nonce")):
        log.warning(f"ip={ip} vmid={vmid} auth=fail reason=replay")
        raise HTTPException(403, "replay")

    if not verify_hmac(x_key, payload, payload.get("signature")):
        log.warning(f"ip={ip} vmid={vmid} auth=fail reason=invalid_signature")
        raise HTTPException(403, "signature")

    cooldown_ok, remaining = check_cooldown(vmid, key_data["cooldown"])
    if not cooldown_ok:
        log.warning(f"ip={ip} vmid={vmid} auth=fail reason=cooldown remaining={remaining}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "cooldown",
                "message": "Trop de requêtes. Veuillez réessayer plus tard.",
                "remaining_seconds": remaining,
                "cooldown": key_data["cooldown"],
            }
        )

    log.info(f"ip={ip} vmid={vmid} auth=ok")
    return key_data


# ============================
# LIST
# ============================
@app.get("/backups")
def list_backups(
    request: Request,
    x_key: str = Header(None),
    timestamp: int = 0
):
    key_data = auth_read(x_key, timestamp, request)
    vmid = key_data["vmid"]
    ip = get_ip(request)

    log.info(f"ip={ip} vmid={vmid} action=list")

    state = load(vmid)

    dump_files = (
        glob(f"/var/lib/vz/dump/vzdump-lxc-{vmid}-*.tar.zst") +
        glob(f"/var/lib/vz/dump/vzdump-qemu-{vmid}-*.vma.zst")
    )

    fs_files = {os.path.basename(path): path for path in dump_files}
    state_backups = {b["file"]: b for b in state.get("backups", [])}
    new_backups = []

    for fname, b in state_backups.items():
        if fname in fs_files:
            new_backups.append(b)

    for fname, path in fs_files.items():
        if fname not in state_backups:
            new_backups.append({
                "file": fname,
                "date": os.path.getmtime(path),
                "size": os.path.getsize(path),
            })

    state["backups"] = sorted(new_backups, key=lambda b: b["date"])
    save(vmid, state)

    total_size = sum(b["size"] for b in state["backups"])
    result = dict(state)
    result["count"] = len(state["backups"])
    result["max_backups"] = key_data.get("max_backups", 0)
    result["total_size_mb"] = round(total_size / 1024 / 1024, 2)
    result["total_backups_size"] = key_data.get("total_backups_size", 0)
    result["quota"] = {
        "max_backups": key_data.get("max_backups", 0),
        "total_backups_size": key_data.get("total_backups_size", 0),
    }

    return result


# ============================
# STATUS
# ============================
@app.get("/backups/status")
def status(
    request: Request,
    x_key: str = Header(None),
    timestamp: int = 0
):
    key_data = auth_read(x_key, timestamp, request)
    vmid = key_data["vmid"]
    ip = get_ip(request)

    log.info(f"ip={ip} vmid={vmid} action=status")

    state = load(vmid)
    return {
        "job": state.get("jobs", {}).get("current"),
        "backups": state.get("backups", []),
    }


# ============================
# CREATE
# ============================
@app.post("/backups")
async def create_backup(
    request: Request,
    x_key: str = Header(None)
):
    payload = await request.json()
    key_data = auth_write(x_key, payload, request)
    vmid = key_data["vmid"]
    ip = get_ip(request)

    state = load(vmid)
    count = len(state.get("backups", []))
    max_backups = key_data.get("max_backups", 0)
    total_size = sum(b["size"] for b in state.get("backups", []))
    total_size_limit = key_data.get("total_backups_size", 0)

    if state.get("jobs", {}).get("current"):
        log.warning(f"ip={ip} vmid={vmid} action=create status=rejected reason=job_in_progress")
        raise HTTPException(409, detail={
            "error": "job_in_progress",
            "message": "Un job est déjà en cours pour cette VM. Attendez la fin avant de créer une nouvelle sauvegarde.",
            "count": count,
            "max_backups": max_backups,
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "total_backups_size": total_size_limit,
        })

    if max_backups > 0 and count >= max_backups:
        log.warning(f"ip={ip} vmid={vmid} action=create status=rejected reason=quota count={count} max={max_backups}")
        raise HTTPException(409, detail={
            "error": "quota_backups",
            "message": "Quota de backups atteint. Supprimez une backup avant.",
            "count": count,
            "max_backups": max_backups,
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "total_backups_size": total_size_limit,
        })

    if total_size_limit > 0 and total_size >= total_size_limit * 1024 * 1024:
        log.warning(f"ip={ip} vmid={vmid} action=create status=rejected reason=size_quota total_size={total_size} limit={total_size_limit * 1024 * 1024}")
        raise HTTPException(409, detail={
            "error": "quota_size",
            "message": "Quota de taille totale des backups atteint. Supprimez une backup avant.",
            "count": count,
            "max_backups": max_backups,
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "total_backups_size": total_size_limit,
        })

    job_queue.put({"vmid": vmid, "action": "create"})
    log.info(f"ip={ip} vmid={vmid} action=create status=queued")
    return {"status": "queued"}


# ============================
# DELETE
# ============================
@app.delete("/backups/{file}")
async def delete_backup(
    file: str,
    request: Request,
    x_key: str = Header(None)
):
    payload = await request.json()
    key_data = auth_write(x_key, payload, request)
    vmid = key_data["vmid"]
    ip = get_ip(request)

    if not validate_bid(file, vmid):
        raise HTTPException(400, "backup_id invalide")

    job_queue.put({"vmid": vmid, "action": "delete", "backup_id": file})
    log.info(f"ip={ip} vmid={vmid} action=delete status=queued file={file}")
    return {"status": "queued"}


# ============================
# RESTORE
# ============================
@app.post("/backups/{file}/restore")
async def restore_backup(
    file: str,
    request: Request,
    x_key: str = Header(None)
):
    payload = await request.json()
    key_data = auth_write(x_key, payload, request)
    vmid = key_data["vmid"]
    ip = get_ip(request)

    if not validate_bid(file, vmid):
        raise HTTPException(400, "backup_id invalide")

    job_queue.put({"vmid": vmid, "action": "restore", "backup_id": file})
    log.info(f"ip={ip} vmid={vmid} action=restore status=queued file={file}")
    return {"status": "queued"}