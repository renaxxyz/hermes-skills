# 🛡️ Hermes Agent Skills

Kumpulan skills untuk [Hermes Agent](https://hermes-agent.nousresearch.com) — AI agent framework.

## 📂 Skills

### [captcha-cloudflare-bypass](./captcha-cloudflare-bypass/)
Bypass Cloudflare protection, CAPTCHA challenges, and anti-bot systems.

**Tiers:**
1. ⚡ `curl_cffi` — TLS fingerprint mimic (~1s)
2. 🛡️ `cloudscraper` — JS challenge solver (~5s)
3. 🥷 `CloakBrowser` — 49 C++ stealth patches (~15s)
4. 🌐 `Playwright` — Fallback browser (~20s)
5. 💰 `2Captcha` — Paid solving service

**Supported:**
- Cloudflare Turnstile, 403, Under Attack Mode
- reCAPTCHA v2/v3/Enterprise
- hCaptcha
- FunCaptcha / Arkose Labs
- Vercel Security Checkpoint
- AWS WAF CAPTCHA

## 🚀 Quick Start

```bash
# Copy skill to Hermes
cp -r captcha-cloudflare-bypass ~/.hermes/skills/software-development/

# Or use directly
python3 captcha-cloudflare-bypass/scripts/smart_bypass.py https://protected-site.com --js
```

## 📦 Dependencies

```bash
pip install curl_cffi cloudscraper cloakbrowser playwright
```

## 📝 License

MIT

---
Made with ❤️ by renaxxyz & Hermes Agent
