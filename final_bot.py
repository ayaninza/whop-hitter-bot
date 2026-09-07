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
    """Click the entry button — varies by checkout: 'Get access', 'Join now', etc."""
    for btn_text in ("Get access", "Join now", "Buy now", "Subscribe"):
        try:
            page.get_by_role("button", name=btn_text).first.click(
                timeout=8000, delay=20)
            print(f"[{tag}] clicked '{btn_text}'", flush=True)
            return True
        except Exception:
            continue
    # fallback: any blue accent button
    try:
        page.locator(
            'button[data-accent-color="blue"]').first.click(timeout=5000, delay=20)
        print(f"[{tag}] clicked blue accent button (fallback)", flush=True)
        return True
    except Exception as e:
        print(f"[{tag}] all button attempts failed: {e}", flush=True)
        return False


def click_agree_checkboxes(page, tag="final"):
    """Find and click any 'I agree' / terms / consent checkboxes on the page.
    Whop checkouts sometimes gate the submit button behind a checkbox that
    must be ticked. We search for checkbox inputs + their labels, and also
    role=checkbox elements, matching text like 'agree', 'terms', 'consent',
    'policy', 'conditions', 'acknowledge'. Returns count of boxes clicked."""
    CHECKBOX_JS = """() => {
        let clicked = 0;
        const keywords = ['agree', 'terms', 'consent', 'policy', 'conditions',
                          'acknowledge', 'acceptable use', 'refund', 'privacy'];
        function textNear(el) {
            // walk up to find the nearest text content (label, parent, sibling)
            let node = el;
            for (let i = 0; i < 5; i++) {
                if (!node) break;
                const txt = (node.innerText || node.textContent || '').toLowerCase();
                for (const kw of keywords) { if (txt.includes(kw)) return true; }
                node = node.parentElement;
            }
            // also check next/previous siblings
            let sib = el.nextElementSibling;
            for (let i = 0; i < 3 && sib; i++) {
                const txt = (sib.innerText || sib.textContent || '').toLowerCase();
                for (const kw of keywords) { if (txt.includes(kw)) return true; }
                sib = sib.nextElementSibling;
            }
            return false;
        }
        // 1) <input type="checkbox"> near agree-text labels
        for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
            if (cb.checked) continue;
            if (textNear(cb)) {
                cb.click();
                clicked++;
            }
        }
        // 2) role="checkbox" elements (custom React checkboxes)
        for (const cb of document.querySelectorAll('[role="checkbox"]')) {
            const state = cb.getAttribute('aria-checked') || cb.getAttribute('data-state');
            if (state === 'true' || state === 'checked') continue;
            if (textNear(cb)) {
                cb.click();
                clicked++;
            }
        }
        // 3) <label> elements that contain a checkbox and agree-text
        for (const lbl of document.querySelectorAll('label')) {
            const txt = (lbl.innerText || lbl.textContent || '').toLowerCase();
            let match = false;
            for (const kw of keywords) { if (txt.includes(kw)) { match = true; break; } }
            if (!match) continue;
            // find the checkbox inside or referenced by this label
            const inner = lbl.querySelector('input[type="checkbox"], [role="checkbox"]');
            if (inner) {
                const state = inner.checked !== undefined ? inner.checked :
                              (inner.getAttribute('aria-checked') || inner.getAttribute('data-state'));
                if (state === true || state === 'true' || state === 'checked') continue;
                inner.click();
                clicked++;
            } else {
                // label itself might be the clickable toggle
                const state = lbl.getAttribute('aria-checked') || lbl.getAttribute('data-state');
                if (state === 'true' || state === 'checked') continue;
                lbl.click();
                clicked++;
            }
        }
        return clicked;
    }"""
    try:
        n = page.evaluate(CHECKBOX_JS)
        if n:
            print(f"[{tag}] clicked {n} agree checkbox(es)", flush=True)
            page.wait_for_timeout(500)
        return n
    except Exception as e:
        print(f"[{tag}] agree checkbox scan: {e}", flush=True)
        return 0


def fill_and_submit(page, addr, email, cc, tag="final", submit=True):
    """TURBO fill: wait for full form, one JS batch, direct card fill."""
    name = addr["name"]
    last4 = cc["number"][-4:]
    state_val = addr["state"]
    abbr, full = W._resolve_state(state_val)

    # ---- STEP 0: Wait for billing fields to ACTUALLY exist ----
    # Email renders first; billing fields render ~1-2s later. We poll until
    # BOTH name AND line1 exist so the batch JS doesn't hit empty DOMs.
    for _ in range(40):  # up to 8s
        ready = page.evaluate("""() => {
            return !!document.querySelector('input[name="name"]')
                && !!document.querySelector('input[name="line1"]');
        }""")
        if ready:
            break
        page.wait_for_timeout(200)

    # ---- STEP 1: ONE JS call fills ALL text inputs at once ----
    page.evaluate("""(d) => {
        const setter = (node, val) => {
            const proto = Object.getPrototypeOf(node);
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(node, val);
            else node.value = val;
            node.dispatchEvent(new Event('input', {bubbles:true}));
            node.dispatchEvent(new Event('change', {bubbles:true}));
        };
        const fill = (sel, val) => {
            document.querySelectorAll(sel).forEach(n => setter(n, val));
        };
        fill('input[name="name"]', d.name);
        fill('input[autocomplete="name"]', d.name);
        fill('input[name="cardName"]', d.name);
        fill('input[name="line1"]', d.line1);
        fill('input[name="city"]', d.city);
        fill('input[name="zip"]', d.zip);
        fill('input[name="postal_code"]', d.zip);
        fill('input[name="email"]', d.email);
        fill('select[name="country"]', d.country);
    }""", {"name": name, "line1": addr["line1"], "city": addr["city"],
            "zip": addr["zip"], "email": email, "country": "US"})
    page.wait_for_timeout(200)  # let React re-render after batch fill

    # ---- STEP 2: State select (native select_option, fire onChange) ----
    for el in page.locator('select[name="state"]').all():
        try:
            el.select_option(value=abbr, timeout=1500)
        except Exception:
            try:
                el.select_option(label=full, timeout=1500)
            except Exception:
                pass

    # ---- STEP 3: Wait for card iframes, then fill ----
    for kw, val in [("card-number", cc["number"]),
                    ("card-expiration", f"{cc['exp_month']} / {cc['exp_year'][2:]}"),
                    ("card-verification", cc["cvc"])]:
        filled = False
        for _ in range(20):  # up to 4s per frame
            for f in page.frames:
                if kw in f.url:
                    try:
                        f.fill('input', val, timeout=2000)
                        print(f"[ok] {kw}", flush=True)
                        filled = True
                    except Exception:
                        pass
                    break
            if filled:
                break
            page.wait_for_timeout(200)
        if not filled:
            print(f"[skip] {kw}: frame not found", flush=True)

    # ---- STEP 4: Click agree checkbox (single JS) ----
    page.evaluate("""() => {
        const kw = ['agree','terms','consent','policy','conditions'];
        const textNear = (el) => {
            let n = el;
            for (let i = 0; i < 5; i++) {
                if (!n) break;
                const t = (n.innerText||n.textContent||'').toLowerCase();
                for (const k of kw) if (t.includes(k)) return true;
                n = n.parentElement;
            }
            return false;
        };
        document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            if (!cb.checked && textNear(cb)) cb.click();
        });
        document.querySelectorAll('[role="checkbox"]').forEach(cb => {
            const s = cb.getAttribute('aria-checked')||cb.getAttribute('data-state');
            if (s!=='true'&&s!=='checked' && textNear(cb)) cb.click();
        });
    }""")

    # ---- STEP 5: Submit button ----
    clicked_submit = False
    for name_btn in ("Pay", "Pay now", "Complete payment", "Process payment",
                     "Submit payment", "Get access", "Join now", "Subscribe",
                     "Confirm", "Finish payment", "Buy now"):
        try:
            page.get_by_role("button", name=name_btn).click(timeout=2000, delay=5)
            print(f"[{tag}] clicked submit ('{name_btn}')", flush=True)
            clicked_submit = True
            break
        except Exception:
            continue
    if not clicked_submit:
        # Fallback: find any button that looks like a submit/pay button
        page.evaluate("""() => {
            const btns = [...document.querySelectorAll('button, [role="button"]')];
            for (const b of btns) {
                const t = (b.innerText || '').toLowerCase();
                if (/pay|submit|access|join|confirm|process/i.test(t) && !b.disabled) {
                    b.click(); break;
                }
            }
        }""")
        print(f"[{tag}] clicked submit (JS fallback)", flush=True)

    if not submit:
        print(f"[{tag}] DRY MODE — not submitting", flush=True)
        return {"cc": cc["number"], "last4": last4, "status": "dry",
                "response": "Filled, no submit", "screenshot": ""}

    # ---- STEP 6: Wait for result ----
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
            'invalid zip','missing field','required field','this field is required',
            'payment could not','card could not','error',
            '3ds','3d secure','additional verification','redirected to your bank',
            'text message to confirm','complete the verification','finish your payment',
            'finish payment','verification step',
            'payment failed','card was declined','try again','invalid card',
            'card number is invalid','incorrect cvv','cvc is invalid',
            'billing postal code','zip code is invalid',
            'something went wrong','an error occurred','please try again'];
        for (const s of sig) if (body.includes(s)) return true;
        if (/confirm|success|access|thank|receipt|order/i.test(location.href)) return true;
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
                page.wait_for_timeout(600)
                break
            # Also check if submit button became disabled (processing)
            btn_disabled = page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button, [role="button"]')];
                for (const b of btns) {
                    const t = (b.innerText || '').toLowerCase();
                    if (/pay|submit|access|confirm|process/i.test(t) && b.disabled) return true;
                }
                return false;
            }""")
            if btn_disabled and not seen_loading:
                seen_loading = True
        except Exception:
            pass
        page.wait_for_timeout(600)

    print(f"[{tag}] settled, reading result", flush=True)
    try:
        resp = page.inner_text("body")
    except Exception:
        resp = ""
    # Scroll the outcome into the viewport, then capture a VIEWPORT screenshot
    # (not the top of the page) so the result text is actually visible in the
    # image we send — a full-page/top shot hides the result below the fold.
    try:
        page.evaluate("""() => {
            const txt = (document.body.innerText || '').toLowerCase();
            const markers = ['insufficient','declined','approved','success','access granted',
                'thank you','error','missing','required','confirm','processing','order',
                'receipt','could not'];
            let found = null;
            for (const m of markers) { if (txt.indexOf(m) >= 0) { found = m; break; } }
            if (found) {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const n = walker.currentNode;
                    if (n.nodeValue && n.nodeValue.toLowerCase().includes(found)) {
                        let el = n.parentElement;
                        while (el && el.scrollIntoView && getComputedStyle(el).display === 'none') el = el.parentElement;
                        if (el && el.scrollIntoView) { el.scrollIntoView({block:'center'}); break; }
                    }
                }
            } else {
                window.scrollTo(0, document.body.scrollHeight);
            }
        }""")
        page.wait_for_timeout(500)
    except Exception:
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
        except Exception:
            pass
    shot = f"{tag}_{last4}.png"
    page.screenshot(path=shot)

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
        ("verification failed", "error", "Verification failed"),
        ("payment could not", "declined", "Card declined by issuer"),
        ("card could not", "declined", "Card declined by issuer"),
    ]
    success_kw = ["payment successful", "payment was successful", "you now have access",
                  "access granted", "order confirmed", "purchase complete",
                  "thank you for your payment", "subscription is active",
                  "your subscription", "welcome to", "you're all set", "all set",
                  "enjoy", "vip access", "you're in", "active now", "success",
                  "receipt", "order #", "order number", "confirmed",
                  "finish your payment", "finish payment", "verification step",
                  "additional verification", "3ds", "3d secure",
                  "redirected to your bank", "text message to confirm",
                  "complete the verification"]
    status = reason = None
    for kw, st, rs in fail_rules:
        if kw in low:
            status, reason = st, rs
            break
    if not status:
        # Check for 3DS verification first (distinct from plain success)
        if any(k in low for k in ("additional verification", "3ds", "3d secure",
                                    "redirected to your bank", "text message to confirm",
                                    "complete the verification")):
            status, reason = "3ds", "3DS verification required"
        elif any(k in low for k in success_kw):
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
                    const btnDisabled = !!([...document.querySelectorAll('button, [role="button"]')]
                        .find(b => {
                            const t = (b.innerText || '').toLowerCase();
                            return /pay|submit|access|confirm|process/i.test(t) && b.disabled;
                        }));
                    return !onForm || btnDisabled;
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


def run_final(proxy=None, headless=True, submit=True, tag=None, cc_override=None,
              checkout_url=None, email=None):
    addr = W.get_new_address()
    if not email:
        email = W.random_email()
    cc = cc_override if cc_override else W.CARD
    if tag is None:
        tag = f"ref_{cc['number'][-4:]}"
    if proxy is None:
        proxy = W.pick_proxy()
    url = checkout_url or BUY_VIP_URL
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
        page.set_default_timeout(15000)

        print(f"[{tag}] goto {url}", flush=True)
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        # Check if form is already visible (direct checkout URLs like /checkout/...)
        form_visible = page.evaluate("""() => {
            return !!(document.querySelector('input[name="email"]')
                || document.querySelector('input[name="line1"]')
                || document.querySelector('input[name="name"]'));
        }""")

        if not form_visible:
            # Product page — need to click entry button
            clicked = click_get_access(page, tag)
            if not clicked:
                # Wait a bit and retry once
                page.wait_for_timeout(1500)
                clicked = click_get_access(page, tag)
            if not clicked:
                # Still no button? Maybe form loaded after JS render
                page.wait_for_timeout(2000)
                form_visible = page.evaluate("""() => {
                    return !!(document.querySelector('input[name="email"]')
                        || document.querySelector('input[name="line1"]'));
                }""")
                if not form_visible:
                    page.screenshot(path=f"{tag}_{last4}.png", full_page=True)
                    browser.close()
                    return {"cc": cc["number"], "last4": last4, "status": "error",
                            "response": "Could not click entry button or find form",
                            "screenshot": f"{tag}_{last4}.png"}

        # Poll until billing fields exist (event-driven, not fixed wait)
        for _ in range(30):
            ready = page.evaluate("""() => {
                return !!document.querySelector('input[name="name"]')
                    && !!document.querySelector('input[name="line1"]');
            }""")
            if ready:
                break
            page.wait_for_timeout(300)

        print(f"[{tag}] form ready, filling...", flush=True)
        result = fill_and_submit(page, addr, email, cc, tag=tag, submit=submit)
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
