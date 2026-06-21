---
name: merlin-ai
description: Integrate Merlin AI (getmerlin.in) as an OpenAI-compatible provider in 9router via a local proxy. Use when adding Merlin models, debugging Merlin token/plan issues, or when you need many free premium models (Claude, GPT, Gemini, etc.) backed by a Merlin Pro account. Triggers - "merlin", "getmerlin", "merlin proxy", "merlin 9router".
---

# Merlin AI → 9router Bridge

## What this is

A small Python proxy that translates OpenAI's `/v1/chat/completions` and `/v1/models` to Merlin AI's private API (`/arcane/api/v2/thread/unified`). The proxy uses Firebase email/password auth (NOT anonymous) to log into a Merlin Pro account and exposes the model catalog to 9router as a single openai-compatible provider.

**Why needed:** Merlin's website is behind Cloudflare Managed Challenge (browser blocked from many VPS IPs and headless environments). But the API endpoint itself is reachable, and a Firebase `accounts:signInWithPassword` call works to authenticate. The proxy wraps all this into something 9router can route to like any other OpenAI-compatible upstream.

## Architecture

```
Client (agent/curl/etc)
    ↓ Bearer sk-...
9router (port 20128)
    ↓ model: merlin/claude-4.6-sonnet
merlin_proxy.py (port 20133, 127.0.0.1)
    ↓ Bearer <firebase idToken> (auto-refresh 55m)
Merlin API (https://www.getmerlin.in/arcane/api/v2/thread/unified)
    ↓ SSE stream
Proxy parses SSE → OpenAI JSON → 9router → client
```

## Files (canonical paths)

- **Proxy:** `<skill>/scripts/merlin_proxy.py` (~350 lines, no external deps — stdlib only)
- **9router registration script:** `<skill>/scripts/register_merlin_to_9router.py` — idempotent
- **API contract reference:** `<skill>/references/merlin-api-contract.md`
- **Auth source:** `MERLIN_EMAIL` + `MERLIN_PASSWORD` env vars (set these before starting the proxy)
- **Firebase API key:** `AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM` (hardcoded in proxy, extracted from Merlin's public Chrome extension manifest — safe to embed)

## Prerequisites

1. **Merlin Pro account** — guest/free accounts have only 5 free models and 30 messages/day. You need Pro to access the full catalog.
2. **9router running on port 20128** with default password (or set `ROUTER_URL` + `ROUTER_PASS` env vars). See the `9router` skill for setup.
3. **Python 3.8+** — no pip dependencies (proxy uses only `urllib`, `http.server`, `json`, `threading`).

## How to deploy from scratch

### Step 1 — Verify the account is PRO (not guest)

Before touching the proxy, confirm the email/password actually has Pro plan. Guest accounts hit daily limits and the model catalog differs.

```bash
curl --noproxy "*" -X POST \
  "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM" \
  -H "Content-Type: application/json" \
  -d '{"email":"YOUR_MERLIN_EMAIL","password":"YOUR_MERLIN_PASSWORD","returnSecureToken":true}'
```

Look in the response: `role:"paid"` and `planName:"Merlin Pro"`. If `role:"user"` and no `planName` → GUEST, the model list will be smaller (5 free models, 30 msg/day).

### Step 2 — Start the proxy in background

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export MERLIN_EMAIL="YOUR_MERLIN_EMAIL"
export MERLIN_PASSWORD="YOUR_MERLIN_PASSWORD"
nohup python3 <skill>/scripts/merlin_proxy.py --port 20133 --host 0.0.0.0 \
  > /tmp/merlin_proxy.log 2>&1 &
```

Verify: `curl -s http://127.0.0.1:20133/health` → `{"status":"ok","service":"merlin-proxy"}`

### Step 3 — Test direct proxy

```bash
curl --noproxy "*" -X POST "http://127.0.0.1:20133/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"minimax-m2.5","messages":[{"role":"user","content":"hi"}],"max_tokens":30}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
```

Expected: ~2-3s response, real text content (not "request failed" or empty).

### Step 4 & 5 — Register in 9router + expose aliases (use the script)

The full idempotent registration is in `<skill>/scripts/register_merlin_to_9router.py`. It logs into 9router, creates the upstream node + connection, and adds all model aliases — skipping anything that already exists. Re-run it any time to recover from a 9router rebuild or to refresh the alias list.

```bash
# Full registration (idempotent — safe to re-run)
python3 <skill>/scripts/register_merlin_to_9router.py

# Just refresh the alias list (e.g. after editing MODELS in the script)
python3 <skill>/scripts/register_merlin_to_9router.py --aliases-only

# Show current state without changes
python3 <skill>/scripts/register_merlin_to_9router.py --list
```

Env vars the script honours:
- `ROUTER_URL` (default `http://localhost:20128`)
- `ROUTER_PASS` (default `123456`)
- `PROXY_URL` (default `http://127.0.0.1:20133/v1`)
- `PROXY_NAME` (default `merlin`)

The script encapsulates all 9router API quirks (auth cookie, PUT-only alias, `isActive: true`, node UUID as `provider`, model value as `{node_uuid}/{model}`). To add a new model, edit the `MODELS = [...]` list in the script and re-run with `--aliases-only`.

For reference, the manual 3-step flow is documented in the `9router` skill's `references/openai-compatible-providers.md`.

### Step 6 — Live test through 9router

```bash
# Replace sk-your-9router-key with your actual 9router bearer key
# (visible in 9router dashboard, or generate one via /api/auth/login)
curl --noproxy "*" -X POST "http://127.0.0.1:20128/v1/chat/completions" \
  -H "Authorization: Bearer sk-your-9router-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-4.6-sonnet","messages":[{"role":"user","content":"ping"}],"max_tokens":20}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
```

Expected: ~2s response with actual text.

## Critical pitfalls (all hit during real use)

1. **Anonymous auth gives GUEST plan, not PRO.** Use `accounts:signInWithPassword` with email+password, not `accounts:signUp`. Response field `role:"paid"` and `planName:"Merlin Pro"` confirms PRO. Guest has 5 free models, 30 msg/day, $1 daily budget. PRO has 16+ models, effectively unlimited for chat.

2. **`isActive: false` by default** on new 9router connection. MUST set `isActive: true` in POST body. Otherwise all chat requests return 404 "No active credentials".

3. **`provider` field in POST /api/providers must be the node UUID**, not the string `"openai-compatible-chat"`. The node UUID is the response of POST `/api/provider-nodes` (`node.id`).

4. **PUT /api/models/alias requires auth cookie** from `/api/auth/login`. Without auth → `{"error":"Unauthorized"}` (silent failure if you don't check response).

5. **The `model` value in alias must be `{node_uuid}/{model_name}`** (e.g. `openai-compatible-chat-<your-node-uuid>/claude-4.6-sonnet`), NOT just `claude-4.6-sonnet`. The format is "fully qualified upstream path". The node UUID is generated when you create the upstream node (step 4) — the registration script handles this automatically.

6. **Stale `http_proxy` / `https_proxy` env breaks curl** on some VPS environments. If you see `Proxy Authentication Required` (HTTP 407) or exit 56, run `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY` first, or pass `--noproxy "*"` to curl. Forgetting this makes 9router + proxy look "broken" when it's just local proxy auth failing.

7. **Firebase idToken expires in 1 hour.** Proxy auto-refreshes every 55 minutes. First request after restart will be slow (login round-trip ~500ms). Subsequent requests cache the token.

8. **Merlin API returns SSE not JSON.** The proxy parses `event: message` lines, filters out `status:"system"` events, and concatenates `data.text` chunks. Without this parser, raw SSE looks like garbage to OpenAI clients.

9. **Merlin website (getmerlin.in) is Cloudflare-blocked** for many VPS IPs. But the API subdomain is not. Don't waste time trying to log in via the website — use Firebase REST API directly.

10. **`/v1/models` returns 14+ models for PRO** but the proxy filters out `archived:true` models. Check `https://cdn.jsdelivr.net/gh/foyer-work/cdn-files@latest/merlin_constants.json` for the full raw list if a model is missing.

11. **Don't trust "active" status from a freshly-added node** — `testStatus: "pending"` until first real request completes. Always do a live test (step 6) before claiming success.

12. **Token format in /v1/chat/completions is just `Authorization: Bearer` from 9router** — the proxy doesn't care what's in the header because it uses its own Firebase token to talk to Merlin. The 9router bearer key is a dummy for 9router's own auth gate.

## Verification commands

```bash
# Proxy alive
pgrep -fa merlin_proxy
ss -tlnp | grep 20133

# Proxy health
curl -s http://127.0.0.1:20133/health

# Direct proxy chat (fastest test)
curl -s -X POST http://127.0.0.1:20133/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"minimax-m2.5","messages":[{"role":"user","content":"ping"}]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'])"

# 9router models count
curl -s -H "Authorization: Bearer sk-your-9router-key" \
  http://127.0.0.1:20128/v1/models | python3 -c "import json,sys; print(len(json.load(sys.stdin)['data']))"

# 9router live chat (end-to-end)
curl -s -X POST http://127.0.0.1:20128/v1/chat/completions \
  -H "Authorization: Bearer sk-your-9router-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-4.6-sonnet","messages":[{"role":"user","content":"hi"}],"max_tokens":20}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
```

## When to use which model

| Need | Model | Why |
|------|-------|-----|
| Cheapest quick task | `gemini-2.5-flash-lite` | Lightweight, fast, no rate limit issues |
| Coding / reasoning | `claude-4.6-sonnet` | Strong code + reasoning, balanced speed |
| Hardest reasoning | `claude-4.8-opus` | Best quality, slower |
| Long context | `gemini-3.1-pro` | Large context window |
| Quick chat | `minimax-m2.5` or `minimax-m2.7` | Fast, reliable defaults |
| Alternative to claude | `gpt-5.5` | OpenAI fallback |
| Math / code | `deepseek-v4-pro` | Specialized |
| Multilingual | `kimi-k2.6` | Chinese/English strong |

See `references/merlin-api-contract.md` for the full model catalog and Pro plan limits.

## Restarting the proxy after reboot

Add to crontab `@reboot`, or include in your service manager:

```bash
@reboot cd ~ && unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \
  MERLIN_EMAIL="YOUR_MERLIN_EMAIL" MERLIN_PASSWORD="YOUR_MERLIN_PASSWORD" \
  nohup python3 <skill>/scripts/merlin_proxy.py --port 20133 --host 0.0.0.0 \
  > /tmp/merlin_proxy.log 2>&1 &
```

Replace `<skill>` with the actual path to the cloned skill directory.

## Security & privacy notes

- **Your Merlin email + password are stored in env vars on your VPS** — treat them like any other credential. Don't commit them to git. Use a secrets manager (or at minimum `chmod 600` on the script that exports them).
- **The proxy has no auth** by default — anyone who can reach `127.0.0.1:20133` can use your Pro account. Keep it bound to localhost (`--host 127.0.0.1`) unless you have a firewall in place. 9router talks to it via localhost anyway, so binding to `0.0.0.0` is only needed if you want external access.
- **The Firebase API key is public** — it ships in Merlin's Chrome extension manifest. It only identifies the project (`foyer-work`); it does NOT grant access. Auth still requires valid email+password.
- **9router bearer keys are session-only** by default. Treat them as low-value but rotate if exposed.

## Related skills

- `devops/9router` — for adding other openai-compatible providers, the alias API quirks
- `devops/agent-doctrine` — when running this in breach v5 mode
- `devops/cron-automation` — for scheduled tasks that call Merlin via 9router

## License

MIT — see parent repo LICENSE.
