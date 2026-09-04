import json
import urllib.request
import urllib.parse
import urllib.error

from config.settings import TELEGRAM_SETTINGS as T

def call(method, **params):
    url = f"https://api.telegram.org/bot{T.bot_token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error_code": e.code}

print("token stored  : len", len(T.bot_token), "| starts", repr(T.bot_token[:8]), "| ends", repr(T.bot_token[-4:]))
print("chat_id stored:", repr(T.chat_id))

me = call("getMe")
if me.get("ok"):
    print("STEP 1 OK  -> this token belongs to bot: @" + me["result"]["username"])
else:
    print("STEP 1 FAIL -> getMe says:", me)
    print("  => the stored token is dead or mistyped. Get the current one from")
    print("     @BotFather -> /mybots -> your bot -> API Token, then re-run setup.")

up = call("getUpdates", limit=5)
if up.get("ok"):
    chats = {u.get("message", {}).get("chat", {}).get("id") for u in up["result"] if u.get("message")}
    print("STEP 2     -> chats this bot has SEEN:", chats)
else:
    print("STEP 2 FAIL -> getUpdates says:", up)

sent = call("sendMessage", chat_id=T.chat_id, text="sporty-edge diagnostic")
print("STEP 3     -> sendMessage says:", sent.get("description", sent.get("ok")))
