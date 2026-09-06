"""
test_ref_flow.py — TURBO debug test: zero-wait fill + instant click
"""
import sys, os, random
sys.path.insert(0, r"D:\whopss")
import whop_bot as W
from final_bot import (click_get_access, click_agree_checkboxes,
                        read_form_final, fill_and_submit)
from playwright.sync_api import sync_playwright

TARGET_URL = "https://whop.com/option-trading-trial/honey-drip-network-affiliates-17?a=huskymanikin4a"

CARD = {
    "number": "5328398287077228",
    "exp_month": "05",
    "exp_year": "2029",
    "cvc": "211",
}

W.FIELD_SELECTORS["zip"] = ['input[name="zip"]', 'input[name="postal_code"]']


def main():
    addr = W.get_new_address()
    email = W.random_email()
    tag = "debug_ref"
    last4 = CARD["number"][-4:]

    ua = random.choice(W.USER_AGENTS)
    vw, vh = random.choice(W.VIEWPORTS)
    tz, lat, lon = random.choice(W.US_GEO)
    concurrency = random.choice([4, 8, 12, 16])
    memory = random.choice([4, 8, 16])

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
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

        print(f"[{tag}] goto {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(500)

        # Click entry button
        clicked = click_get_access(page, tag)
        print(f"[{tag}] entry button clicked: {clicked}")

        # Wait for checkout form (event-driven)
        form_loaded = False
        for attempt in range(3):
            if attempt > 0:
                click_get_access(page, tag)
            try:
                page.wait_for_selector(
                    'input[name="line1"], input[name="name"], input[name="email"]',
                    state="visible", timeout=10000)
                form_loaded = True
                break
            except Exception:
                page.wait_for_timeout(500)

        if not form_loaded:
            print(f"[{tag}] CHECKOUT FORM NOT LOADED")
            browser.close()
            return

        # Fill + submit
        result = fill_and_submit(page, addr, email, CARD, tag=tag, submit=True)
        print(f"[{tag}] result: {result}")

        # Wait for result (event-driven)
        page.wait_for_timeout(3000)

        shot = f"step5_result_{last4}.png"
        page.screenshot(path=shot, full_page=True)
        print(f"[{tag}] screenshot saved ({shot})")

        resp = page.inner_text("body")
        print(f"\nRESULT: {resp[:300]}")
        print(f"EMAIL: {email}")
        print(f"ADDRESS: {addr}")
        browser.close()


if __name__ == "__main__":
    main()
