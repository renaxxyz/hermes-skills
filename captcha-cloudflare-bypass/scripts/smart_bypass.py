#!/usr/bin/env python3
"""Smart anti-bot bypass — tries curl_cffi → cloudscraper → CloakBrowser automatically.

Usage:
    python3 smart_bypass.py <url>              # API/page fetch (tiers 1-2)
    python3 smart_bypass.py <url> --js          # Full JS rendering (adds tier 3)
    python3 smart_bypass.py <url> --js --save /tmp/out.html  # Save content
"""
import sys
import time
import json

def tier1_curl_cffi(url: str) -> dict | None:
    """Tier 1: curl_cffi with browser-grade TLS fingerprint."""
    try:
        from curl_cffi import requests as cf_requests
        r = cf_requests.get(url, impersonate="chrome", timeout=15, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": url.rsplit("/", 1)[0] + "/",
        })
        if r.status_code == 200:
            lower = r.text[:500].lower()
            if not any(m in lower for m in ["challenge", "captcha", "just a moment", "turnstile"]):
                return {"tier": "curl_cffi", "status": r.status_code, "content": r.text, "headers": dict(r.headers)}
        return None
    except Exception as e:
        return {"error": f"curl_cffi: {e}"}

def tier2_cloudscraper(url: str) -> dict | None:
    """Tier 2: cloudscraper (solves JS computational challenges)."""
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        r = scraper.get(url, timeout=20)
        if r.status_code == 200:
            lower = r.text[:500].lower()
            if not any(m in lower for m in ["challenge", "captcha", "just a moment", "turnstile"]):
                return {"tier": "cloudscraper", "status": r.status_code, "content": r.text}
        return None
    except Exception as e:
        return {"error": f"cloudscraper: {e}"}

def tier3_cloakbrowser(url: str, wait: int = 15) -> dict | None:
    """Tier 3: CloakBrowser stealth browser with humanize."""
    try:
        from cloakbrowser import launch
        browser = launch(humanize=True)
        page = browser.new_page()
        page.goto(url, timeout=30000)
        time.sleep(wait)

        title = page.title()
        # Check if still stuck on challenge
        challenge_words = ["just a moment", "checking", "verify", "captcha"]
        if any(w in title.lower() for w in challenge_words):
            time.sleep(10)  # Wait more
            title = page.title()

        content = page.content()
        page.screenshot(path="/tmp/cloak_bypass_result.png")
        browser.close()

        still_blocked = any(w in title.lower() for w in challenge_words)
        if still_blocked:
            return {"error": f"cloakbrowser: still blocked, title='{title}'", "screenshot": "/tmp/cloak_bypass_result.png"}

        return {"tier": "cloakbrowser", "status": 200, "content": content, "title": title, "screenshot": "/tmp/cloak_bypass_result.png"}
    except Exception as e:
        return {"error": f"cloakbrowser: {e}"}

def check_blocked(text: str) -> list[str]:
    """Check if response contains anti-bot markers."""
    markers = ["challenge", "captcha", "just a moment", "checking your browser",
               "verify you are human", "turnstile", "cf-challenge", "ray id"]
    lower = text[:2000].lower()
    return [m for m in markers if m in lower]

def main():
    args = sys.argv[1:]
    if not args or args[0] in ["-h", "--help"]:
        print(__doc__)
        sys.exit(0)

    url = args[0]
    need_js = "--js" in args
    save_path = None
    if "--save" in args:
        idx = args.index("--save")
        if idx + 1 < len(args):
            save_path = args[idx + 1]

    print(f"🔒 Target: {url}")
    print(f"📋 Mode: {'JS rendering' if need_js else 'API/page fetch'}")
    print()

    # Tier 1
    print("⚡ Tier 1: curl_cffi...")
    r = tier1_curl_cffi(url)
    if r and r.get("tier"):
        print(f"✅ SUCCESS via {r['tier']} (status {r['status']})")
        print(f"   Content: {len(r['content'])} chars")
        if save_path:
            open(save_path, "w").write(r["content"])
            print(f"   Saved to: {save_path}")
        print(f"   Preview: {r['content'][:300]}")
        return
    elif r:
        print(f"   ❌ {r.get('error', 'blocked')}")
    else:
        print("   ❌ Blocked or failed")

    # Tier 2
    print("\n🛡️ Tier 2: cloudscraper...")
    r = tier2_cloudscraper(url)
    if r and r.get("tier"):
        print(f"✅ SUCCESS via {r['tier']} (status {r['status']})")
        print(f"   Content: {len(r['content'])} chars")
        if save_path:
            open(save_path, "w").write(r["content"])
            print(f"   Saved to: {save_path}")
        print(f"   Preview: {r['content'][:300]}")
        return
    elif r:
        print(f"   ❌ {r.get('error', 'blocked')}")
    else:
        print("   ❌ Blocked or failed")

    # Tier 3 (only if --js)
    if need_js:
        print("\n🥷 Tier 3: CloakBrowser (this may take 30+ seconds)...")
        r = tier3_cloakbrowser(url)
        if r and r.get("tier"):
            print(f"✅ SUCCESS via {r['tier']} (status {r['status']})")
            print(f"   Title: {r.get('title', 'N/A')}")
            print(f"   Content: {len(r['content'])} chars")
            if save_path:
                open(save_path, "w").write(r["content"])
                print(f"   Saved to: {save_path}")
            print(f"   Screenshot: {r.get('screenshot', 'N/A')}")
            return
        elif r:
            print(f"   ❌ {r.get('error', 'blocked')}")
            if r.get("screenshot"):
                print(f"   Screenshot saved: {r['screenshot']}")
        else:
            print("   ❌ Blocked or failed")

    print("\n❌ ALL TIERS FAILED — site has strong protection.")
    print("   Consider: 2Captcha API, residential proxy, or manual access.")

if __name__ == "__main__":
    main()
