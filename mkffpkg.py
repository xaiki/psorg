#!/usr/bin/env python3
"""
mkffpkg.py - PS5 build system
Supports two modes:
1. exfat (Default): Local execution using mkexfat.sh
2. qemu: Virtualized FreeBSD 13.5 environment for mkufs2
"""

import os
import sys
import time
import hashlib
import subprocess
import argparse
import logging
from pathlib import Path
from typing import Tuple

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# --------------------
# Configuration
# --------------------

FREEBSD_VERSION = "13.5-RELEASE"
ARCH = "amd64"

BASE_DIR = Path("./freebsd-vm-base").absolute()
OVERLAY_DIR = Path("./freebsd-vm-overlays").absolute()

QCOW2_URL_TMPL = (
    "https://download.freebsd.org/releases/VM-IMAGES/"
    "{version}/{arch}/Latest/FreeBSD-{version}-{arch}.qcow2.xz"
)

SSH_PORT = 2222
SSH_USER = "psbuilder"

def resolve_game_root(dump_path: Path) -> Path:
    """
    Hunts down the actual game root by looking for eboot.bin or sce_sys.
    Returns the resolved path, or the original if not found.
    """
    # 1. Check if we are already at the true root
    if (dump_path / "eboot.bin").is_file() or (dump_path / "sce_sys").is_dir():
        return dump_path

    # 2. Look for eboot.bin inside subdirectories
    try:
        # next() grabs the very first match efficiently without scanning the whole tree
        eboot_path = next(dump_path.rglob("eboot.bin"))
        print(f"🔍 Found game root tucked inside: {eboot_path.parent.name}")
        return eboot_path.parent
    except StopIteration:
        pass

    # 3. Fallback: look for sce_sys just in case eboot is missing but it's still a valid dump
    try:
        sce_sys_path = next(dump_path.rglob("sce_sys"))
        if sce_sys_path.is_dir():
            print(f"🔍 Found game root via sce_sys inside: {sce_sys_path.parent.name}")
            return sce_sys_path.parent
    except StopIteration:
        pass

    # If nothing is found, return the original and let the downstream tool fail naturally
    return dump_path

# --------------------
# VM Utility Functions
# --------------------

def ensure_base_image() -> Path:
    """Ensure the official FreeBSD QCOW2 base image is downloaded + decompressed."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    qcow2_xz = BASE_DIR / f"FreeBSD-{FREEBSD_VERSION}-{ARCH}.qcow2.xz"
    qcow2_img = BASE_DIR / f"FreeBSD-{FREEBSD_VERSION}-{ARCH}.qcow2"

    if not qcow2_xz.exists() and not qcow2_img.exists():
        url = QCOW2_URL_TMPL.format(version=FREEBSD_VERSION, arch=ARCH)
        print(f"📦 Downloading FreeBSD base image from {url} ...")
        subprocess.run(["curl", "-L", "-o", str(qcow2_xz), url], check=True)

    if qcow2_xz.exists() and not qcow2_img.exists():
        print("📂 Decompressing base image...")
        subprocess.run(["unxz", "-k", str(qcow2_xz)], check=True)

    return qcow2_img

def create_overlay(base_img: Path) -> Path:
    """Create a per-run overlay from the base image."""
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    overlay = OVERLAY_DIR / f"overlay-{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}.qcow2"
    
    print(f"💾 Creating overlay: {overlay}")
    subprocess.run([
        "qemu-img", "create",
        "-f", "qcow2",
        "-F", "qcow2",
        "-b", str(base_img.resolve()),
        str(overlay.resolve()),
    ], check=True)

    return overlay.resolve()

def start_vm(overlay: Path) -> Tuple[int, Path]:
    """Start QEMU with overlay."""
    pidfile = Path(f"/tmp/freebsd-vm-{overlay.stem}.pid")
    logpath = Path(f"/tmp/qemu-{overlay.stem}.log")

    cmd = [
        "qemu-system-x86_64",
        "-name", f"freebsd-ps5-{overlay.stem}",
        "-machine", "q35", "-m", "4096", "-smp", "4",
        "-drive", f"if=none,id=hd0,file={overlay},format=qcow2",
        "-device", "virtio-blk-pci,drive=hd0",
        "-netdev", f"user,id=net0,hostfwd=tcp::{SSH_PORT}-:22",
        "-device", "virtio-net,netdev=net0",
        "-display", "none", "-serial", f"file:{logpath}",
        "-daemonize", "-pidfile", str(pidfile),
    ]

    print("🚀 Launching VM...")
    subprocess.run(cmd, check=True)
    pid = int(pidfile.read_text().strip())
    return pid, logpath

def wait_for_ssh(timeout: int = 180) -> bool:
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
    return False

def run_in_vm(cmd: str):
    sshcmd = [
        "ssh", "-p", str(SSH_PORT),
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        f"{SSH_USER}@localhost", cmd
    ]
    return subprocess.run(sshcmd, capture_output=True, text=True)

def stop_vm(pid: int):
    try:
        run_in_vm("sudo shutdown -p now")
    except:
        pass
    time.sleep(5)
    subprocess.run(["kill", str(pid)], stderr=subprocess.DEVNULL)

# --------------------
# Workflow Logic
# --------------------

def run_exfat_mode(game_dir: Path, output_file: Path):
    """Simple local exfat run."""
    print(f"🛠️ Mode: Local exFAT for {game_dir.name}")
    script_path = Path(__file__).parent / "mkexfat.sh"
    
    if not script_path.exists():
        print(f"❌ Error: {script_path} not found.")
        sys.exit(1)

    actual_game_dir = resolve_game_root(game_dir)    
    print(f"📦 Running {script_path.name} on {actual_game_dir}...")
    
    subprocess.run(["sudo", "bash", str(script_path), str(actual_game_dir), str(output_file)], check=True)

def run_qemu_mode(game_dir: Path, output_file: Path):
    """Messy FreeBSD VM UFS2 run."""
    print(f"🛠️ Mode: QEMU FreeBSD ({FREEBSD_VERSION}) for {game_dir.name}")
    
    base_img = ensure_base_image()
    overlay = create_overlay(base_img)
    pid, logpath = start_vm(overlay)

    try:
        if not wait_for_ssh():
            print("❌ SSH timeout. Check logs:", logpath)
            sys.exit(1)

        print("📝 Running build logic inside VM...")
        out = run_in_vm("uname -a")
        print(f"VM Info: {out.stdout.strip()}")
        # Build logic for mkufs2.sh would be triggered here, mapping game_dir and output_file
        
    finally:
        stop_vm(pid)

# --------------------
# Main Entry
# --------------------

def main():
    parser = argparse.ArgumentParser(description="PS5 FPKG Tool")
    parser.add_argument("dumps_dir", help="Directory containing the decrypted dumps")
    parser.add_argument("--mode", choices=["exfat", "qemu"], default="exfat",
                        help="Build mode: 'exfat' (local script) or 'qemu' (FreeBSD VM). Default: exfat")
    
    # Backward compatibility for the old --qemu flag
    if "--qemu" in sys.argv:
        sys.argv.remove("--qemu")
        sys.argv.append("--mode")
        sys.argv.append("qemu")

    args = parser.parse_args()
    dumps_path = Path(args.dumps_dir).resolve()

    if not dumps_path.exists() or not dumps_path.is_dir():
        print(f"❌ Dumps directory not found or is not a directory: {dumps_path}")
        sys.exit(1)

    # Output directory setup
    output_dir_name = "FFExFAT" if args.mode == "exfat" else "FFPKG"
    output_ext = ".ffexfat" if args.mode == "exfat" else ".ffpfs" # Assuming ffpfs for ufs2/qemu mode based on extensions in ps.org.py
    
    output_dir = dumps_path.parent / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Output directory set to: {output_dir}")

    import ps_org # Import the logic from ps.org.py (assuming it's named ps_org.py)
    # Instantiate the organizer to use its methods
    organizer = ps_org.PSGameOrganizer(str(dumps_path), debug=False)

    for item in dumps_path.iterdir():
        if not item.is_dir() or item.name in ["INCOMING", ps_org.CACHE_FILE]:
            continue

        code = organizer.extract_code(item.name)
        if not code:
            logger.warning(f"⚠️ Could not extract title code from directory name: {item.name}. Skipping.")
            continue

        raw_name = organizer.get_display_name(code)
        
        # Check if we have metadata, if not, try fetching it
        if not raw_name:
            logger.info(f"🌐 Fetching metadata for {code}...")
            organizer.fetch_metadata(code)
            raw_name = organizer.get_display_name(code)
            
        if not raw_name:
             logger.warning(f"⚠️ No metadata found for {code} ({item.name}), skipping conversion.")
             continue

        clean_title = organizer.sanitize(raw_name)
        expected_name = f"{clean_title}.{code}"
        output_file_name = f"{expected_name}{output_ext}"
        output_file_path = output_dir / output_file_name

        # Check for wrong name (assuming ps.org.py logic would have renamed it, but we check here just in case)
        if item.name != expected_name:
            logger.warning(f"⚠️ Found directory with potential wrong name: {item.name}. Expected: {expected_name}. Processing anyway using expected name for output.")

        if output_file_path.exists():
            logger.info(f"⏭️ Skipping {item.name}: Output file {output_file_name} already exists.")
            continue

        logger.info(f"🚀 Processing {item.name} -> {output_file_name}")

        if args.mode == "exfat":
            run_exfat_mode(item, output_file_path)
        else:
            run_qemu_mode(item, output_file_path)

    print("✅ Process complete")

if __name__ == "__main__":
    main()
