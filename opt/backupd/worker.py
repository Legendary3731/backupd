import queue
import threading
import subprocess
import time
import os
import re

from state import load, save
from logger import get_logger

job_queue = queue.Queue()
VM_LOCKS = {}

log = get_logger("worker")

ARCHIVE_RE = re.compile(r"archive '([^']+)'")


def extract_backup_config(bid: str) -> dict:
    try:
        result = subprocess.run(
            ["pvesm", "extractconfig", bid],
            capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        log.warning(f"bid={bid} extractconfig=fail reason={e.stderr.strip()}")
        return {}

    config = {}
    for line in result.stdout.splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            config[key.strip()] = value.strip()

    log.info(f"bid={bid} extractconfig=ok keys={list(config.keys())}")
    return config


def get_storage_from_config(cfg: dict, is_lxc: bool) -> str | None:
    if is_lxc:
        rootfs = cfg.get("rootfs", "")
        if ":" in rootfs:
            return rootfs.split(":")[0]
    else:
        for key, val in cfg.items():
            if re.match(r"(scsi|virtio|ide|sata)\d+", key) and ":" in val:
                return val.split(":")[0]
    return None


def run_worker():
    while True:
        job = job_queue.get()
        vmid = job["vmid"]
        action = job["action"]

        VM_LOCKS.setdefault(vmid, threading.Lock())

        with VM_LOCKS[vmid]:
            state = load(vmid)
            state.setdefault("jobs", {})["current"] = {
                "action": action,
                "started": time.time(),
                "file": None,
                "current_size": 0,
            }
            save(vmid, state)

            t_start = time.time()
            log.info(f"vmid={vmid} action={action} status=start")

            try:
                # ---------------- CREATE ----------------
                if action == "create":
                    proc = subprocess.Popen(
                        [
                            "vzdump", str(vmid),
                            "--mode", "snapshot",
                            "--compress", "zstd",
                            "--storage", "local",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )

                    dump_path = None

                    for line in proc.stdout:
                        if dump_path is None:
                            m = ARCHIVE_RE.search(line)
                            if m:
                                dump_path = m.group(1)
                                log.info(f"vmid={vmid} action=create file={dump_path} status=writing")
                                s = load(vmid)
                                s["jobs"]["current"]["file"] = dump_path
                                save(vmid, s)

                        if dump_path and os.path.exists(dump_path):
                            s = load(vmid)
                            s["jobs"]["current"]["current_size"] = os.path.getsize(dump_path)
                            save(vmid, s)

                    rc = proc.wait()
                    if rc != 0:
                        raise RuntimeError(f"vzdump exited with code {rc}")

                    if not dump_path or not os.path.exists(dump_path):
                        raise RuntimeError("dump file not found after vzdump")

                    size = os.path.getsize(dump_path)
                    fname = os.path.basename(dump_path)

                    s = load(vmid)
                    s.setdefault("backups", []).append({
                        "file": fname,
                        "date": time.time(),
                        "size": size,
                    })
                    s["last_backup"] = time.time()
                    save(vmid, s)

                    elapsed = round(time.time() - t_start, 1)
                    log.info(f"vmid={vmid} action=create status=done file={fname} size={size} elapsed={elapsed}s")

                # ---------------- DELETE ----------------
                elif action == "delete":
                    bid = job["backup_id"]
                    path = os.path.join("/var/lib/vz/dump", bid)

                    state = load(vmid)
                    state["backups"] = [
                        b for b in state.get("backups", [])
                        if b["file"] != bid
                    ]

                    if os.path.exists(path):
                        os.remove(path)
                        log.info(f"vmid={vmid} action=delete status=done file={bid}")
                    else:
                        log.warning(f"vmid={vmid} action=delete file={bid} reason=file_not_found")

                    save(vmid, state)

                # ---------------- RESTORE ----------------
                elif action == "restore":
                    bid = job["backup_id"]

                    is_lxc  = bid.startswith("vzdump-lxc-")
                    is_qemu = bid.startswith("vzdump-qemu-")
                    is_pbs  = bid.startswith("pbs:")

                    if not (is_lxc or is_qemu or is_pbs):
                        raise RuntimeError(f"unknown backup type for bid={bid}")

                    bid_full = bid if is_pbs else f"local:backup/{bid}"
                    vm_type = "lxc" if is_lxc else "kvm"

                    cfg = extract_backup_config(bid_full)
                    storage = get_storage_from_config(cfg, is_lxc)

                    if not storage:
                        raise RuntimeError(
                            f"impossible de déterminer le stockage depuis la config de {bid}"
                        )

                    log.info(f"vmid={vmid} action=restore type={vm_type} storage={storage} file={bid}")

                    if is_lxc:
                        log.info(f"vmid={vmid} action=restore step=stop")
                        subprocess.run(["pct", "stop", str(vmid)], check=False)

                        log.info(f"vmid={vmid} action=restore step=destroy")
                        subprocess.run(["pct", "destroy", str(vmid)], check=False)

                        log.info(f"vmid={vmid} action=restore step=restore")
                        subprocess.run(
                            ["pct", "restore", str(vmid), bid_full,
                             "--storage", storage,
                             "--force",
                             "--unique", "0"],
                            check=True
                        )

                        log.info(f"vmid={vmid} action=restore step=start")
                        subprocess.run(["pct", "start", str(vmid)], check=True)

                    else:
                        log.info(f"vmid={vmid} action=restore step=stop")
                        subprocess.run(["qm", "stop", str(vmid)], check=False)

                        log.info(f"vmid={vmid} action=restore step=destroy")
                        subprocess.run(["qm", "destroy", str(vmid)], check=False)

                        if is_pbs:
                            log.info(f"vmid={vmid} action=restore step=live_restore (PBS)")
                            subprocess.run(
                                ["qmrestore", bid, str(vmid),
                                 "--storage", storage,
                                 "--force",
                                 "--unique", "0",
                                 "--live-restore"],
                                check=True
                            )
                        else:
                            log.info(f"vmid={vmid} action=restore step=restore")
                            subprocess.run(
                                ["qmrestore", bid_full, str(vmid),
                                 "--storage", storage,
                                 "--force",
                                 "--unique", "0"],
                                check=True
                            )

                            log.info(f"vmid={vmid} action=restore step=start")
                            subprocess.run(["qm", "start", str(vmid)], check=True)

                    elapsed = round(time.time() - t_start, 1)
                    log.info(f"vmid={vmid} action=restore status=done file={bid} elapsed={elapsed}s")

            except Exception as e:
                elapsed = round(time.time() - t_start, 1)
                log.error(f"vmid={vmid} action={action} status=error elapsed={elapsed}s error={e}", exc_info=True)

            finally:
                s = load(vmid)
                s.get("jobs", {}).pop("current", None)
                save(vmid, s)

        job_queue.task_done()


def start_worker():
    threading.Thread(target=run_worker, daemon=True).start()