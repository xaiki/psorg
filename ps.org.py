#!/usr/bin/env python3
import os, re, sys, shutil, logging, requests, json
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

CACHE_FILE = "ps_cache.json"
# We switched to the content-ids endpoint to see the full list of DLC vs Game
API_URL = "https://api.serialstation.com/v1/content-ids/"

class PSGameOrganizer:
    def __init__(self, base_dir: str, debug: bool = False):
        self.base_dir = Path(base_dir).resolve()
        self.debug = debug
        self.code_pattern = re.compile(r'(CUSA|PPSA)[-]?([0-9]{5})', re.IGNORECASE)
        self.cache = self._load_cache()
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 PS-Organizer/1.0'})

    def _load_cache(self):
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        return {}

    def _save_cache(self):
        with open(CACHE_FILE, 'w') as f:
            json.dump(self.cache, f, indent=4)

    def extract_code(self, text: str):
        match = self.code_pattern.search(text)
        return f"{match.group(1).upper()}{match.group(2)}" if match else None

    def sanitize(self, name: str):
        s = re.sub(r'[^a-zA-Z0-9]', '.', name)
        return re.sub(r'\.+', '.', s).strip('.')

    def get_base_game_title(self, code: str):
        """Fetches all items for a CUSA and picks the most likely base game title."""
        if code in self.cache:
            return self.cache[code]

        params = {'title_id': code, 'limit': 100}
        
        if self.debug:
            print(f"\n# Debug Curl for {code}:")
            print(f"curl -G '{API_URL}' --data-urlencode 'title_id={code}' -H 'accept: application/json'\n")

        try:
            res = self.session.get(API_URL, params=params, timeout=30)
            if res.status_code == 200:
                items = res.json().get('items', [])
                if not items:
                    return None
                
                # Logic: Sort by length and pick the shortest name. 
                # The base game is usually "Lake" while DLC is "Lake: Season's Greetings"
                names = [i['name'] for i in items if i.get('name')]
                if not names: return None
                
                # Sort by length ascending
                names.sort(key=len)
                base_title = names[0]
                
                self.cache[code] = base_title
                self._save_cache()
                return base_title
        except Exception as e:
            logger.error(f"❌ API Error for {code}: {e}")
        return None

    def run(self):
        all_items = list(self.base_dir.iterdir())
        found_codes = {self.extract_code(i.name) for i in all_items if self.extract_code(i.name)}

        # Harvest missing info
        for code in found_codes:
            if code not in self.cache:
                self.get_base_game_title(code)

        # Organize
        for item in all_items:
            if item.name == "INCOMING": continue
            code = self.extract_code(item.name)
            if not code: continue

            title = self.cache.get(code)
            if not title: continue # Safe Mode: Don't rename if we don't know it

            expected = f"{self.sanitize(title)}.{code}"
            target = self.base_dir / expected

            if item.name != expected:
                if item.is_dir():
                    if target.exists() and target != item:
                        logger.info(f"🚜 Merging {item.name} ➡️ {expected}")
                        for f in item.iterdir(): shutil.move(str(f), str(target/f.name))
                        item.rmdir()
                    else:
                        logger.info(f"🔧 Repairing: {item.name} ➡️ {expected}")
                        item.rename(target)
                else:
                    target.mkdir(exist_ok=True)
                    logger.info(f"🚚 Sorting file: {item.name} ➡️ {expected}/")
                    shutil.move(str(item), str(target/item.name))

if __name__ == "__main__":
    debug_mode = "--debug" in sys.argv
    path = [a for a in sys.argv if a != "--debug"][1] if len(sys.argv) > 1 else os.getcwd()
    PSGameOrganizer(path, debug=debug_mode).run()
