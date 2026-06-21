# Merlin AI — Internal API Contract

> Reverse-engineered from Chrome extension source, Firebase auth response, and live API probing. **Subject to change** — Merlin can break this at any time by rotating the Firebase project, API path, or request schema.

## Authentication

### Endpoint
`POST https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}`

### API Key (extracted from Chrome extension — public)
```
AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM
```
Firebase project: `foyer-work`

This key only identifies the Firebase project; it does NOT grant access. Authentication still requires a valid email+password.

### Request body
```json
{
  "email": "your-merlin-email@example.com",
  "password": "your-merlin-password",
  "returnSecureToken": true
}
```

### Response (Pro account)
```json
{
  "kind": "identitytoolkit#VerifyPasswordResponse",
  "localId": "<firebase-uid>",
  "email": "your-merlin-email@example.com",
  "displayName": "<account-display-name>",
  "idToken": "eyJhbGciOiJSUzI1NiIs...<JWT, 1h expiry>...",
  "refreshToken": "AMf-vBz29hC73FlV...<long-lived>...",
  "expiresIn": "3600",
  "registered": true
}
```

### Response fields to check
- `role` in idToken JWT payload (`"paid"` = Pro, `"user"` = Guest)
- `planName` in idToken JWT payload (`"Merlin Pro"` for Pro, absent for Guest)
- `idToken` → use as `Authorization: Bearer {idToken}` for API calls
- Token expires in 1 hour, must refresh (proxy does this every 55 min)

## Chat API

### Endpoint
`POST https://www.getmerlin.in/arcane/api/v2/thread/unified`

### Headers
```
Content-Type: application/json
Accept: text/event-stream
Authorization: Bearer {firebase_idToken}
X-Merlin-Version: web-merlin
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36
Origin: https://www.getmerlin.in
Referer: https://www.getmerlin.in/chat
```

### Request body
```json
{
  "attachments": [],
  "chatId": "<uuid4>",
  "language": "AUTO",
  "message": {
    "childId": "<uuid4>",
    "content": "user's last message",
    "context": "user: msg1\nassistant: msg1\nuser: msg2",
    "id": "<uuid4>",
    "parentId": "root"
  },
  "mode": "UNIFIED_CHAT",
  "model": "claude-4.6-sonnet",
  "metadata": {
    "noTask": true,
    "isWebpageChat": false,
    "deepResearch": false,
    "webAccess": true,
    "proFinderMode": false,
    "mcpConfig": {"isEnabled": false},
    "merlinMagic": false
  }
}
```

### Response format (SSE)

Server streams `event: message` chunks:
```
event: message
data: {"status":"system","data":{"type":"reasoning","content":""}}

event: message
data: {"status":"in_progress","data":{"type":"text","text":"Hi","content":"Hi"}}

event: message
data: {"status":"in_progress","data":{"type":"text","text":" there","content":" there"}}

event: message
data: {"status":"finished","data":{"type":"text","text":"","content":""}}
```

**Filter rules** (proxy uses these):
- Skip events with `status: "system"` (internal setup, no useful text)
- Keep events with `data.type` of `"text"` (or absent)
- Concatenate `data.text` (or `data.content`) across all in_progress events
- Stop at `status: "finished"`

## Model catalog

### Source
`https://cdn.jsdelivr.net/gh/foyer-work/cdn-files@latest/merlin_constants.json`

### Structure
```json
{
  "textLLMs": [
    {
      "id": "claude-4.6-sonnet",
      "name": "Claude 4.6 Sonnet",
      "paid": true,
      "queryCost": 2,
      "archived": false
    }
  ]
}
```

### Free vs paid (filter logic)
- **Free (guest can use):** `!paid && queryCost <= 1` → 5 models
- **Paid only (Pro required):** all others → 11+ models
- **Archived:** filter out regardless of plan

### Pro model list (snapshot — verify against CDN for current state)
```
claude-4.5-haiku        (paid, fast)
claude-4.6-sonnet       (paid, recommended default)
claude-4.8-opus         (paid, slowest, highest quality)
gpt-5.4                 (paid)
gpt-5.5                 (paid, latest)
gemini-2.5-flash-lite   (free, lightweight)
gemini-3.0-flash        (free)
gemini-3.1-flash-lite   (free, smallest)
gemini-3.1-pro          (paid, large context)
gemini-3.5-flash        (paid)
kimi-k2.6               (paid, multilingual)
grok-4.3                (paid)
deepseek-v4-pro         (paid, code/math)
glm-5.1                 (paid, Chinese)
minimax-m2.5            (free, fast default)
minimax-m2.7            (free)
```

Total: 16 unique active models for Pro. The CDN is the source of truth — Merlin adds/retires models frequently.

## Error responses

### 401 Unauthorized
```json
{
  "status": "error",
  "data": {
    "message": "Request is missing the bearer token!",
    "type": "MISSING_BEARER_TOKEN"
  }
}
```
→ Token expired or missing. Refresh via Firebase signInWithPassword.

### 403 Forbidden
```json
{
  "status": "error",
  "data": {"message": "Insufficient permissions"}
}
```
→ Account is not Pro and trying to use a paid model. Switch to free model or upgrade.

### 429 Too Many Requests
→ Rate limited. Wait 30-60s, retry. Proxy doesn't have explicit backoff but is fast enough that retries usually succeed.

### 500/502/503
→ Merlin backend issue. Retry with backoff.

## Limits

- **Guest:** 30 messages/day, $1 daily budget
- **Pro:** effectively unlimited for chat (no documented daily cap), all models available
- **Rate limit:** ~5-10 requests/second per token (unconfirmed, observed in practice)
- **Token expiry:** 1 hour, refresh proactively

## How the proxy translates

OpenAI request:
```json
{
  "model": "claude-4.6-sonnet",
  "messages": [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hello"},
    {"role": "user", "content": "How are you?"}
  ],
  "stream": false
}
```

↓ converted to Merlin request ↓

```json
{
  "attachments": [],
  "chatId": "abc-123",
  "language": "AUTO",
  "message": {
    "childId": "def-456",
    "content": "How are you?",
    "context": "user: Hi\nassistant: Hello",
    "id": "ghi-789",
    "parentId": "root"
  },
  "mode": "UNIFIED_CHAT",
  "model": "claude-4.6-sonnet",
  "metadata": { ... }
}
```

↓ returned SSE is parsed and concatenated ↓

```json
{
  "id": "chatcmpl-xyz",
  "object": "chat.completion",
  "created": 1782032569,
  "model": "claude-4.6-sonnet",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "I'm doing well, thanks for asking!"
    },
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

(`usage` is always 0 because Merlin doesn't return token counts)

## Streaming mode

Proxy supports OpenAI streaming:
- First chunk: `delta: {role: "assistant"}`
- Middle chunks: `delta: {content: "<char>"}` (one char per chunk — yes, char-by-char)
- Last chunk: `finish_reason: "stop"`, `delta: {}`
- Final line: `data: [DONE]`

Char-by-char streaming is wasteful for OpenAI's per-chunk model but works correctly. Optimization: could batch by word/phrase if performance matters.

## File locations

- **Proxy script:** `<skill>/scripts/merlin_proxy.py`
- **Registration script:** `<skill>/scripts/register_merlin_to_9router.py`
- **Proxy log:** `/tmp/merlin_proxy.log` (when started with nohup)
- **Token cache:** in-memory only (lost on proxy restart)
- **Model catalog:** fetched fresh on cache miss (5min TTL)

## Future-proofing

If Merlin changes the API:
1. Check `https://www.getmerlin.in/` (likely 403/Cloudflare from many VPS IPs) — use a residential proxy to inspect
2. Or pull the latest Chrome extension from the Chrome Web Store, extract from `manifest.json` → background service worker JS
3. Or sniff via a stealth browser (CloakBrowser, Playwright with anti-detect patches)
4. Update `MERLIN_API_URL`, `build_merlin_request()`, or `parse_merlin_sse()` in `merlin_proxy.py` accordingly

## License

MIT — see parent repo LICENSE.
