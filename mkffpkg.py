#!/usr/bin/env python3
"""
mkffpkg.py — PS5 build system with QEMU/FreeBSD support
Uses official FreeBSD 13.5‑RELEASE VM image and creates overlays.
"""

import os
import re
import sys
import json
import time
import hashlib
import shutil
import subprocess
import platform

from pathlib import Path
from typing import Tuple

# --------------------
# Configuration
# --------------------

FREEBSD_VERSION = "13.5-RELEASE"
ARCH = "amd64"

BASE_DIR     = Path("./freebsd-vm-base").absolute()
OVERLAY_DIR  = Path("./freebsd-vm-overlays").absolute()

# Official FreeBSD cloud VM image (QCOW2 compressed)
QCOW2_URL_TMPL = (
    "https://download.freebsd.org/releases/VM-IMAGES/"
    "{version}/{arch}/Latest/FreeBSD-{version}-{arch}.qcow2.xz"
)

SSH_PORT     = 2222
SSH_USER     = "psbuilder"

# --------------------
# VM Utility Functions
# --------------------

def ensure_base_image() -> Path:
    """Ensure the official FreeBSD QCOW2 base image is downloaded + decompressed."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    qcow2_xz = BASE_DIR / f"FreeBSD-{FREEBSD_VERSION}-{ARCH}.qcow2.xz"
    qcow2_img = BASE_DIR / f"FreeBSD-{FREEBSD_VERSION}-{ARCH}.qcow2"

    # Download if missing
    if not qcow2_xz.exists() and not qcow2_img.exists():
        url = QCOW2_URL_TMPL.format(version=FREEBSD_VERSION, arch=ARCH)
        print(f"📥 Downloading FreeBSD base image from {url} ...")
        subprocess.run(["curl", "-L", "-o", str(qcow2_xz), url], check=True)

    # Decompress if needed
    if qcow2_xz.exists() and not qcow2_img.exists():
        print("📦 Decompressing base image...")
        subprocess.run(["unxz", "-k", str(qcow2_xz)], check=True)

    if not qcow2_img.exists():
        raise FileNotFoundError(f"Base image not available: {qcow2_img}")

    print(f"✅ Base image ready: {qcow2_img}")
    return qcow2_img

def create_overlay(base_img: Path) -> Path:
    """Create a per‑run overlay from the base image."""
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    overlay = OVERLAY_DIR / f"overlay-{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}.qcow2"
    overlay_abs = overlay.resolve()
    base_abs    = base_img.resolve()

    print(f"📀 Creating overlay: {overlay_abs}")
    subprocess.run([
        "qemu-img", "create",
        "-f", "qcow2",
        "-F", "qcow2",
        "-b", str(base_abs),
        str(overlay_abs),
    ], check=True)

    return overlay_abs

def start_vm(overlay: Path) -> Tuple[int, Path]:
    """Start QEMU with overlay, returns (pid, log_path)."""
    pidfile = Path(f"/tmp/freebsd-vm-{overlay.stem}.pid")
    logpath = Path(f"/tmp/qemu-{overlay.stem}.log")

    cmd = [
        "qemu-system-x86_64",
        "-name", f"freebsd-ps5-{overlay.stem}",
        "-machine", "q35",
        "-m", "4096",
        "-smp", "4",

        "-drive", f"if=none,id=hd0,file={overlay},format=qcow2",
        "-device", "virtio-blk-pci,drive=hd0",

        "-netdev", f"user,id=net0,hostfwd=tcp::{SSH_PORT}-:22",
        "-device", "virtio-net,netdev=net0",

        "-display", "none",
        "-serial", f"file:{logpath}",

        "-daemonize",
        "-pidfile", str(pidfile),
    ]

    print("🚀 Launching VM...")
    subprocess.run(cmd, check=True)
    pid = int(pidfile.read_text().strip())
    print(f"   QEMU PID: {pid}, log: {logpath}")

    return pid, logpath

def wait_for_ssh(timeout: int = 120) -> bool:
    """Wait for SSH to become available inside VM."""
    print("⏳ Waiting for SSH...")
    start = time.time()
    while time.time() - start < timeout:
        result = subprocess.run([
            "ssh", "-p", str(SSH_PORT),
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=3",
            "-o", "BatchMode=yes",
            f"{SSH_USER}@localhost", "echo ok"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            print("✅ SSH ready!")
            return True
        time.sleep(2)
    print("❌ SSH timeout")
    return False

def run_in_vm(cmd: str):
    """Run a command inside the VM over SSH."""
    sshcmd = [
        "ssh", "-p", str(SSH_PORT),
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        f"{SSH_USER}@localhost", cmd
    ]
    return subprocess.run(sshcmd, capture_output=True, text=True)

def stop_vm(pid: int):
    """Stop the VM gracefully."""
    try:
        run_in_vm("sudo shutdown -p now")
    except:
        pass
    time.sleep(5)
    subprocess.run(["kill", str(pid)], stderr=subprocess.DEVNULL)

# --------------------
# Main Entry
# --------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python mkffpkg.py --qemu <DUMPS_DIR>")
        sys.exit(1)

    # Only qemu mode supported here
    if "--qemu" not in sys.argv:
        print("ERROR: QEMU mode required")
        sys.exit(1)

    dumps_dir = Path(sys.argv[-1]).resolve()
    if not dumps_dir.exists():
        print(f"❌ Dumps dir not found: {dumps_dir}")
        sys.exit(1)

    print(f"📂 Dumps: {dumps_dir}")
    print(f"🔧 Using FreeBSD {FREEBSD_VERSION} VM base")

    # Ensure base image is ready
    base_img = ensure_base_image()

    # Start VM
    overlay = create_overlay(base_img)
    pid, logpath = start_vm(overlay)

    # Wait for SSH
    if not wait_for_ssh(timeout=180):
        print("❌ Cannot connect to VM via SSH — check console log")
        print("Log output:")
        print(logpath.read_text())
        stop_vm(pid)
        sys.exit(1)

    # Example: test inside VM
    print("📝 Test: uname inside VM")
    out = run_in_vm("uname -a")
    print(out.stdout)

    # Place build logic here (e.g., mount dirs, run mkufs2.sh, etc.)
    # For simplicity, not included here.

    # Shutdown VM
    stop_vm(pid)

    print("✅ VM run complete")

if __name__ == "__main__":
    main()
