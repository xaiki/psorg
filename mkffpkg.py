#!/usr/bin/env python3
"""
Optimized mkffpkg with QEMU/FreeBSD support - NO file copying
Supports multiple architectures (amd64, arm64, etc.)
"""

import os
import re
import sys
import json
import time
import logging
import subprocess
import requests
import hashlib
import shutil
import platform
from pathlib import Path
from typing import Tuple, Optional, Dict, List
from dataclasses import dataclass
from contextlib import contextmanager

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

CACHE_FILE      = "ps_cache.json"
API_URL         = "https://api.serialstation.com/v1/title-ids/"
SCRIPTS_DIR     = Path(__file__).parent
MKEXFAT_SCRIPT  = SCRIPTS_DIR / "mkexfat.sh"
MKUFS2_SCRIPT   = SCRIPTS_DIR / "mkufs2.sh"

EXT_PS4         = ".ffexfat"
EXT_PS5         = ".ffufs"
CODE_PATTERN    = re.compile(r'(CUSA|PPSA)[-]?(\d{5})', re.IGNORECASE)

# Architecture mappings
# Maps user-friendly arch names to FreeBSD QEMU target names
ARCH_MAP = {
    'amd64': {
        'qemu_system': 'qemu-system-x86_64',
        'freebsd_arch': 'amd64',
        'qemu_target': 'x86_64',
        'description': 'AMD64 (x86_64)'
    },
    'x86_64': {
        'qemu_system': 'qemu-system-x86_64',
        'freebsd_arch': 'amd64',
        'qemu_target': 'x86_64',
        'description': 'AMD64 (x86_64)'
    },
    'arm64': {
        'qemu_system': 'qemu-system-aarch64',
        'freebsd_arch': 'aarch64',
        'qemu_target': 'aarch64',
        'description': 'ARM64 (AArch64)'
    },
    'aarch64': {
        'qemu_system': 'qemu-system-aarch64',
        'freebsd_arch': 'aarch64',
        'qemu_target': 'aarch64',
        'description': 'ARM64 (AArch64)'
    },
    'riscv64': {
        'qemu_system': 'qemu-system-riscv64',
        'freebsd_arch': 'riscv',
        'qemu_target': 'riscv64',
        'description': 'RISC-V 64-bit'
    }
}

# Default architecture (target for emulation)
DEFAULT_ARCH = "amd64"  # We want to run amd64 FreeBSD
DEFAULT_FREEBSD_VERSION = "13.5-RELEASE"

# QEMU/FreeBSD configuration - LOCAL DIRECTORIES
VM_BASE_DIR     = Path("./freebsd-vm-base").absolute()      # Base VM images
VM_OVERLAY_DIR  = Path("./freebsd-vm-overlays").absolute()  # VM overlays
VM_SSH_PORT     = 2222
VM_USER         = "psbuilder"

# Mount tags (cannot contain slashes)
MOUNT_TAG_GAMES = "games"
MOUNT_TAG_OUTPUT = "output"

class FreeBSDVM:
    """Manages FreeBSD VM with overlay filesystem for caching"""
    
    def __init__(self, games_base: Path, output_base: Path, arch: str = DEFAULT_ARCH,
                 freebsd_version: str = DEFAULT_FREEBSD_VERSION, 
                 debug: bool = False, keep_alive: bool = True):
        self.debug = debug
        self.keep_alive = keep_alive
        self.arch = arch
        self.freebsd_version = freebsd_version
        self.vm_id = None
        self.overlay_path = None
        self.games_base = games_base.resolve()
        self.output_base = output_base.resolve()
        self.vm_pid_file = None
        self.accel_type = None  # Will be 'kvm', 'hvf', or 'tcg'
        
        # Get architecture config
        if arch not in ARCH_MAP:
            raise ValueError(f"Unsupported architecture: {arch}. Supported: {list(ARCH_MAP.keys())}")
        
        self.arch_config = ARCH_MAP[arch]
        self.qemu_binary = self.arch_config['qemu_system']
        self.freebsd_arch = self.arch_config['freebsd_arch']
        
        # Create directories
        VM_BASE_DIR.mkdir(parents=True, exist_ok=True)
        VM_OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
        
        # Detect available acceleration
        self._detect_acceleration()
        
        # Check if base VM exists, if not, create it
        base_vm_path = self._get_base_vm_path()
        if not base_vm_path.exists():
            logger.warning(f"Base VM not found: {base_vm_path}")
            if self._create_base_vm():
                logger.info("✅ Base VM created successfully")
            else:
                raise FileNotFoundError(f"Failed to create base VM. Please check dependencies.")
    
    def _get_base_vm_path(self) -> Path:
        """Get path for base VM image based on architecture"""
        return VM_BASE_DIR / f"freebsd-{self.freebsd_arch}-base.qcow2"
    
    def _detect_acceleration(self):
        """Detect available QEMU acceleration for the host architecture"""
        host_arch = platform.machine()
        
        # Check if we're cross-emulating
        is_cross_emulation = (host_arch != self.arch_config['qemu_target'])
        
        if is_cross_emulation:
            logger.info(f"⚠️  Cross-emulating {self.arch} on {host_arch} - using TCG (slow)")
            self.accel_type = "tcg"
            return
        
        # Native emulation, check for accelerators
        if sys.platform == 'darwin':
            # macOS Hypervisor.framework
            if shutil.which('hvf'):
                self.accel_type = "hvf"
                logger.info("✅ Using HVF acceleration (macOS Hypervisor.framework)")
            else:
                self.accel_type = "tcg"
                logger.warning("⚠️  HVF not available, using TCG (slower)")
        else:
            # Linux KVM
            if Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK):
                # Test if KVM works for this architecture
                try:
                    result = subprocess.run(
                        [self.qemu_binary, "-accel", "kvm", "-display", "none", "-M", "none"],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.returncode == 0:
                        self.accel_type = "kvm"
                        logger.info(f"✅ KVM acceleration available for {self.arch}")
                    else:
                        self.accel_type = "tcg"
                        logger.warning(f"⚠️  KVM not working for {self.arch}, using TCG")
                except:
                    self.accel_type = "tcg"
                    logger.warning("⚠️  KVM test failed, using TCG")
            else:
                self.accel_type = "tcg"
                logger.warning("⚠️  KVM not available, using TCG (slower)")
        
        if self.accel_type == "tcg" and not is_cross_emulation:
            logger.info("💡 Tip: For better performance, enable hardware acceleration")
            if sys.platform == 'darwin':
                logger.info("   - On macOS: HVF is built into QEMU")
            else:
                logger.info("   - On Linux: add user to 'kvm' group: sudo usermod -aG kvm $USER")
    
    def _check_qemu_img(self) -> bool:
        """Check if qemu-img is available"""
        qemu_img = shutil.which('qemu-img')
        if not qemu_img:
            logger.error("❌ qemu-img not found. Please install qemu-utils or qemu-tools")
            return False
        logger.debug(f"Found qemu-img: {qemu_img}")
        return True
    
    def _check_qemu_binary(self) -> bool:
        """Check if QEMU system binary is available"""
        qemu_bin = shutil.which(self.qemu_binary)
        if not qemu_bin:
            logger.error(f"❌ {self.qemu_binary} not found. Please install QEMU for {self.arch}")
            logger.info(f"   On Ubuntu/Debian: apt-get install qemu-system-{self.arch_config['qemu_target']}")
            logger.info(f"   On macOS: brew install qemu")
            return False
        logger.debug(f"Found QEMU binary: {qemu_bin}")
        return True
    
    def _create_base_vm(self) -> bool:
        """Create a base FreeBSD VM if it doesn't exist"""
        logger.info(f"📦 Creating base FreeBSD VM for {self.arch} (this may take a few minutes)...")
        
        if not self._check_qemu_img():
            return False
        
        base_vm_path = self._get_base_vm_path()
        temp_image = VM_BASE_DIR / f"freebsd-download-{self.freebsd_arch}.qcow2"
        compressed_img = VM_BASE_DIR / f"freebsd-download-{self.freebsd_arch}.qcow2.xz"
        
        try:
            # Construct download URL based on architecture
            url = f"https://download.freebsd.org/ftp/releases/VM-IMAGES/{self.freebsd_version}/{self.freebsd_arch}/Latest/FreeBSD-{self.freebsd_version}-{self.freebsd_arch}.qcow2.xz"
            logger.info(f"Downloading FreeBSD image from {url}...")
            
            # Use wget or curl
            download_cmd = None
            if shutil.which('wget'):
                download_cmd = ['wget', '-O', str(compressed_img), url]
            elif shutil.which('curl'):
                download_cmd = ['curl', '-L', '-o', str(compressed_img), url]
            else:
                logger.error("❌ Neither wget nor curl found. Please install one of them.")
                return False
            
            subprocess.run(download_cmd, check=True, capture_output=True)
            
            # Decompress
            logger.info("Decompressing image...")
            if shutil.which('xz'):
                subprocess.run(['xz', '-d', str(compressed_img)], check=True)
                # Rename decompressed file
                decompressed = compressed_img.with_suffix('')
                decompressed.rename(temp_image)
            else:
                logger.error("❌ xz not found. Please install xz-utils")
                return False
            
            # Resize image to 10GB
            logger.info("Resizing image to 10GB...")
            subprocess.run([
                'qemu-img', 'resize', str(temp_image), '10G'
            ], check=True, capture_output=True)
            
            # Move to final location
            temp_image.rename(base_vm_path)
            
            logger.info(f"Base VM image created: {base_vm_path}")
            logger.info("You may want to customize it with tools.")
            logger.info(f"To customize, run:")
            logger.info(f"  {self.qemu_binary} -drive file={base_vm_path},format=qcow2 -netdev user,id=net0 -device virtio-net,netdev=net0")
            logger.info("Then inside VM: pkg install -y makefs bash")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to create base VM: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False
    
    def start(self) -> bool:
        """Start VM with overlay for this session"""
        if not self._check_qemu_img() or not self._check_qemu_binary():
            return False
        
        # Create overlay for this VM instance
        self.vm_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
        self.overlay_path = VM_OVERLAY_DIR / f"overlay-{self.vm_id}-{self.arch}.qcow2"
        
        logger.info(f"🚀 Starting FreeBSD VM for {self.arch} (overlay: {self.overlay_path.name})")
        logger.info(f"⚡ Acceleration: {self.accel_type.upper()}")
        logger.info(f"📝 QEMU binary: {self.qemu_binary}")
        
        # Create overlay from base image - USE ABSOLUTE PATH for backing file
        try:
            base_vm_path = self._get_base_vm_path()
            base_image_abs = base_vm_path.absolute()
            overlay_path_abs = self.overlay_path.absolute()
            
            logger.debug(f"Creating overlay from {base_image_abs} to {overlay_path_abs}")
            
            result = subprocess.run([
                "qemu-img", "create", "-f", "qcow2", 
                "-b", str(base_image_abs),  # Use absolute path
                "-F", "qcow2",
                str(overlay_path_abs)
            ], check=True, capture_output=True, text=True)
            logger.debug(f"Overlay created: {result.stdout}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create overlay: {e.stderr}")
            return False
        
        # Build virtio-fs arguments with proper mount tags (no slashes)
        virtio_args = []
        
        # Mount games directory
        if self.games_base.exists():
            virtio_args.extend([
                "-virtfs", f"local,path={self.games_base},mount_tag={MOUNT_TAG_GAMES},security_model=mapped-xattr"
            ])
            logger.debug(f"  Mounting {self.games_base} -> tag: {MOUNT_TAG_GAMES}")
        else:
            logger.error(f"Games directory not found: {self.games_base}")
            return False
        
        # Mount output directory
        if self.output_base.exists():
            virtio_args.extend([
                "-virtfs", f"local,path={self.output_base},mount_tag={MOUNT_TAG_OUTPUT},security_model=mapped-xattr"
            ])
            logger.debug(f"  Mounting {self.output_base} -> tag: {MOUNT_TAG_OUTPUT}")
        else:
            logger.warning(f"Output directory doesn't exist yet, creating: {self.output_base}")
            self.output_base.mkdir(parents=True, exist_ok=True)
            virtio_args.extend([
                "-virtfs", f"local,path={self.output_base},mount_tag={MOUNT_TAG_OUTPUT},security_model=mapped-xattr"
            ])
        
        # Prepare pid file
        self.vm_pid_file = Path(f"/tmp/freebsd-vm-{self.vm_id}-{self.arch}.pid")
        
        # Build QEMU command with appropriate acceleration and machine type
        cmd = [
            self.qemu_binary,
            "-name", f"freebsd-ps5-{self.vm_id}",
            "-machine", "q35",  # q35 works for both x86_64 and aarch64
            "-accel", self.accel_type,
            "-m", "4096",
            "-smp", "4",
            "-drive", f"file={self.overlay_path},format=qcow2,if=virtio",
            "-netdev", f"user,id=net0,hostfwd=tcp::{VM_SSH_PORT}-:22",
            "-device", "virtio-net,netdev=net0",
            "-daemonize",
            "-display", "none",
            "-pidfile", str(self.vm_pid_file)
        ] + virtio_args
        
        # Add architecture-specific options
        if self.arch in ['arm64', 'aarch64']:
            # For ARM64, we need UEFI firmware
            firmware_paths = [
                "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
                "/usr/local/share/qemu/edk2-aarch64-code.fd",
                "/usr/share/AAVMF/AAVMF_CODE.fd"
            ]
            for fw_path in firmware_paths:
                if Path(fw_path).exists():
                    cmd.extend(["-bios", fw_path])
                    logger.debug(f"Using UEFI firmware: {fw_path}")
                    break
        
        if self.debug:
            logger.debug(f"VM command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"QEMU failed to start: {result.stderr}")
                # Try without acceleration if that was the issue
                if "invalid accelerator" in result.stderr:
                    logger.info("Retrying with TCG acceleration...")
                    self.accel_type = "tcg"
                    # Replace the acceleration in command
                    accel_idx = cmd.index("-accel") + 1
                    cmd[accel_idx] = "tcg"
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        logger.error(f"QEMU failed again with TCG: {result.stderr}")
                        return False
                else:
                    return False
            
            # Wait for SSH (longer timeout for TCG or cross-emulation)
            if self._wait_for_ssh():
                # Setup mount points inside VM
                if self._setup_mounts():
                    logger.info("✅ VM ready")
                    return True
                else:
                    logger.error("❌ Failed to setup mounts")
                    return False
            else:
                logger.error("❌ SSH not available")
                return False
                
        except Exception as e:
            logger.error(f"❌ Failed to start VM: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False
    
    def _setup_mounts(self) -> bool:
        """Setup mount points inside VM for virtio-fs shares"""
        try:
            # Wait a bit for VM to fully boot
            time.sleep(5)
            
            # Create mount points
            ret, stdout, stderr = self.run_command("sudo mkdir -p /mnt/games /mnt/output")
            if ret != 0:
                logger.error(f"Failed to create mount points: {stderr}")
                return False
            
            # Mount virtio-fs shares
            ret, stdout, stderr = self.run_command(f"sudo mount -t virtiofs {MOUNT_TAG_GAMES} /mnt/games")
            if ret != 0:
                logger.error(f"Failed to mount games: {stderr}")
                return False
            
            ret, stdout, stderr = self.run_command(f"sudo mount -t virtiofs {MOUNT_TAG_OUTPUT} /mnt/output")
            if ret != 0:
                logger.error(f"Failed to mount output: {stderr}")
                return False
            
            # Create symlinks for easier access
            self.run_command("sudo ln -sf /mnt/games /games")
            self.run_command("sudo ln -sf /mnt/output /output")
            
            if self.debug:
                self.run_command("ls -la /games /output")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to setup mounts: {e}")
            return False
    
    def _wait_for_ssh(self, timeout: int = None) -> bool:
        """Wait for SSH to become available (longer timeout for slow emulation)"""
        # Set timeout based on acceleration type
        if timeout is None:
            if self.accel_type == "tcg":
                timeout = 300  # 5 minutes for TCG
            else:
                timeout = 120  # 2 minutes for hardware accel
        
        logger.info(f"⏳ Waiting for VM to boot (timeout: {timeout}s)...")
        start_time = time.time()
        attempt = 0
        while time.time() - start_time < timeout:
            attempt += 1
            if self._check_ssh():
                logger.info(f"✅ SSH available after {attempt} attempts ({int(time.time() - start_time)}s)")
                return True
            if attempt % 5 == 0:  # Log every 10 seconds
                logger.debug(f"Still waiting for SSH... ({int(time.time() - start_time)}s)")
            time.sleep(2)
        logger.error(f"Timeout waiting for SSH after {timeout} seconds")
        return False
    
    def _check_ssh(self) -> bool:
        """Check if SSH is responsive"""
        # First check if VM process is still running
        if self.vm_pid_file and self.vm_pid_file.exists():
            try:
                pid = int(self.vm_pid_file.read_text().strip())
                if not Path(f"/proc/{pid}").exists():
                    return False
            except:
                pass
        
        result = subprocess.run(
            ["ssh", "-p", str(VM_SSH_PORT), "-o", "ConnectTimeout=3",
             "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
             "-o", "PasswordAuthentication=no",
             f"{VM_USER}@localhost", "echo ok 2>/dev/null"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0 and b"ok" in result.stdout
    
    def run_command(self, cmd: str, check: bool = False) -> Tuple[int, str, str]:
        """Run command in VM via SSH"""
        ssh_cmd = [
            "ssh", "-p", str(VM_SSH_PORT),
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            f"{VM_USER}@localhost",
            cmd
        ]
        
        if self.debug:
            logger.debug(f"  SSH: {cmd[:100]}...")
        
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=60)
            
            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
            
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timeout"
        except Exception as e:
            return -1, "", str(e)
    
    def build_ufs_image(self, game_path: Path, output_path: Path, script_path: Path) -> bool:
        """
        Build UFS image directly from mounted game folder - NO COPYING
        
        Inside VM:
        - /games contains the host's game base directory
        - /output contains the output directory
        """
        # Calculate relative paths from mount points
        try:
            game_relative = game_path.relative_to(self.games_base)
            vm_game_path = f"/games/{game_relative}"
        except ValueError:
            logger.error(f"Game path {game_path} is not under {self.games_base}")
            return False
        
        vm_output_path = f"/output/{output_path.name}"
        
        # Copy script to VM (small file, okay to copy)
        vm_script_path = f"/tmp/{script_path.name}"
        if not self._copy_script_to_vm(script_path, vm_script_path):
            return False
        
        # Create build script that runs directly on mounted paths
        build_cmd = f"""
        set -e
        chmod +x {vm_script_path}
        sudo {vm_script_path} "{vm_game_path}" "{vm_output_path}"
        """
        
        logger.info(f"  Building: {vm_game_path} -> {vm_output_path}")
        
        returncode, stdout, stderr = self.run_command(build_cmd)
        
        if self.debug:
            if stdout:
                logger.debug(f"VM stdout: {stdout}")
            if stderr:
                logger.debug(f"VM stderr: {stderr}")
        
        if returncode == 0:
            # Verify output was created
            if output_path.exists():
                file_size = output_path.stat().st_size
                logger.info(f"  ✅ Image created: {output_path.name} ({file_size / (1024**3):.2f} GB)")
                return True
            else:
                logger.error(f"  ❌ Output file not found: {output_path}")
                # Check if it was created in VM but not visible
                self.run_command(f"ls -la {vm_output_path}")
                return False
        else:
            logger.error(f"  ❌ Build failed with code {returncode}")
            if stderr:
                logger.error(f"  Error: {stderr[:500]}")
            return False
    
    def _copy_script_to_vm(self, local_script: Path, vm_path: str) -> bool:
        """Copy script to VM using SCP"""
        scp_cmd = [
            "scp", "-P", str(VM_SSH_PORT),
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            str(local_script),
            f"{VM_USER}@localhost:{vm_path}"
        ]
        
        try:
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error(f"Failed to copy script: {result.stderr}")
                return False
            return True
        except Exception as e:
            logger.error(f"SCP failed: {e}")
            return False
    
    def stop(self):
        """Stop the VM and remove overlay"""
        if not self.overlay_path:
            return
            
        logger.info(f"🛑 Stopping VM")
        
        # Try graceful shutdown
        try:
            self.run_command("sudo shutdown -p now", check=False)
            time.sleep(3)
        except:
            pass
        
        # Kill QEMU process
        if self.vm_pid_file and self.vm_pid_file.exists():
            try:
                pid = int(self.vm_pid_file.read_text().strip())
                subprocess.run(["kill", str(pid)], capture_output=True)
                time.sleep(1)
            except Exception as e:
                logger.debug(f"Error killing VM: {e}")
            finally:
                self.vm_pid_file.unlink(missing_ok=True)
        
        # Remove overlay to save space
        if self.overlay_path and self.overlay_path.exists():
            self.overlay_path.unlink()
            logger.debug(f"Removed overlay: {self.overlay_path}")
        
        self.overlay_path = None
    
    def __enter__(self):
        if self.start():
            return self
        else:
            raise RuntimeError("Failed to start VM")
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.keep_alive:
            self.stop()

# ── metadata (unchanged from previous) ─────────────────────────────────────

class MetadataStore:
    def __init__(self, cache_path: Path, debug: bool = False):
        self.cache_path = cache_path
        self.debug = debug
        self.cache = self._load()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 PS-Organizer/1.0',
            'accept': 'application/json',
        })

    def _load(self):
        if self.cache_path.exists():
            try:
                with open(self.cache_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        with open(self.cache_path, 'w') as f:
            json.dump(self.cache, f, indent=4)

    def fetch(self, code: str):
        if code in self.cache and isinstance(self.cache[code], dict):
            return self.cache[code]
        url = f"{API_URL}{code}"
        if self.debug:
            logger.debug(f"  curl '{url}'")
        try:
            res = self.session.get(url, timeout=30)
            if res.status_code == 200:
                data = res.json()
                self.cache[code] = data
                self._save()
                return data
        except Exception as e:
            logger.warning(f"⚠️  API error for {code}: {e}")
        return None

    def display_name(self, code: str):
        data = self.cache.get(code)
        if not data:
            return None
        if isinstance(data, str):
            return data
        return data.get('name') or data.get('title')

# ── helpers ───────────────────────────────────────────────────────────────────

def extract_code(text: str):
    m = CODE_PATTERN.search(text)
    return f"{m.group(1).upper()}{m.group(2)}" if m else None

def sanitize(name: str):
    s = re.sub(r'[^a-zA-Z0-9]', '.', name)
    return re.sub(r'\.+', '.', s).strip('.')

def platform_type(code: str):
    return 'PS5' if code.upper().startswith('PPSA') else 'PS4'

def target_ext(code: str):
    return EXT_PS5 if platform_type(code) == 'PS5' else EXT_PS4

def target_script(code: str):
    return MKUFS2_SCRIPT if platform_type(code) == 'PS5' else MKEXFAT_SCRIPT

def scan_dumps(dumps_dir: Path, store: MetadataStore):
    """Scan dump directory for valid games"""
    entries = []
    for item in sorted(dumps_dir.iterdir()):
        if not item.is_dir():
            continue
        code = extract_code(item.name)
        if not code:
            logger.debug(f"  skip (no code): {item.name}")
            continue

        raw = store.display_name(code)
        if not raw:
            logger.info(f"🌐 Fetching metadata for {code}…")
            store.fetch(code)
            raw = store.display_name(code)

        if not raw:
            logger.warning(f"⚠️  No metadata for {code} ({item.name}) — skipping")
            continue

        entries.append((item, code, sanitize(raw), target_ext(code)))
    return entries

def build_image_native(game_dir: Path, out_path: Path, script: Path, debug: bool) -> bool:
    """Build image natively (for PS4/exFAT)"""
    cmd = ["bash", str(script), str(game_dir), str(out_path)]
    
    if debug:
        logger.debug(f"  Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, capture_output=debug)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Script failed (exit {e.returncode})")
        if debug and e.stderr:
            logger.error(e.stderr.decode())
        return False

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run = '--dry-run' in sys.argv
    force = '--force' in sys.argv
    debug = '--debug' in sys.argv
    use_qemu = '--qemu' in sys.argv
    keep_vm = '--keep-vm' in sys.argv
    
    # Parse architecture
    arch = DEFAULT_ARCH
    for i, arg in enumerate(sys.argv):
        if arg.startswith('--arch='):
            arch = arg.split('=')[1]
        elif arg == '--arch' and i+1 < len(sys.argv):
            arch = sys.argv[i+1]
    
    # Parse FreeBSD version if provided
    freebsd_version = DEFAULT_FREEBSD_VERSION
    for i, arg in enumerate(sys.argv):
        if arg.startswith('--freebsd-version='):
            freebsd_version = arg.split('=')[1]
        elif arg == '--freebsd-version' and i+1 < len(sys.argv):
            freebsd_version = sys.argv[i+1]
    
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Configure paths
    dumps_dir = Path(args[0]).resolve() if len(args) > 0 else Path('/data/Dumps')
    ffpkg_dir = Path(args[1]).resolve() if len(args) > 1 else (dumps_dir.parent / 'FFPKG')
    
    if not dumps_dir.exists():
        logger.error(f"❌ Dumps dir not found: {dumps_dir}")
        sys.exit(1)
    
    ffpkg_dir.mkdir(parents=True, exist_ok=True)
    cache_path = dumps_dir / CACHE_FILE
    
    logger.info(f"📂 Dumps : {dumps_dir}")
    logger.info(f"📦 FFPKG : {ffpkg_dir}")
    logger.info(f"🎮 Mode  : {'QEMU/FreeBSD' if use_qemu else 'Native'}")
    logger.info(f"💾 VM Base: {VM_BASE_DIR}")
    logger.info(f"💾 VM Overlays: {VM_OVERLAY_DIR}")
    logger.info(f"🔧 Architecture: {arch} ({ARCH_MAP[arch]['description']})")
    logger.info(f"🔧 FreeBSD Version: {freebsd_version}")
    
    if dry_run:
        logger.info("🔍 Dry-run mode — no files will be written")
    
    store = MetadataStore(cache_path, debug=debug)
    entries = scan_dumps(dumps_dir, store)
    
    if not entries:
        logger.info("Nothing to process.")
        return
    
    # Separate PS4 and PS5 entries
    ps4_entries = [(d, c, t, e) for d, c, t, e in entries if platform_type(c) == 'PS4']
    ps5_entries = [(d, c, t, e) for d, c, t, e in entries if platform_type(c) == 'PS5']
    
    ok = skip = fail = 0
    
    # Process PS4 games natively
    for game_dir, code, clean_title, ext in ps4_entries:
        stem = f"{clean_title}.{code}"
        out_path = ffpkg_dir / f"{stem}{ext}"
        
        if out_path.exists() and not force:
            logger.info(f"✅ {out_path.name} (exists)")
            skip += 1
            continue
        
        logger.info(f"\n🆕 [PS4] {game_dir.name}")
        logger.info(f"   → {out_path.name}")
        
        if dry_run:
            ok += 1
            continue
        
        if build_image_native(game_dir, out_path, MKEXFAT_SCRIPT, debug):
            logger.info(f"✅ Done: {out_path.name}")
            ok += 1
        else:
            fail += 1
    
    # Process PS5 games with QEMU if requested
    if ps5_entries:
        if not use_qemu:
            logger.error("\n❌ PS5 games require FreeBSD. Use --qemu flag")
            fail += len(ps5_entries)
        else:
            # Determine base directories for mounting
            games_base = dumps_dir  # Mount the entire Dumps directory
            output_base = ffpkg_dir  # Mount the output directory
            
            logger.info(f"🔧 Mounting {games_base} as 'games' tag")
            logger.info(f"🔧 Mounting {output_base} as 'output' tag")
            
            try:
                with FreeBSDVM(games_base, output_base, arch=arch, 
                             freebsd_version=freebsd_version,
                             debug=debug, keep_alive=keep_vm) as vm:
                    for game_dir, code, clean_title, ext in ps5_entries:
                        stem = f"{clean_title}.{code}"
                        out_path = ffpkg_dir / f"{stem}{ext}"
                        
                        if out_path.exists() and not force:
                            logger.info(f"✅ {out_path.name} (exists)")
                            skip += 1
                            continue
                        
                        logger.info(f"\n🆕 [PS5] {game_dir.name}")
                        logger.info(f"   → {out_path.name}")
                        
                        if dry_run:
                            ok += 1
                            continue
                        
                        if vm.build_ufs_image(game_dir, out_path, MKUFS2_SCRIPT):
                            logger.info(f"✅ Done: {out_path.name}")
                            ok += 1
                        else:
                            fail += 1
            except Exception as e:
                logger.error(f"❌ VM error: {e}")
                if debug:
                    import traceback
                    traceback.print_exc()
                fail += len(ps5_entries)
    
    print()
    logger.info(f"Summary — built: {ok} skipped: {skip} failed: {fail}")

if __name__ == '__main__':
    main()
