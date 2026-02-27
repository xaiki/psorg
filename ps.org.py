#!/usr/bin/env python3
import os, re, sys, shutil, logging, requests, json
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

CACHE_FILE = "ps_cache.json"
API_URL = "https://api.serialstation.com/v1/content-ids/"
FLAT_EXTENSIONS = {'.ffpkg', '.ffpfs', '.ffexfat'}

class PSGameOrganizer:
    def __init__(self, base_dir: str, debug: bool = False):
        self.base_dir = Path(base_dir).resolve()
        self.debug = debug
        self.code_pattern = re.compile(r'(CUSA|PPSA)[-]?(\d{5})', re.IGNORECASE)
        self.cache = self._load_cache()
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 PS-Organizer/1.0'})

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

    def get_base_game_title(self, code: str):
        if code in self.cache: return self.cache[code]
        params = {'title_id': code, 'limit': 100}
        try:
            res = self.session.get(API_URL, params=params, timeout=30)
            if res.status_code == 200:
                items = res.json().get('items', [])
                names = [i['name'] for i in items if i.get('name')]
                if not names: return None
                names.sort(key=len)
                base_title = names[0]
                self.cache[code] = base_title
                self._save_cache()
                return base_title
        except Exception as e:
            logger.error(f"❌ API Error for {code}: {e}")
            if self.debug:
                print(f"\n# Debug Curl for {code}:")
                print(f"curl -G '{API_URL}' --data-urlencode 'title_id={code}' -H 'accept: application/json'\n")

        return None

    def recover_flat_files(self):
        """Finds flat files inside directories and moves them back to root."""
        for folder in [d for d in self.base_dir.iterdir() if d.is_dir()]:
            for file in folder.iterdir():
                if file.suffix.lower() in FLAT_EXTENSIONS:
                    logger.info(f"⏪ Recovering: {file.name} from {folder.name}")
                    shutil.move(str(file), str(self.base_dir / file.name))
            
            # Clean up now-empty folders
            try:
                if not any(folder.iterdir()):
                    folder.rmdir()
            except:
                pass

    def run(self):
        if not self.base_dir.exists():
            logger.error(f"❌ Path not found: {self.base_dir}")
            return

        # 1. Recover flat files that were moved incorrectly
        self.recover_flat_files()

        items = list(self.base_dir.iterdir())
        
        # 2. Harvest missing titles
        codes_to_fetch = {self.extract_code(i.name) for i in items if self.extract_code(i.name)}
        for code in codes_to_fetch:
            if code and code not in self.cache:
                self.get_base_game_title(code)

        # 3. Organize
        for item in items:
            if item.name == "INCOMING" or item.name == CACHE_FILE: continue
            
            code = self.extract_code(item.name)
            if not code: continue

            title = self.cache.get(code)
            if not title:
                if self.debug:
                    logger.warning(f"🔍 Skipping {item.name}: ID {code} not in cache/API.")
                continue
            
            clean_title = self.sanitize(title)
            
            # FLAT FILE LOGIC: Rename only
            if item.is_file() and item.suffix.lower() in FLAT_EXTENSIONS:
                expected_filename = f"{clean_title}.{code}{item.suffix.lower()}"
                if item.name != expected_filename:
                    logger.info(f"📝 Renaming flat file: {item.name} ➡️ {expected_filename}")
                    item.rename(self.base_dir / expected_filename)
                continue

            # DIRECTORY LOGIC: For folders and other file types (PKGs/Zips)
            expected_folder = f"{clean_title}.{code}"
            target_dir = self.base_dir / expected_folder

            if item.name != expected_folder:
                if item.is_dir():
                    if target_dir.exists() and target_dir.resolve() != item.resolve():
                        logger.info(f"🚜 Merging: {item.name} ➡️ {expected_folder}")
                        for f in item.iterdir(): shutil.move(str(f), str(target_dir/f.name))
                        item.rmdir()
                    else:
                        logger.info(f"🔧 Correcting: {item.name} ➡️ {expected_folder}")
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
