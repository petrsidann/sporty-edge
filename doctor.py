"""
doctor.py - full system health check. Run FIRST when the feed looks empty.

    python doctor.py

Checks: credentials file (validity + auto-repair), keys loaded, settings,
system clock vs real time (HTTP Date header), live feed test (1 credit),
odds cache age, session clock. ASCII only, stdlib only.
"""

from __future__ import annotations

import email.utils
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CRED = Path("data") / "credentials.json"
CACHE = Path("data") / "feed_cache.json"
PASS, FAIL, WARN = "[PASS]", "[FAIL]", "[WARN]"


def say(tag: str, label: str, detail: str = "") -> None:
    print(f"  {tag} {label}" + (f" - {detail}" if detail else ""))


def try_parse(text: str):
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} (line {exc.lineno}, col {exc.colno})"


def attempt_repair(text: str):
    """Fix the two killers: smart quotes and trailing commas."""
    fixes = []
    t = text
    for bad, good in (("\u201c", '"'), ("\u201d", '"'),
                      ("\u2018", "'"), ("\u2019", "'")):
        if bad in t:
            t = t.replace(bad, good)
            fixes.append("smart quotes straightened")
    t2 = re.sub(r",\s*([}\]])", r"\1", t)
    if t2 != t:
        fixes.append("trailing comma(s) removed")
    return t, t2, fixes


def main() -> None:
    print("=" * 66)
    print("  sporty-edge DOCTOR")
    print("=" * 66)
    print(f"  repo: {Path.cwd()}")
    if "Documents" in str(Path.cwd()):
        say(WARN, "this is the Documents clone - Desktop is the working one")

    # ---- 1. credentials ----
    data = None
    if not CRED.exists():
        say(FAIL, "data/credentials.json missing", "run: python setup_credentials.py")
    else:
        raw = CRED.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        if bom:
            say(WARN, "BOM bytes found (PowerShell save)", "loader tolerates it")
        parsed, err = try_parse(raw.decode("utf-8-sig"))
        if parsed is None:
            _, repaired, fixes = attempt_repair(raw.decode("utf-8-sig"))
            parsed2, err2 = try_parse(repaired)
            if parsed2 is not None and fixes:
                CRED.write_text(json.dumps(parsed2, indent=2), encoding="utf-8")
                say(PASS, "credentials AUTO-FIXED and re-saved", "; ".join(fixes))
                parsed, err = parsed2, None
            else:
                say(FAIL, "credentials.json is INVALID JSON", err or "")
                print("        -> re-save it cleanly (template at the bottom of")
                print("           the runbook) or run: python setup_credentials.py")
        else:
            say(PASS, "credentials.json valid JSON")
        data = parsed

    keys_loaded = 0
    if isinstance(data, dict):
        primary = str(data.get("odds_api_key") or "").strip()
        extras = [str(k).strip() for k in (data.get("odds_api_keys_extra") or [])
                  if str(k).strip()]
        keys_loaded = (1 if primary else 0) + len(extras)
        tg_ok = bool(str(data.get("telegram_bot_token") or "").strip()
                     and str(data.get("telegram_chat_id") or "").strip())
        say(PASS if tg_ok else FAIL, "telegram credentials", f"present={tg_ok}")
        say(PASS if keys_loaded else FAIL, "api keys stored",
            f"total={keys_loaded} (primary={bool(primary)}, extras={len(extras)})")

    # ---- 2. settings ----
    try:
        from config.settings import FEED_SETTINGS, PLACEABLE_BOOKS
        n_sports = len(getattr(FEED_SETTINGS, "feed_sports", []) or [])
        say(PASS if n_sports else FAIL, "feed sports configured",
            f"{n_sports} sports, markets={FEED_SETTINGS.markets}")
        print(f"        load targets: {', '.join(PLACEABLE_BOOKS)}")
    except Exception as exc:
        say(FAIL, "config.settings failed to import", f"{type(exc).__name__}: {exc}")
        return

    # ---- 3. clock skew (wrong clock = played games appear / games vanish) ----
    skew_min = None
    try:
        req = urllib.request.Request("https://github.com", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            server = email.utils.parsedate_to_datetime(resp.headers["Date"])
        skew_min = abs((datetime.now(timezone.utc) - server).total_seconds()) / 60.0
        ok = skew_min <= 10.0
        say(PASS if ok else FAIL, "system clock vs real time",
            f"skew {skew_min:.1f} min"
            + ("" if ok else "  -> Settings > Time > Set time automatically + Sync now"))
    except Exception as exc:
        say(WARN, "clock check skipped", f"{type(exc).__name__}")

    # ---- 4. live feed test (costs 1 credit, uses key 1) ----
    key = ""
    if isinstance(data, dict):
        key = str(data.get("odds_api_key") or "").strip()
        if not key:
            extras = data.get("odds_api_keys_extra") or []
            key = str(extras[0]).strip() if extras else ""
    if not key:
        say(FAIL, "live feed test skipped", "no key available to test with")
    else:
        try:
            url = f"https://api.the-odds-api.com/v4/sports?apiKey={key}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                remaining = resp.headers.get("x-requests-remaining", "?")
                n = len(json.loads(resp.read().decode()))
                say(PASS, "live feed test", f"{n} sport keys listed, "
                    f"credits left ~{remaining}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                say(FAIL, "live feed test: key INVALID/expired",
                    "get the current key from your the-odds-api dashboard")
            elif exc.code == 429:
                say(FAIL, "live feed test: quota exhausted on tested key",
                    "squad will auto-failover to your other keys")
            else:
                say(FAIL, "live feed test", f"HTTP {exc.code}")
        except Exception as exc:
            say(FAIL, "live feed test", f"{type(exc).__name__}: {exc}")

    # ---- 5. cache ----
    if CACHE.exists():
        try:
            c = json.loads(CACHE.read_text(encoding="utf-8-sig"))
            sports = c.get("sports") or {}
            newest = max((float(v.get("ts", 0)) for v in sports.values()), default=0)
            age = (datetime.now(timezone.utc).timestamp() - newest) / 3600.0
            say(PASS, "odds cache", f"{len(sports)} sports cached, newest {age:.1f}h old")
        except Exception as exc:
            say(WARN, "odds cache unreadable", str(exc)[:60])
    else:
        say(WARN, "no odds cache yet", "first successful run creates it")

    # ---- 6. session clock ----
    try:
        from utils.session import clock_line
        print(f"\n  {clock_line()}")
    except Exception as exc:
        say(WARN, "session clock", f"{type(exc).__name__}: {exc}")

    print("=" * 66)
    print("  VERDICT: fix every [FAIL] above, then run:  python squad.py")
    print("=" * 66)


if __name__ == "__main__":
    main()