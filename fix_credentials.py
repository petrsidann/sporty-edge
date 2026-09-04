import json
import sys
from pathlib import Path

# 1) rewrite credentials.json as clean UTF-8 (no BOM)
cred = Path("data") / "credentials.json"
if not cred.exists():
    print("ERROR: data/credentials.json not found - run this from the repo root.")
    sys.exit(1)

raw = cred.read_bytes()
bom = raw.startswith(b"\xef\xbb\xbf")
data = json.loads(raw.decode("utf-8-sig"))
cred.write_text(json.dumps(data, indent=2), encoding="utf-8")
print("credentials.json rewritten clean. BOM was present:", bom)
print("keys stored:", sorted(data.keys()))

# 2) harden config/settings.py against BOM forever (idempotent)
sp = Path("config") / "settings.py"
t = sp.read_text(encoding="utf-8-sig")
if "_load_credentials" not in t:
    print("WARNING: settings.py has no credential loader (old version).")
    print("Run:  git pull --rebase -X theirs origin main   then re-run this script.")
elif 'read_text(encoding="utf-8-sig")' in t:
    print("settings.py loader already BOM-safe.")
else:
    old = '_CREDENTIALS_PATH.read_text(encoding="utf-8")'
    new = '_CREDENTIALS_PATH.read_text(encoding="utf-8-sig")'
    if old in t:
        sp.write_text(t.replace(old, new, 1), encoding="utf-8")
        print("settings.py loader hardened: utf-8 -> utf-8-sig.")
    else:
        print("settings.py: loader line not found - paste settings.py to support.")

# 3) verify end-to-end
try:
    from config.settings import TELEGRAM_SETTINGS as T, FEED_SETTINGS as F
    tg_ok = bool(T.bot_token and T.chat_id and T.enabled)
    print("VERIFY | telegram:", tg_ok, "| api key:", bool(F.odds_api_key))
    if not tg_ok:
        print("Telegram still empty - paste the output above to support.")
except Exception as exc:
    print("Import failed:", type(exc).__name__, exc)
