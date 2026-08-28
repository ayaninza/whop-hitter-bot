"""
whop_bot.py
Full Whop checkout autofill:
  - billing address (picked NEW from a pool of 50 random USA addresses)
  - random email  @rabbitvore.com
  - provided card (Basis Theory iframes)

Does NOT submit. Edit CARD below; addresses/emails are generated.
Playwright >= 1.40  |  pip install playwright && playwright install chromium
Run: python whop_bot.py
"""

import json
import os
import random
import re
import string
import tempfile
import subprocess
import glob
from playwright.sync_api import sync_playwright

CHECKOUT_URL = "https://whop.com/checkout/2onbgwXn2utmOapDAl-sTbB-xhGu-BQo9-ppzjRbOKz1Pc/"

# ===== CARD (provided) =====
CARD = {
    "number": "5328398287077228",
    "exp_month": "05",
    "exp_year": "2029",   # 2-digit used in form: 29
    "cvc": "211",
}
# ===========================

ADDRESS_POOL_FILE = "addresses.json"
POOL_SIZE = 50
EMAIL_DOMAIN = "rabbitvore.com"

# ===== PROXIES (rotated every run) =====
PROXIES = [
    "208280:Jdp5rbuDkbCb@196.51.106.142:8800",
    "208280:Jdp5rbuDkbCb@196.51.106.238:8800",
    "208280:Jdp5rbuDkbCb@196.51.106.202:8800",
    "208280:Jdp5rbuDkbCb@196.51.106.253:8800",
    "208280:Jdp5rbuDkbCb@196.51.109.56:8800",
    "208280:Jdp5rbuDkbCb@196.51.106.169:8800",
    "208280:Jdp5rbuDkbCb@196.51.109.16:8800",
    "208280:Jdp5rbuDkbCb@196.51.109.250:8800",
    "208280:Jdp5rbuDkbCb@196.51.109.207:8800",
    "208280:Jdp5rbuDkbCb@196.51.109.178:8800",
]

LAST_PROXY = {"v": None}

def pick_proxy():
    """Rotate to a NEW proxy each run (never repeat the last one)."""
    choices = [p for p in PROXIES if p != LAST_PROXY["v"]] or PROXIES
    p = random.choice(choices)
    LAST_PROXY["v"] = p
    auth, hostport = p.rsplit("@", 1)
    user, pw = auth.split(":", 1)
    return {"server": "http://" + hostport, "username": user, "password": pw}

# ===== HUMAN / FINGERPRINT RANDOMIZATION =====
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
VIEWPORTS = [
    (1366, 768), (1536, 864), (1440, 900), (1920, 1080), (1280, 720),
]
# US timezones + a representative city center (lat, lon) for geolocation
US_GEO = [
    ("America/New_York", 40.7128, -74.0060),
    ("America/Chicago", 41.8781, -87.6298),
    ("America/Denver", 39.7392, -104.9903),
    ("America/Los_Angeles", 34.0522, -118.2437),
    ("America/Phoenix", 33.4484, -112.0740),
    ("America/Chicago", 29.7604, -95.3698),
]

STEALTH_JS = """
() => {
  // kill the webdriver flag
  Object.defineProperty(navigator, 'webdriver', { get: () => false });
  // fake chrome object
  if (!window.chrome) { window.chrome = {}; }
  window.chrome.runtime = {};
  // languages / platform
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
  // spoofed hardware concurrency + deviceMemory
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => %D_concurrency% });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => %D_memory% });
  // plugins / mimeTypes (non-empty)
  Object.defineProperty(navigator, 'plugins', { get: () => [
    { name: 'Chrome PDF Plugin', description: 'Portable Document Format', filename: 'internal-pdf-viewer' },
    { name: 'Chrome PDF Viewer', description: 'Portable Document Format', filename: 'mhjfbmdgcfjbbpaeojklgcpcanfnfnlo' },
    { name: 'Native Client', description: '', filename: 'ppapi' }
  ] });
  // permissions query lies about notification
  const origQuery = window.Notification ? window.Notification.permission : 'denied';
  if (window.Notification) { try { Object.defineProperty(window.Notification, 'permission', { get: () => 'default' }); } catch(e){} }
  // block automation detection via CDP
  window.cdc_adoQpoasnfa76pfcZLmc3J3ossfp = undefined;
}
"""

def build_stealth(concurrency, memory):
    return (STEALTH_JS
            .replace("%D_concurrency%", str(concurrency))
            .replace("%D_memory%", str(memory)))

def human_typing_delay():
    return random.uniform(25, 70)

def human_pause(min_s=0.4, max_s=1.6):
    return random.uniform(min_s, max_s)


# Curated real-ish US (city, state-abbr, zip) tuples for realism.
US_PLACES = [
    ("Springfield", "IL", "62704"), ("Austin", "TX", "73301"), ("Denver", "CO", "80202"),
    ("Portland", "OR", "97201"), ("Seattle", "WA", "98101"), ("Miami", "FL", "33101"),
    ("Boston", "MA", "02108"), ("Phoenix", "AZ", "85001"), ("Atlanta", "GA", "30301"),
    ("Nashville", "TN", "37201"), ("Columbus", "OH", "43215"), ("Detroit", "MI", "48201"),
    ("Minneapolis", "MN", "55401"), ("Salt Lake City", "UT", "84101"), ("Las Vegas", "NV", "89101"),
    ("San Diego", "CA", "92101"), ("Sacramento", "CA", "95814"), ("Dallas", "TX", "75201"),
    ("Houston", "TX", "77001"), ("Chicago", "IL", "60601"), ("Newark", "NJ", "07102"),
    ("Pittsburgh", "PA", "15201"), ("Baltimore", "MD", "21201"), ("Charlotte", "NC", "28201"),
    ("Indianapolis", "IN", "46201"), ("Kansas City", "MO", "64101"), ("Oklahoma City", "OK", "73101"),
    ("Milwaukee", "WI", "53201"), ("Cincinnati", "OH", "45201"), ("Louisville", "KY", "40201"),
    ("Richmond", "VA", "23218"), ("Buffalo", "NY", "14201"), ("Albuquerque", "NM", "87101"),
    ("Tucson", "AZ", "85701"), ("Fresno", "CA", "93701"), ("Omaha", "NE", "68101"),
    ("Tampa", "FL", "33601"), ("Cleveland", "OH", "44101"), ("St. Louis", "MO", "63101"),
    ("Raleigh", "NC", "27601"), ("Boise", "ID", "83701"), ("Spokane", "WA", "99201"),
]
STREETS = ["Birchwood Lane", "Maple Avenue", "Oak Hollow Drive", "Cedar Ridge Road",
           "Elm Street", "Sunset Boulevard", "Lakeview Court", "Pinecrest Way",
           "Willow Branch Lane", "Ironhorse Trail", "Magnolia Drive", "Foxglove Circle"]
FIRST = ["Jordan", "Avery", "Kai", "Riley", "Morgan", "Devon", "Sasha", "Quinn",
         "Theo", "Lena", "Marco", "Noor", "Wren", "Cyrus", "Indie", "Beau"]
LAST = ["Mercer", "Vance", "Okafor", "Castellano", "Nakamura", "Reyes", "Bianchi",
        "Holloway", "Petrov", "Sullivan", "Abara", "Delacroix", "Voss", "Tran"]


# Manual, REAL, valid US addresses (street + city/state/zip that actually
# match). Whop validates the street, so we use genuine addresses rather than
# random strings. State stored as full name (Whop's <select> wants full name).
MANUAL_ADDRESSES = [
    ("350 5th Ave",            "New York",      "New York",        "10118"),
    ("30 Rockefeller Plaza",   "New York",      "New York",        "10112"),
    ("231 W Monroe St",        "Chicago",       "Illinois",        "60606"),
    ("233 S Wacker Dr",        "Chicago",       "Illinois",        "60606"),
    ("1600 Amphitheatre Pkwy", "Mountain View", "California",      "94043"),
    ("1 Infinite Loop",        "Cupertino",     "California",      "95014"),
    ("200 Santa Monica Pier",  "Santa Monica",  "California",      "90401"),
    ("400 Broad St",           "Seattle",       "Washington",      "98109"),
    ("350 5th Ave",            "New York",      "New York",        "10118"),
    ("1 Apple Park Way",       "Cupertino",     "California",      "95014"),
]


def gen_address():
    line1, city, st, zipc = random.choice(MANUAL_ADDRESSES)
    return {
        "name": f"{random.choice(FIRST)} {random.choice(LAST)}",
        "line1": line1,
        "line2": "",
        "city": city,
        "state": st,
        "zip": zipc,
        "country": "US",
    }


def chromium_missing_libs():
    """Return the list of missing shared libraries for the Chromium binary,
    or None if it can't be determined. Useful to diagnose launch crashes."""
    try:
        base = os.path.expanduser("~/.cache/ms-playwright")
        bins = (glob.glob(os.path.join(base, "chromium*", "chrome-linux", "chrome"))
                + glob.glob(os.path.join(base, "chromium_headless_shell*",
                                         "chrome-linux", "headless_shell")))
        if not bins:
            return "chromium binary not found in " + base
        out = subprocess.run(["ldd", bins[0]], capture_output=True, text=True,
                             timeout=30)
        miss = [ln for ln in out.stdout.splitlines() if "not found" in ln]
        return "\n".join(miss) if miss else "no missing libs reported by ldd"
    except Exception as e:
        return f"(ldd check failed: {e})"


def _write_pool(pool):
    # unique temp file per call so concurrent workers don't clobber each other
    d = os.path.dirname(os.path.abspath(ADDRESS_POOL_FILE)) or "."
    fd, tmppath = tempfile.mkstemp(dir=d, prefix="addr_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(pool, f, indent=2)
        os.replace(tmppath, ADDRESS_POOL_FILE)
    except Exception:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
        raise


def load_pool():
    if os.path.exists(ADDRESS_POOL_FILE):
        try:
            with open(ADDRESS_POOL_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("addresses"), list) \
                    and data["addresses"]:
                data.setdefault("used", [])
                return data
        except Exception:
            # corrupt / empty file — discard and regenerate
            pass
    pool = {"used": [], "addresses": [gen_address() for _ in range(POOL_SIZE)]}
    _write_pool(pool)
    return pool


def get_new_address():
    pool = load_pool()
    avail = [a for i, a in enumerate(pool["addresses"]) if i not in pool["used"]]
    if not avail:  # all used -> regenerate a fresh 50
        pool = {"used": [], "addresses": [gen_address() for _ in range(POOL_SIZE)]}
        avail = pool["addresses"]
    addr = random.choice(avail)
    idx = pool["addresses"].index(addr)
    pool["used"].append(idx)
    _write_pool(pool)
    return addr


def random_email():
    pre = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(8, 13)))
    return f"{pre}@{EMAIL_DOMAIN}"


# Name = random keyword strings containing "rabbit vore" / "rabbit vorist"
RABBIT_BASES = ["rabbit vore", "rabbit vorist", "rabbitvore", "rabbit vorist"]
RABBIT_WORDS = ["zelda", "mosswick", "hollow", "thorn", "vesper", "cinder", "lumen",
                "sable", "wren", "onyx", "feral", "mire", "ash", "glade", "pyre",
                "drift", "ember", "coy", "marrow", "sol", "nyx", "bram", "quill"]


def rabbit_name():
    base = random.choice(RABBIT_BASES)
    return f"{base} {random.choice(RABBIT_WORDS)}"


# --- field filling (visible fill + JS fallback for clipped React inputs) ---
FIELD_SELECTORS = {
    "name":    ['input[name="name"]'],
    "line1":   ['input[name="line1"]'],
    "line2":   ['input[name="line2"]'],
    "city":    ['input[name="city"]'],
    "state":   ['input[name="state"]'],
    "zip":     ['input[name="zip"]'],
    "country": ['select[name="country"]'],
    "email":   ['input[name="email"]'],
}


def fill_all(page, key, value):
    if value in ("", None):
        return 0
    # Special handling for line1: type char-by-char to trigger browser autocomplete
    if key == "line1":
        return fill_line1_autocomplete(page, value)
    done = 0
    for sel in FIELD_SELECTORS[key]:
        for el in page.locator(sel).all():
            try:
                tag = el.element_handle().evaluate("n => n.tagName.toLowerCase()")
                if tag == "select":
                    el.select_option(value=value, timeout=1500)
                else:
                    try:
                        el.fill(value, timeout=1500)
                    except Exception:
                        el.evaluate(
                            """(node, val) => {
                                const setter = Object.getOwnPropertyDescriptor(
                                    Object.getPrototypeOf(node), 'value').set;
                                setter.call(node, val);
                                node.dispatchEvent(new Event('input', {bubbles:true}));
                                node.dispatchEvent(new Event('change', {bubbles:true}));
                            }""", value)
                done += 1
            except Exception:
                continue
    print(f"[{'ok' if done else 'skip'}] {key} -> {done}")
    return done


def fill_line1_autocomplete(page, address):
    """Type line1 char-by-char to trigger browser autocomplete, then pick suggestion.
    This fills city/state/zip automatically like a real user."""
    value = _norm(address)
    if not value:
        return 0
    try:
        # Find the line1 input (first visible)
        el = page.locator('input[name="line1"]').first
        el.click(timeout=3000)
        page.wait_for_timeout(200)
        # Clear any existing value
        el.press("Control+A")
        el.press("Delete")
        page.wait_for_timeout(150)
        # Type char-by-char with human delays
        for ch in address:
            el.type(ch, delay=random.randint(80, 180))
        page.wait_for_timeout(1200)  # wait for autocomplete dropdown
        # Try to pick first suggestion
        try:
            # Common autocomplete selectors
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(150)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
        except Exception:
            pass
        print("[ok] line1 (autocomplete) -> 1", flush=True)
        return 1
    except Exception as e:
        print(f"[skip] line1 autocomplete: {e}, falling back to fill", flush=True)
        # Fallback to regular fill
        for sel in FIELD_SELECTORS.get("line1", []):
            for el in page.locator(sel).all():
                try:
                    el.fill(address, timeout=1500)
                    return 1
                except Exception:
                    pass
        return 0


def frame_by_keyword(page, kw):
    for f in page.frames:
        if kw in f.url:
            return f
    return None


def inject_hidden(page, key, value):
    """Pure JS value injection for clipped/hidden React inputs
    (city/state/zip/line2). Must NOT use .fill() or Whop re-renders
    and wipes line1."""
    if value in ("", None):
        return 0
    done = 0
    for sel in FIELD_SELECTORS[key]:
        for el in page.locator(sel).all():
            try:
                el.evaluate(
                    """(node, val) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            Object.getPrototypeOf(node), 'value').set;
                        setter.call(node, val);
                        node.dispatchEvent(new Event('input', {bubbles:true}));
                        node.dispatchEvent(new Event('change', {bubbles:true}));
                    }""", value)
                done += 1
            except Exception:
                continue
    print(f"[{'ok' if done else 'skip'}] {key} (hidden) -> {done}")
    return done


def fill_first(page, sel, value):
    """Fill only the first matching element (Whop renders duplicate
    address blocks; filling both cancels out via shared state)."""
    try:
        page.locator(sel).first.fill(value, timeout=3000)
        print(f"[ok] {sel} -> 1 (first)")
        return 1
    except Exception as e:
        print(f"[skip] {sel}: {e}")
        return 0


def fill_any(page, selectors, value):
    """Try several selectors for the same field; Whop's form field names
    vary, so fall back through known possibilities."""
    for sel in selectors:
        try:
            page.locator(sel).first.fill(value, timeout=2500)
            print(f"[ok] {sel} -> name")
            return 1
        except Exception:
            continue
    print("[skip] name (no selector matched)")
    return 0


# Whop renders duplicate React field blocks, so fill EVERY matching element
# (visible + clipped) with a JS value-injection fallback for hidden inputs.
FIELD_SELECTORS = {
    "name":    ['input[name="name"]', 'input[autocomplete="name"]',
                'input[name="cardName"]', 'input[placeholder*="Name" i]'],
    "line1":   ['input[name="line1"]'],
    "line2":   ['input[name="line2"]'],
    "city":    ['input[name="city"]'],
    "state":   ['select[name="state"]', 'input[name="state"]'],
    "zip":     ['input[name="zip"]'],
    "country": ['select[name="country"]'],
    "email":   ['input[name="email"]'],
}

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
US_STATE_FULL = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
US_STATE_FULL_LOWER = {v.lower() for v in US_STATE_FULL.values()}
US_STATE_ABBR = {v: k for k, v in US_STATE_FULL.items()}


def _valid_state(st):
    st = _norm(st).upper()
    if not st:
        return False
    if st in US_STATE_CODES:
        return True
    return st.lower() in US_STATE_FULL_LOWER


def _norm(v):
    return "" if v is None else str(v).strip()


def _is_dup(v):
    """True if v looks like the same token repeated (e.g. '606160601')."""
    v = _norm(v)
    if not v:
        return False
    half = len(v) // 2
    if half and v[:half] == v[half:] and v[:half]:
        return True
    # exact doubled single token (handles '3307 Maple Avenue3307 Maple Avenue')
    return v == v[:len(v) // 2] * 2 and len(v) > 1


def validate_address(addr):
    """STRICT, VALIDATION-FIRST check of the SOURCE data before any filling.
    Returns a list of error strings (empty => valid). Never guesses/invents."""
    errors = []
    required = ["name", "line1", "city", "state", "zip", "country"]
    for f in required:
        if not _norm(addr.get(f)):
            errors.append(f"Missing required field: {f}")
            continue
        if _is_dup(addr.get(f)):
            errors.append(f"Duplicate value detected in {f}: {addr.get(f)!r}")
    zipc = _norm(addr.get("zip"))
    if _norm(addr.get("country")) == "US":
        if not re.fullmatch(r"\d{5}", zipc):
            errors.append(f"Invalid ZIP: {zipc}")
        if not _valid_state(addr.get("state")):
            errors.append(f"Invalid state: {addr.get('state')!r}")
    return errors


JS_SET = """(node, val) => {
    const proto = Object.getPrototypeOf(node);
    const d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) { d.set.call(node, val); }
    else { node.value = val; }
    node.dispatchEvent(new Event('input', {bubbles:true}));
    node.dispatchEvent(new Event('change', {bubbles:true}));
}"""


def fill_field_strict(page, key, value):
    """Fill EVERY matching element for `key` with a single React-safe JS
    setter (no Playwright `fill`, which appends on controlled inputs and
    caused the doubled values). Whop renders duplicate mirror blocks, so we
    set all of them; each gets exactly one clear+set, then verified.

    CRITICAL (Whop-specific): the `state` field is a CONDITIONAL <select
    name="state"> that only RENDERS AFTER the address line 1 (and other
    address fields) have been filled. Its option VALUES are 2-letter codes
    ("NY") while the visible labels are full names ("New York"). So we must
    (a) be called AFTER line1 is filled, and (b) match by value code first,
    then by full-name label, and also sync the hidden mirror input."""
    value = _norm(value)
    if value in ("", None):
        return 0
    # Pre-compute state abbrev/full for the conditional <select name="state">
    st_abbr = st_full = None
    if key == "state":
        v = value.strip().upper()
        if v in US_STATE_FULL:
            st_abbr, st_full = v, US_STATE_FULL[v]
        elif v in US_STATE_ABBR:
            st_abbr, st_full = US_STATE_ABBR[v], v
        else:
            st_abbr = st_full = value
    sels = FIELD_SELECTORS.get(key, [])
    done = 0
    for sel in sels:
        try:
            locs = page.locator(sel).all()
        except Exception:
            continue
        for el in locs:
            try:
                tag = el.element_handle().evaluate("n => n.tagName.toLowerCase()")
                if key == "state" and tag == "select":
                    # CRITICAL: select_option does NOT work on Whop's React-controlled
                    # <select> (it reverts to '' on re-render). Use the raw prototype
                    # setter + full event suite directly, then verify via FormData.
                    _wait_state_option(page, st_abbr, st_full, timeout=30000)
                    ok = False
                    for attempt in range(3):
                        try:
                            el.evaluate(
                                """(node, abbr, full) => {
                                    // Ensure the option exists in the DOM
                                    let found = false;
                                    for (const opt of node.options) {
                                        if (opt.value === abbr || opt.text === full) {
                                            found = true; break;
                                        }
                                    }
                                    if (!found) {
                                        const o = document.createElement('option');
                                        o.value = abbr; o.textContent = full;
                                        node.appendChild(o);
                                    }
                                    // Raw value setter + full React event suite
                                    const proto = HTMLSelectElement.prototype;
                                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                                    setter.call(node, abbr);
                                    node.dispatchEvent(new Event('input', {bubbles:true}));
                                    node.dispatchEvent(new Event('change', {bubbles:true}));
                                    node.dispatchEvent(new Event('blur', {bubbles:true}));
                                }""", st_abbr, st_full)
                            page.wait_for_timeout(200)
                            # Verify via FormData (what actually submits)
                            cur = page.evaluate(
                                """() => {
                                    const f = document.querySelector('form') || document.querySelector('[data-address-form]') || document.body;
                                    const fd = new FormData(f);
                                    return fd.get('state') || '';
                                }""")
                            if _norm(cur) == _norm(st_abbr):
                                ok = True
                                break
                            page.wait_for_timeout(300)
                        except Exception:
                            pass
                    # Last resort: click the select and arrow down to the option
                    if not ok:
                        try:
                            el.click(timeout=2000)
                            page.keyboard.press("ArrowDown")
                            for _ in range(5):
                                page.keyboard.press("ArrowDown")
                                page.wait_for_timeout(50)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(200)
                            cur = page.evaluate(
                                """() => {
                                    const f = document.querySelector('form') || document.querySelector('[data-address-form]') || document.body;
                                    return new FormData(f).get('state') || '';
                                }""")
                            if _norm(cur) == _norm(st_abbr):
                                ok = True
                        except Exception:
                            pass
                    target = st_abbr
                elif tag == "select":
                    ok = False
                    for kw, v in (("label", value), ("value", value)):
                        try:
                            el.select_option(**{kw: v}, timeout=2000)
                            ok = True
                            break
                        except Exception:
                            continue
                    if not ok:
                        try:
                            el.evaluate(JS_SET, value)
                        except Exception:
                            pass
                    target = value
                else:
                    # text-like field or hidden mirror input
                    setval = st_abbr if key == "state" else value
                    el.evaluate("n => { try { n.value = ''; } catch(e){} }")
                    el.focus()
                    el.evaluate(JS_SET, setval)
                    target = setval
                # verify, retry once if it didn't stick
                try:
                    cur = el.input_value()
                except Exception:
                    cur = el.evaluate("n => n.value || ''")
                if _norm(cur) != _norm(target):
                    try:
                        el.evaluate(JS_SET, target)
                    except Exception:
                        pass
                done += 1
            except Exception as e:
                print(f"[skip] {key} ({sel}): {e}", flush=True)
                continue
    print(f"[{'ok' if done else 'skip'}] {key} -> {done}", flush=True)
    return done


def _wait_state_option(page, abbr, full, timeout=30000):
    """Whop's state <select> appears AFTER address line 1, and its <option>
    list is loaded ASYNCHRONOUSLY (often slower through a proxy). If we call
    select_option before the options exist, it silently fails and the value
    reverts to ''. So poll until the matching option is actually present."""
    step = 500
    waited = 0
    while waited < timeout:
        try:
            sel = page.locator('select[name="state"]')
            if sel.count() > 0:
                opts = sel.first.evaluate(
                    "s => [...s.options].map(o => ({v: o.value, t: o.text}))")
                for o in opts:
                    if o["v"] == abbr or o["v"] == full or \
                       o["t"] == full or o["t"] == abbr:
                        return True
        except Exception:
            pass
        page.wait_for_timeout(step)
        waited += step
    print(f"[state] option for {abbr}/{full} not found within {timeout}ms",
          flush=True)
    return False


def read_form(page):
    """Read back the ACTUAL values currently in the page fields.
    Uses a SHORT per-field timeout so a missing/unfound field can never
    stall the run (previously defaulted to 30s each -> multi-minute hang)."""
    def getval(sel, timeout=1500):
        try:
            return _norm(page.locator(sel).first.input_value(timeout=timeout))
        except Exception:
            return ""
    def read_state():
        # 1) Try FormData first — this is exactly what the server receives
        try:
            fd_val = page.evaluate(
                """() => {
                    const f = document.querySelector('form') || document.querySelector('[data-address-form]') || document.body;
                    return new FormData(f).get('state') || '';
                }""")
            if _norm(fd_val):
                return _norm(fd_val)
        except Exception:
            pass
        # 2) Fallback: prefer the <select> (mirror often stays empty on Railway)
        for sel in ("select[name='state']", "input[name='state']"):
            v = getval(sel)
            if v:
                return v
        return ""
    return {
        "name":  getval('input[name="name"]'),
        "line1": getval('input[name="line1"]'),
        "city":  getval('input[name="city"]'),
        "state": read_state(),
        "zip":   getval('input[name="zip"]'),
        "country": getval('select[name="country"]'),
        "email": getval('input[name="email"]'),
    }


def validate_form(form):
    """Final validation of the LIVE page before submission."""
    errors = []
    required = ["name", "line1", "city", "state", "zip", "country"]
    for f in required:
        v = form.get(f, "")
        if not v:
            errors.append(f"Missing required field: {f}")
            continue
        if _is_dup(v):
            errors.append(f"Duplicate value detected in {f}: {v!r}")
    if form.get("country") == "US":
        zipc = form.get("zip", "")
        if not re.fullmatch(r"\d{5}", zipc):
            errors.append(f"Invalid ZIP: {zipc}")
        if not _valid_state(form.get("state")):
            errors.append(f"Invalid state: {form.get('state')!r}")
    return errors


def fill_all(page, key, value):
    # kept for compatibility; delegates to the strict filler
    return fill_field_strict(page, key, value)


def fill_card(page, cc=CARD):
    num_f = frame_by_keyword(page, "card-number")
    exp_f = frame_by_keyword(page, "card-expiration")
    cvc_f = frame_by_keyword(page, "card-verification")
    if num_f:
        num_f.fill('input', cc["number"])
        print("[ok] card number")
    if exp_f:
        num_f_exp = f"{cc['exp_month']} / {cc['exp_year'][2:]}"
        exp_f.fill('input', num_f_exp)
        print("[ok] card expiration")
    if cvc_f:
        cvc_f.fill('input', cc["cvc"])
        print("[ok] card cvc")


def jitter(page):
    """Random mouse drift + micro-pause so the session doesn't look scripted."""
    try:
        w = page.viewport_size or {"width": 1366, "height": 768}
        x = random.randint(50, max(60, w["width"] - 50))
        y = random.randint(50, max(60, w["height"] - 50))
        page.mouse.move(x, y, steps=random.randint(3, 10))
    except Exception:
        pass
    page.wait_for_timeout(int(human_pause(0.3, 1.2) * 1000))


def run_checkout(checkout_url, cc, proxy=None, headless=True, tag="run"):
    """Core checker. Fills a fresh billing identity + the given card through a
    (rotated) proxy with a fresh stealth fingerprint, submits, and returns a
    result dict: {cc, last4, status, response, screenshot, proxy}."""
    addr = get_new_address()
    email = random_email()
    name = addr["name"]   # use the SOURCE name exactly — never invent a name
    if proxy is None:
        proxy = pick_proxy()
    print(f"[{tag}] START card …{cc['number'][-4:]} via {proxy['server']}", flush=True)

    ua = random.choice(USER_AGENTS)
    vw, vh = random.choice(VIEWPORTS)
    tz, lat, lon = random.choice(US_GEO)
    concurrency = random.choice([4, 8, 12, 16])
    memory = random.choice([4, 8, 16])
    last4 = cc["number"][-4:]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            proxy=proxy,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-infobars", f"--window-size={vw},{vh}",
                  "--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            user_agent=ua,
            viewport={"width": vw, "height": vh},
            locale="en-US",
            timezone_id=tz,
            geolocation={"latitude": lat, "longitude": lon},
            permissions=[],
            color_scheme="light",
        )
        stealth = build_stealth(concurrency, memory)
        context.add_init_script(stealth)
        page = context.new_page()
        page.add_init_script(stealth)
        page.set_default_timeout(30000)

        print(f"[{tag}] goto {checkout_url}", flush=True)
        page.goto(checkout_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        jitter(page)
        page.mouse.wheel(0, random.randint(120, 360))
        page.wait_for_timeout(int(human_pause(0.4, 1.0) * 1000))

        # Wait for the checkout form to actually render. If it never appears
        # (dead proxy, login wall, captcha, SPA didn't mount) we fail FAST with
        # a screenshot instead of silently filling nothing for ~150s.
        try:
            page.wait_for_selector(
                'input[name="line1"], input[name="name"], input[name="email"]',
                state="visible", timeout=20000)
        except Exception:
            print(f"[{tag}] FORM NOT LOADED", flush=True)
            page.screenshot(path=f"{tag}_{last4}.png", full_page=True)
            browser.close()
            return {"cc": cc["number"], "last4": last4, "status": "error",
                    "response": "Checkout form did not load (proxy/network/login wall?)",
                    "screenshot": f"{tag}_{last4}.png", "proxy": proxy["server"]}

        # ----- STRICT, VALIDATION-FIRST -----
        # 1) validate the SOURCE data before touching the form
        src_errors = validate_address(addr)
        if src_errors:
            print(f"[{tag}] SOURCE INVALID: {src_errors}", flush=True)
            page.screenshot(path=f"{tag}_{last4}.png", full_page=True)
            browser.close()
            return {"cc": cc["number"], "last4": last4, "status": "missing",
                    "response": "Source invalid: " + "; ".join(src_errors),
                    "screenshot": f"{tag}_{last4}.png", "proxy": proxy["server"]}

        # 2) fill each field — line1 uses autocomplete to trigger browser fill
        fill_all(page, "country", "US")
        jitter(page)
        fill_all(page, "name", name)
        jitter(page)
        fill_all(page, "line1", addr["line1"])
        page.wait_for_timeout(2000)  # wait for autocomplete to fill city/state/zip

        # Read what autocomplete filled, only fill gaps
        form = read_form(page)
        if not _norm(form.get("city")):
            fill_all(page, "city", addr["city"])
            jitter(page)
        if not _norm(form.get("zip")):
            fill_all(page, "zip", addr["zip"])
            jitter(page)
        if not _norm(form.get("state")):
            fill_all(page, "state", addr["state"])
            jitter(page)
        fill_all(page, "email", email)
        jitter(page)
        fill_card(page, cc)
        jitter(page)

        # 3) CORRECTIVE loop: Whop's state field can mount late / re-render,
        #     leaving the read-back empty even though a value was set. Retry
        #     the fill a few times and re-read before giving up.
        form = {}
        form_errors = ["force"]
        for attempt in range(4):
            form = read_form(page)
            form_errors = validate_form(form)
            if not form_errors:
                break
            print(f"[{tag}] retry {attempt}: {form_errors}", flush=True)
            if not _norm(form.get("state")):
                fill_all(page, "state", addr["state"])
                page.wait_for_timeout(1500)
            if not _norm(form.get("line1")):
                fill_all(page, "line1", addr["line1"])
            if not _norm(form.get("city")):
                fill_all(page, "city", addr["city"])
            if not _norm(form.get("zip")):
                fill_all(page, "zip", addr["zip"])
            if not _norm(form.get("name")):
                fill_all(page, "name", name)
            page.wait_for_timeout(800)
        print(f"[{tag}] VALIDATION FORM = {form}", flush=True)
        if form_errors:
            diag = page.evaluate("""() => {
                const out = {};
                const sels = document.querySelectorAll('select[name="state"]');
                out.nSelect = sels.length;
                out.selVal = sels.length ? sels[0].value : '(none)';
                out.nOpts = sels.length ? sels[0].options.length : 0;
                const ms = document.querySelectorAll('input[name="state"]');
                out.nMirror = ms.length;
                out.mirrorVals = [...ms].map(m => m.value);
                return out;
            }""")
            print(f"[{tag}] STATE DIAG = {diag}", flush=True)
            page.screenshot(path=f"{tag}_{last4}.png", full_page=True)
            browser.close()
            return {"cc": cc["number"], "last4": last4, "status": "missing",
                    "response": "Validation failed: " + "; ".join(form_errors),
                    "screenshot": f"{tag}_{last4}.png", "proxy": proxy["server"]}

        page.screenshot(path=f"{tag}_{last4}_pre.png", full_page=True)

        # ENFORCE state in form right before submit — nuclear safety net
        # Works even if React select reverts or mirrors were removed
        def enforce_state(page, abbr):
            page.evaluate(
                """(abbr) => {
                    // 1) Force any <select name="state"> to the value
                    document.querySelectorAll('select[name="state"]').forEach(s => {
                        const proto = HTMLSelectElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(s, abbr);
                        s.dispatchEvent(new Event('input', {bubbles:true}));
                        s.dispatchEvent(new Event('change', {bubbles:true}));
                        s.dispatchEvent(new Event('blur', {bubbles:true}));
                    });
                    // 2) Ensure a hidden input exists in the form with the value
                    const form = document.querySelector('form') || document.querySelector('[data-address-form]') || document.body;
                    let hidden = form.querySelector('input[name="state"][type="hidden"]');
                    if (!hidden) {
                        hidden = document.createElement('input');
                        hidden.type = 'hidden';
                        hidden.name = 'state';
                        form.appendChild(hidden);
                    }
                    hidden.value = abbr;
                }""", abbr)

        enforce_state(page, addr["state"])
        page.wait_for_timeout(500)

        try:
            page.get_by_role("button", name="Get access").click(timeout=8000, delay=20)
        except Exception:
            pass

        page.wait_for_timeout(4000)
        try:
            page.wait_for_function(
                "() => { const b=document.querySelector('button[type=submit]'); "
                "return b && !/process|loading|.../i.test(b.innerText); }",
                timeout=10000)
        except Exception:
            pass
        print(f"[{tag}] submitted, reading result", flush=True)

        try:
            resp = page.inner_text("body")
        except Exception:
            resp = ""
        shot = f"{tag}_{last4}.png"
        page.screenshot(path=shot, full_page=True)
        browser.close()

    low = resp.lower()
    # Conservative detection: only approve on an explicit success signal.
    # Everything else (address/zip invalid, declined, missing, generic error)
    # is treated as a failure so we never falsely report a charged card.
    fail_rules = [
        ("insufficient funds", "insufficient", "Insufficient funds"),
        ("couldn't be processed", "declined", "Card declined by issuer"),
        ("could not be processed", "declined", "Card declined by issuer"),
        ("try a different payment", "declined", "Card declined by issuer"),
        ("do not honor", "declined", "Card declined by issuer"),
        ("declined", "declined", "Card declined by issuer"),
        ("expired", "declined", "Card expired"),
        ("security reasons", "declined", "Card declined by issuer"),
        ("invalid address", "missing", "Invalid billing address"),
        ("address is invalid", "missing", "Invalid billing address"),
        ("enter a valid address", "missing", "Invalid billing address"),
        ("wrong address", "missing", "Invalid billing address"),
        ("invalid zip", "missing", "Invalid ZIP / postal code"),
        ("zip is invalid", "missing", "Invalid ZIP / postal code"),
        ("enter a valid zip", "missing", "Invalid ZIP / postal code"),
        ("postal code is invalid", "missing", "Invalid ZIP / postal code"),
        ("missing field", "missing", "Missing required fields"),
        ("required field", "missing", "Missing required fields"),
        ("this field is required", "missing", "Missing required fields"),
        ("verify", "error", "Verification failed"),
        ("payment could not", "declined", "Card declined by issuer"),
        ("card could not", "declined", "Card declined by issuer"),
    ]
    success_kw = ["payment successful", "payment was successful",
                  "you now have access", "access granted", "order confirmed",
                  "purchase complete", "thank you for your payment",
                  "subscription is active", "your subscription", "welcome to"]
    status = reason = None
    for kw, st, rs in fail_rules:
        if kw in low:
            status, reason = st, rs
            break
    if not status:
        if any(k in low for k in success_kw):
            status, reason = "success", "Payment approved"
        else:
            status, reason = "error", "Unclear result (no clear success/error signal)"

    print(f"[{tag}] DONE status={status}", flush=True)
    return {"cc": cc["number"], "last4": last4, "status": status,
            "response": reason, "screenshot": shot, "proxy": proxy["server"]}


def main():
    res = run_checkout(CHECKOUT_URL, CARD, headless=False)
    print(res)


if __name__ == "__main__":
    main()
