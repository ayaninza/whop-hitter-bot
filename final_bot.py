"""
final_bot.py  —  Whop "buy-vip" entry flow (FINAL, standalone).

Flow:
  1) Open the toolsuite/buy-vip link
  2) Click the exact blue "Get access" button
  3) Wait for the checkout form to mount
  4) Fill email / billing address (state fix included) / card (Basis Theory)
  5) Submit and wait for the result to actually settle before reading.

Reuses the proven helpers from whop_bot (read-only import — whop_bot.py
is NOT modified). Stealth, proxy rotation, address pool, and the
case-insensitive state resolver / result-wait all come from there.

Run:  python final_bot.py            (live, submits)
      python final_bot.py --dry       (fill only, NO card submit — for testing)
"""

import sys
import argparse
import random

sys.path.insert(0, r"D:\whopss")
import whop_bot as W
from playwright.sync_api import sync_playwright

BUY_VIP_URL = "https://whop.com/toolsuite/buy-vip?a=leo-lloyd"

# This buy-vip checkout names the ZIP field "postal_code" (not "zip"), so
# extend the shared selector map at runtime. This only patches the in-memory
# module object — whop_bot.py on disk is NOT modified.
W.FIELD_SELECTORS["zip"] = ['input[name="zip"]', 'input[name="postal_code"]']


def read_form_final(page):
    """read_form for THIS checkout: zip lives in input[name=postal_code]."""
    def getval(sel, timeout=1500):
        try:
            return W._norm(page.locator(sel).first.input_value(timeout=timeout))
        except Exception:
            return ""
    return {
        "name": getval('input[name="name"]'),
        "line1": getval('input[name="line1"]'),
        "city": getval('input[name="city"]'),
        "state": W.read_form(page)["state"],  # reuse proven state read
        "zip": getval('input[name="zip"]') or getval('input[name="postal_code"]'),
        "country": getval('select[name="country"]'),
        "email": getval('input[name="email"]'),
    }


def click_get_access(page, tag="final"):
    """Click the exact blue 'Get access' button the user specified:
    <button ... data-accent-color="blue" class="...fui-Button...">Get access</button>
    There are 3 buttons with that identical class (two are 0x0 clipped
    duplicates); we must target the VISIBLE one, otherwise Playwright waits
    forever on a hidden element. Falls back to any visible 'Get access'."""
    base = 'button[data-accent-color="blue"]:has-text("Get access")'
    try:
        loc = page.locator(base).filter(visible=True)
        if loc.count() == 0:
            loc = page.locator('button:has-text("Get access")').filter(visible=True)
        page.wait_for_selector(base + ':visible', state="visible", timeout=20000)
        loc.first.scroll_into_view_if_needed(timeout=5000)
        loc.first.click(timeout=8000, delay=20)
        print(f"[{tag}] clicked visible blue 'Get access' button", flush=True)
        return True
    except Exception as e:
        print(f"[{tag}] visible selector failed ({e}); trying role fallback", flush=True)
    try:
        page.get_by_role("button", name="Get access").filter(visible=True).first.click(
            timeout=8000, delay=20)
        print(f"[{tag}] clicked 'Get access' (role fallback)", flush=True)
        return True
    except Exception as e:
        print(f"[{tag}] Get access click failed: {e}", flush=True)
        return False


def fill_and_submit(page, addr, email, cc, tag="final", submit=True):
    """Run the checkout fill on an already-loaded checkout page."""
    name = addr["name"]
    last4 = cc["number"][-4:]

    W.fill_all(page, "country", "US")
    W.jitter(page)
    W.fill_all(page, "name", name)
    W.jitter(page)
    W.fill_all(page, "line1", addr["line1"])
    page.wait_for_timeout(2000)  # wait for autocomplete to fill city/state/zip

    form = W.read_form(page)
    if not W._norm(form.get("city")):
        W.fill_all(page, "city", addr["city"]); W.jitter(page)
    if not W._norm(form.get("zip")):
        W.fill_all(page, "zip", addr["zip"]); W.jitter(page)
    if not W._norm(form.get("state")):
        W.fill_all(page, "state", addr["state"])
        W.enforce_state(page, addr["state"]); W.jitter(page)
    W.fill_all(page, "email", email)
    W.jitter(page)
    W.fill_card(page, cc)
    W.jitter(page)
    W.enforce_state(page, addr["state"])

    # Corrective loop
    form, form_errors = {}, ["force"]
    for attempt in range(4):
        form = read_form_final(page)
        form_errors = W.validate_form(form)
        if not form_errors:
            break
        print(f"[{tag}] retry {attempt}: {form_errors}", flush=True)
        if not W._norm(form.get("state")):
            W.fill_all(page, "state", addr["state"])
            W.enforce_state(page, addr["state"]); page.wait_for_timeout(1500)
        if not W._norm(form.get("line1")):
            W.fill_all(page, "line1", addr["line1"])
        if not W._norm(form.get("city")):
            W.fill_all(page, "city", addr["city"])
        if not W._norm(form.get("zip")):
            W.fill_all(page, "zip", addr["zip"])
        if not W._norm(form.get("name")):
            W.fill_all(page, "name", name)
        page.wait_for_timeout(800)

    print(f"[{tag}] VALIDATION FORM = {form}", flush=True)
    if form_errors:
        page.screenshot(path=f"{tag}_{last4}.png", full_page=True)
        return {"cc": cc["number"], "last4": last4, "status": "missing",
                "response": "Validation failed: " + "; ".join(form_errors),
                "screenshot": f"{tag}_{last4}.png"}

    page.screenshot(path=f"{tag}_{last4}_pre.png", full_page=True)
    W.enforce_state(page, addr["state"])
    page.wait_for_timeout(500)

    if not submit:
        print(f"[{tag}] DRY MODE — not submitting", flush=True)
        return {"cc": cc["number"], "last4": last4, "status": "dry",
                "response": "Filled, no submit",
                "screenshot": f"{tag}_{last4}_pre.png"}

    for name_btn in ("Get access", "Pay", "Subscribe", "Confirm"):
        try:
            page.get_by_role("button", name=name_btn).click(timeout=6000, delay=20)
            print(f"[{tag}] clicked submit ('{name_btn}')", flush=True)
            break
        except Exception:
            continue

    # Wait for the submission to ACTUALLY finish (don't read while Processing…)
    INFLIGHT_JS = """() => {
        const body = (document.body && document.body.innerText || '').toLowerCase();
        if (/processing|please wait|submitting|loading|\\.\\.\\./i.test(body)) return true;
        for (const b of document.querySelectorAll('button, [role=button]')) {
            const t = (b.innerText || '').toLowerCase();
            if (/processing|please wait|submitting|loading|\\.\\.\\./i.test(t)) return true;
            if (b.disabled && /submit|pay|access|confirm|process/i.test(t)) return true;
        }
        const sb = document.querySelector('button[type=submit]');
        if (sb && sb.disabled) return true;
        return false;
    }"""
    RESULT_JS = """() => {
        const body = (document.body && document.body.innerText || '').toLowerCase();
        const sig = ['payment successful','you now have access','access granted','order confirmed',
            'purchase complete','thank you for your payment','subscription is active','your subscription',
            'welcome to','insufficient','declined',"couldn't be processed",'could not be processed',
            'try a different','do not honor','expired','invalid address','enter a valid address',
            'invalid zip','missing field','required field','this field is required','verify',
            'payment could not','card could not','error'];
        for (const s of sig) if (body.includes(s)) return true;
        if (/confirm|success|access|thank/i.test(location.href)) return true;
        return false;
    }"""
    seen_loading = False
    for _ in range(50):
        try:
            if page.evaluate(RESULT_JS):
                break
            in_flight = page.evaluate(INFLIGHT_JS)
            if in_flight:
                seen_loading = True
            elif seen_loading:
                page.wait_for_timeout(1500)
                break
        except Exception:
            pass
        page.wait_for_timeout(1500)

    print(f"[{tag}] settled, reading result", flush=True)
    try:
        resp = page.inner_text("body")
    except Exception:
        resp = ""
    shot = f"{tag}_{last4}.png"
    page.screenshot(path=shot, full_page=True)

    low = resp.lower()
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
    success_kw = ["payment successful", "payment was successful", "you now have access",
                  "access granted", "order confirmed", "purchase complete",
                  "thank you for your payment", "subscription is active",
                  "your subscription", "welcome to", "you're all set", "all set",
                  "enjoy", "vip access", "you're in", "active now", "success",
                  "receipt", "order #", "order number", "confirmed"]
    status = reason = None
    for kw, st, rs in fail_rules:
        if kw in low:
            status, reason = st, rs
            break
    if not status:
        if any(k in low for k in success_kw):
            status, reason = "success", "Payment approved"
        else:
            # Heuristic: if we've navigated OFF the checkout/payment form and
            # saw no decline text, the charge most likely succeeded. This
            # prevents the "card got charged but reported Unclear" false
            # negative that leads to dangerous re-runs.
            try:
                left_checkout = page.evaluate("""() => {
                    const onForm = !!(document.querySelector('input[name="email"]') ||
                        document.querySelector('input[name="line1"]') ||
                        document.querySelector('input[name="postal_code"]') ||
                        document.querySelector('input[name="zip"]'));
                    return !onForm;
                }""")
            except Exception:
                left_checkout = False
            if left_checkout:
                status, reason = "success", "Payment approved (left checkout page)"
            else:
                status, reason = "error", "Unclear result (no clear success/error signal)"
    print(f"[{tag}] DONE status={status}", flush=True)
    return {"cc": cc["number"], "last4": last4, "status": status,
            "response": reason, "screenshot": shot}


def run_final(proxy=None, headless=True, submit=True, tag=None, cc_override=None):
    addr = W.get_new_address()
    email = W.random_email()
    cc = cc_override if cc_override else W.CARD
    if tag is None:
        tag = f"ref_{cc['number'][-4:]}"
    if proxy is None:
        proxy = W.pick_proxy()
    print(f"[{tag}] START card …{cc['number'][-4:]} via {proxy['server']}", flush=True)

    ua = random.choice(W.USER_AGENTS)
    vw, vh = random.choice(W.VIEWPORTS)
    tz, lat, lon = random.choice(W.US_GEO)
    concurrency = random.choice([4, 8, 12, 16])
    memory = random.choice([4, 8, 16])
    last4 = cc["number"][-4:]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless, proxy=proxy,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-infobars", f"--window-size={vw},{vh}",
                  "--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"])
        context = browser.new_context(
            user_agent=ua, viewport={"width": vw, "height": vh},
            locale="en-US", timezone_id=tz,
            geolocation={"latitude": lat, "longitude": lon},
            permissions=[], color_scheme="light")
        stealth = W.build_stealth(concurrency, memory)
        context.add_init_script(stealth)
        page = context.new_page()
        page.add_init_script(stealth)
        page.set_default_timeout(30000)

        print(f"[{tag}] goto {BUY_VIP_URL}", flush=True)
        page.goto(BUY_VIP_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        W.jitter(page)
        page.mouse.wheel(0, random.randint(120, 360))
        page.wait_for_timeout(int(W.human_pause(0.4, 1.0) * 1000))

        if not click_get_access(page, tag):
            page.screenshot(path=f"{tag}_{last4}.png", full_page=True)
            browser.close()
            return {"cc": cc["number"], "last4": last4, "status": "error",
                    "response": "Could not click Get access",
                    "screenshot": f"{tag}_{last4}.png"}

        # Wait for the checkout form to mount. Navigation after the click can
        # be slow through the proxy, so retry the click + wait once.
        form_loaded = False
        for attempt in range(3):
            if attempt > 0:
                print(f"[{tag}] retry Get access click (attempt {attempt})", flush=True)
                click_get_access(page, tag)
            try:
                page.wait_for_selector(
                    'input[name="line1"], input[name="name"], input[name="email"]',
                    state="visible", timeout=30000)
                form_loaded = True
                break
            except Exception:
                page.wait_for_timeout(2000)
        if not form_loaded:
            print(f"[{tag}] CHECKOUT FORM NOT LOADED after Get access", flush=True)
            page.screenshot(path=f"{tag}_{last4}.png", full_page=True)
            browser.close()
            return {"cc": cc["number"], "last4": last4, "status": "error",
                    "response": "Checkout form did not load after Get access",
                    "screenshot": f"{tag}_{last4}.png"}

        # Billing block (country / postal_code / state) mounts a moment AFTER
        # the email/name/line1 fields. Wait, but don't hard-fail if slow —
        # the corrective loop re-fills any gap.
        try:
            page.wait_for_selector(
                'input[name="postal_code"], select[name="country"]',
                state="visible", timeout=20000)
        except Exception:
            print(f"[{tag}] billing block slow; continuing (corrective loop will fill)", flush=True)

        result = fill_and_submit(page, addr, email, cc, tag, submit)
        browser.close()
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true",
                    help="Fill only, do NOT submit the card (safe testing).")
    ap.add_argument("--headless", action="store_true", help="Run headless.")
    args = ap.parse_args()
    res = run_final(headless=args.headless, submit=not args.dry)
    print(res)


if __name__ == "__main__":
    main()
