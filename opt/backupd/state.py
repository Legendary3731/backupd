import json
import os

STATE_DIR = "/var/lib/backupd/state"

def _path(vmid):
    return f"{STATE_DIR}/{vmid}.json"

def load(vmid):
    try:
        return json.load(open(_path(vmid)))
    except:
        return {
            "backups": [],
            "last_backup": 0
        }

def save(vmid, data):
    os.makedirs(STATE_DIR, exist_ok=True)

    tmp = _path(vmid) + ".tmp"
    json.dump(data, open(tmp, "w"))
    os.replace(tmp, _path(vmid))