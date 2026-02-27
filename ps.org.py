#!/usr/bin/env python3
import os, re, sys, shutil, logging, requests, json
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

CACHE_FILE = "ps_cache.json"
API_URL = "https://api.serialstation.com/v1/title-ids/"
FLAT_EXTENSIONS = {'.ffpkg', '.ffpfs', '.ffexfat'}

class PSGameOrganizer:
    def __init__(self, base_dir: str, debug: bool = False):
        self.base_dir = Path(base_dir).resolve()
        self.debug = debug
        self.code_pattern = re.compile(r'(CUSA|PPSA)[-]?(\d{5})', re.IGNORECASE)
        self.cache = self._load_cache()
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 PS-Organizer/1.0', 'accept': 'application/json'})

    def _load_cache(self):
        cache_path = Path(__file__).parent / CACHE_FILE
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_cache(self):
        cache_path = Path(__file__).parent / CACHE_FILE
        with open(cache_path, 'w') as f:
            json.dump(self.cache, f, indent=4)

    def extract_code(self, text: str):
        match = self.code_pattern.search(text)
        return f"{match.group(1).upper()}{match.group(2)}" if match else None    
    
    def sanitize(self, name: str):
        s = re.sub(r'[^a-zA-Z0-9]', '.', name)
        return re.sub(r'\.+', '.', s).strip('.')

    def fetch_metadata(self, code: str):
        """Fetches and caches the full JSON response for a given ID."""
        if code in self.cache and isinstance(self.cache[code], dict):
            return self.cache[code]

        url = f"{API_URL}{code}"
        if self.debug:
            print(f"\n# Debug Curl:\ncurl -X 'GET' '{url}' -H 'accept: application/json'\n")

        try:
            res = self.session.get(url, timeout=30)
            if res.status_code == 200:
                data = res.json()
                # Store the whole response
                self.cache[code] = data
                self._save_cache()
                return data
        except Exception as e:
            logger.error(f"❌ API Error for {code}: {e}")
        return None

    def recover_flat_files(self):
        """Finds flat files inside directories and moves them back to root."""
        for folder in [d for d in self.base_dir.iterdir() if d.is_dir()]:
            for file in folder.iterdir():
                if file.suffix.lower() in FLAT_EXTENSIONS:
                    logger.info(f"⏪ Recovering: {file.name} from {folder.name}")
                    shutil.move(str(file), str(self.base_dir / file.name))

    def get_display_name(self, code: str):
        """Extracts the best name from the cached JSON."""
        data = self.cache.get(code)
        if not data: return None
        # Handle manual string overrides in cache
        if isinstance(data, str):
            return data
        # SerialStation usually puts the title in 'name' or 'title'
        return data.get('name') or data.get('title')                    

    def run(self):
        if not self.base_dir.exists():
            logger.error(f"❌ Path not found: {self.base_dir}")
            return

        self.recover_flat_files()

        items = list(self.base_dir.iterdir())
        
        # Sync Cache
        codes = {self.extract_code(i.name) for i in items if self.extract_code(i.name)}
        for code in codes:
            if code not in self.cache:
                logger.info(f"🌐 Syncing metadata for {code}...")
                self.fetch_metadata(code)

        # Organize
        for item in items:
            if item.name in [CACHE_FILE, "INCOMING"]: continue
            
            code = self.extract_code(item.name)
            if not code: continue

            raw_name = self.get_display_name(code)
            if not raw_name:
                logger.warning(f"⚠️ No metadata for {code}, skipping.")
                continue

            clean_title = self.sanitize(raw_name)
            ext = item.suffix.lower()

            # Flat File Logic
            if item.is_file() and ext in FLAT_EXTENSIONS:
                expected = f"{clean_title}.{code}{ext}"
                if item.name != expected:
                    logger.info(f"📝 Renaming flat file: {item.name} ➡️ {expected}")
                    item.rename(self.base_dir / expected)
                continue

            # Standard Directory Logic
            expected_folder = f"{clean_title}.{code}"
            target_dir = self.base_dir / expected_folder

            if item.name != expected_folder:
                if item.is_dir():
                    if target_dir.exists():
                        logger.info(f"🚜 Merging: {item.name} ➡️ {expected_folder}")
                        for f in item.iterdir(): shutil.move(str(f), str(target_dir/f.name))
                        item.rmdir()
                    else:
                        logger.info(f"🔧 Correcting folder: {item.name} ➡️ {expected_folder}")
                        item.rename(target_dir)
                else:
                    target_dir.mkdir(exist_ok=True)
                    logger.info(f"🚚 Moving: {item.name} ➡️ {expected_folder}/")
                    shutil.move(str(item), str(target_dir/item.name))

if __name__ == "__main__":
    debug = "--debug" in sys.argv
    clean_args = [a for a in sys.argv if a != "--debug" and not a.endswith('.py')]
    path = clean_args[0] if clean_args else os.getcwd()
    PSGameOrganizer(path, debug=debug).run()
