#!/usr/bin/env python3
"""
mkffpkg.py - Create ffexfat/ffufs single-file images from organized PS4/PS5 dumps.

Reuses metadata/naming logic from ps.org.py.
- CUSA* (PS4) → .ffexfat via mkexfat.sh
- PPSA* (PS5) → .ffufs   via mkufs.sh

Usage:
    mkffpkg.py [dumps_dir] [ffpkg_dir] [--dry-run] [--force] [--debug]

Defaults:
    dumps_dir  = /data/Dumps
    ffpkg_dir  = /data/FFPKG     (i.e. ../FFPKG relative to dumps_dir)
"""

import os
import re
import sys
import json
import logging
import subprocess
import requests
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

CACHE_FILE      = "ps_cache.json"
API_URL         = "https://api.serialstation.com/v1/title-ids/"
SCRIPTS_DIR     = Path(__file__).parent          # mkexfat.sh / mkufs.sh live here
MKEXFAT_SCRIPT  = SCRIPTS_DIR / "mkexfat.sh"
MKufs_SCRIPT    = SCRIPTS_DIR / "mkufs2.sh"

EXT_PS4         = ".ffexfat"
EXT_PS5         = ".ffufs"

CODE_PATTERN    = re.compile(r'(CUSA|PPSA)[-]?(\d{5})', re.IGNORECASE)

# ── metadata (mirrored from ps.org.py) ───────────────────────────────────────

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

def platform(code: str):
    return 'PS5' if code.upper().startswith('PPSA') else 'PS4'

def target_ext(code: str):
    return EXT_PS5 if platform(code) == 'PS5' else EXT_PS4

def target_script(code: str):
    return MKufs_SCRIPT if platform(code) == 'PS5' else MKEXFAT_SCRIPT

# ── core ──────────────────────────────────────────────────────────────────────

def scan_dumps(dumps_dir: Path, store: MetadataStore):
    """
    Return list of (game_dir, code, clean_title, ext) for every valid dump folder.
    Folders that lack a recognisable code are skipped with a warning.
    """
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

def build_image(game_dir: Path, out_path: Path, code: str, dry_run: bool, debug: bool):
    script = target_script(code)
    if not script.exists():
        logger.error(f"❌  Script not found: {script}")
        return False

    cmd = ["bash", str(script), str(game_dir), str(out_path)]
    logger.info(f"  ▶  {' '.join(cmd)}")
    if dry_run:
        return True

    try:
        result = subprocess.run(cmd, check=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        logger.error(f"❌  {script.name} failed (exit {e.returncode}) for {game_dir.name}")
        return False

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args     = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run  = '--dry-run' in sys.argv
    force    = '--force'   in sys.argv
    debug    = '--debug'   in sys.argv

    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    dumps_dir = Path(args[0]).resolve() if len(args) > 0 else Path('/data/Dumps')
    ffpkg_dir = Path(args[1]).resolve() if len(args) > 1 else (dumps_dir.parent / 'FFPKG')

    if not dumps_dir.exists():
        logger.error(f"❌  Dumps dir not found: {dumps_dir}")
        sys.exit(1)

    ffpkg_dir.mkdir(parents=True, exist_ok=True)
    cache_path = dumps_dir / CACHE_FILE          # share cache with ps.org.py

    logger.info(f"📂  Dumps : {dumps_dir}")
    logger.info(f"📦  FFPKG : {ffpkg_dir}")
    if dry_run:
        logger.info("🔍  Dry-run mode — no files will be written")

    store   = MetadataStore(cache_path, debug=debug)
    entries = scan_dumps(dumps_dir, store)

    if not entries:
        logger.info("Nothing to process.")
        return

    ok = skip = fail = 0

    for game_dir, code, clean_title, ext in entries:
        stem     = f"{clean_title}.{code}"
        out_path = ffpkg_dir / f"{stem}{ext}"
        plat     = platform(code)

        if out_path.exists() and not force:
            logger.info(f"✅  {out_path.name}  (exists)")
            skip += 1
            continue

        tag = "🔄" if out_path.exists() else "🆕"
        logger.info(f"\n{tag}  [{plat}] {game_dir.name}")
        logger.info(f"   → {out_path.name}")

        if build_image(game_dir, out_path, code, dry_run, debug):
            logger.info(f"✅  Done: {out_path.name}")
            ok += 1
        else:
            fail += 1

    print()
    logger.info(f"Summary — built: {ok}  skipped: {skip}  failed: {fail}")

if __name__ == '__main__':
    main()
