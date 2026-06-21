#!/usr/bin/env python3
"""
Merlin AI (getmerlin.in) OpenAI-Compatible Proxy.

Uses MERLIN_EMAIL + MERLIN_PASSWORD env vars for Firebase auth (Pro account).

Usage:
  export MERLIN_EMAIL="your-merlin-email@example.com"
  export MERLIN_PASSWORD="your-merlin-password"
  python3 merlin_proxy.py [--port 20133] [--host 0.0.0.0]

Requires a Merlin Pro account. Guest/free accounts will get throttled and have
a much smaller model catalog. See ../SKILL.md for setup details.
"""

import json
import os
import sys
import uuid
import time
import http.server
import urllib.request
import urllib.error
import threading
import argparse

MERLIN_API_URL = "https://www.getmerlin.in/arcane/api/v2/thread/unified"
FIREBASE_LOGIN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
MODELS_CDN_URL = "https://cdn.jsdelivr.net/gh/foyer-work/cdn-files@latest/merlin_constants.json"

GOOGLE_API_KEY = "AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM"

# Token cache (memory + disk for cross-restart persistence)
_token_cache = {"token": None, "expires_at": 0, "lock": threading.Lock()}
TOKEN_LIFETIME = 55 * 60  # Firebase tokens valid 1h
TOKEN_DISK_PATH = os.environ.get("MERLIN_TOKEN_CACHE", "/tmp/merlin_token.json")

# Rate-limit backoff: if Firebase returns TOO_MANY_ATTEMPTS_TRY_LATER, hold off
# for RATE_LIMIT_COOLDOWN seconds before retrying. Persisted to disk so a
# restart can't bypass the cooldown and re-trigger the rate limit.
RATE_LIMIT_COOLDOWN = int(os.environ.get("MERLIN_RATE_LIMIT_COOLDOWN", "300"))  # 5 min default
_rate_limit = {"locked_until": 0, "lock": threading.Lock()}
RATE_LIMIT_DISK_PATH = TOKEN_DISK_PATH + ".ratelimit"
MODEL_CACHE = {"models": None, "expires_at": 0, "lock": threading.Lock()}
MODEL_CACHE_TTL = 300


def _load_cached_token():
    """Load persisted token from disk if still valid."""
    try:
        with open(TOKEN_DISK_PATH, "r") as f:
            data = json.load(f)
        if data.get("token") and data.get("expires_at", 0) > time.time():
            return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def _save_cached_token(token, expires_at):
    """Persist token to disk for next restart."""
    try:
        with open(TOKEN_DISK_PATH, "w") as f:
            json.dump({"token": token, "expires_at": expires_at}, f)
        os.chmod(TOKEN_DISK_PATH, 0o600)
    except OSError as e:
        print(f"[merlin_proxy] Warning: could not persist token to {TOKEN_DISK_PATH}: {e}", file=sys.stderr)


def _load_rate_limit_state():
    """Load rate-limit cooldown from disk (survives restarts)."""
    try:
        with open(RATE_LIMIT_DISK_PATH, "r") as f:
            data = json.load(f)
        return float(data.get("locked_until", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return 0.0


def _save_rate_limit_state(locked_until):
    """Persist rate-limit cooldown to disk."""
    try:
        with open(RATE_LIMIT_DISK_PATH, "w") as f:
            json.dump({"locked_until": locked_until}, f)
        os.chmod(RATE_LIMIT_DISK_PATH, 0o600)
    except OSError as e:
        print(f"[merlin_proxy] Warning: could not persist rate-limit state: {e}", file=sys.stderr)


class RateLimitedError(Exception):
    """Raised when Firebase is rate-limiting us; caller should back off."""

    def __init__(self, retry_after):
        super().__init__(f"Firebase rate limit active; retry in {int(retry_after)}s")
        self.retry_after = retry_after


def _check_rate_limit():
    """If we are inside a Firebase rate-limit cooldown, raise RateLimitedError."""
    with _rate_limit["lock"]:
        now = time.time()
        locked_until = max(_rate_limit["locked_until"], _load_rate_limit_state())
        _rate_limit["locked_until"] = locked_until
        if now < locked_until:
            raise RateLimitedError(locked_until - now)


def _mark_rate_limited():
    """Record that Firebase rate-limited us. Cooldown starts NOW + RATE_LIMIT_COOLDOWN."""
    with _rate_limit["lock"]:
        locked_until = time.time() + RATE_LIMIT_COOLDOWN
        _rate_limit["locked_until"] = locked_until
        _save_rate_limit_state(locked_until)
        print(
            f"[merlin_proxy] Firebase rate-limited; cooldown for {RATE_LIMIT_COOLDOWN}s "
            f"until {time.strftime('%H:%M:%S', time.localtime(locked_until))}",
            file=sys.stderr,
        )


def _clear_rate_limit():
    """Clear rate-limit cooldown (called on successful login)."""
    with _rate_limit["lock"]:
        _rate_limit["locked_until"] = 0
        try:
            os.remove(RATE_LIMIT_DISK_PATH)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[merlin_proxy] Warning: could not clear rate-limit file: {e}", file=sys.stderr)


def fetch_models():
    """Fetch ALL available models from CDN (PRO user sees all)"""
    try:
        req = urllib.request.Request(MODELS_CDN_URL)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            all_models = [m for m in data.get("textLLMs", []) if not m.get("archived")]
            return all_models
    except Exception as e:
        print(f"[merlin_proxy] Error fetching models: {e}", file=sys.stderr)
        return []


def get_firebase_token():
    """Get Firebase auth token using email/password (Pro account)"""
    email = os.environ.get("MERLIN_EMAIL", "")
    password = os.environ.get("MERLIN_PASSWORD", "")

    if not email or not password:
        raise ValueError("MERLIN_EMAIL and MERLIN_PASSWORD environment variables required")

    with _token_cache["lock"]:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]

    # Check rate limit (acquires its own lock, so call outside the token lock)
    _check_rate_limit()

    with _token_cache["lock"]:
        # Re-check token cache (another thread may have refreshed it)
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]

        # Try disk cache first (survives restarts, avoids Firebase rate limits)
        disk = _load_cached_token()
        if disk:
            _token_cache["token"] = disk["token"]
            _token_cache["expires_at"] = disk["expires_at"]
            return _token_cache["token"]

        url = f"{FIREBASE_LOGIN_URL}?key={GOOGLE_API_KEY}"
        body = json.dumps({
            "email": email,
            "password": password,
            "returnSecureToken": True,
        }).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                token = data.get("idToken", "")
                if not token:
                    raise ValueError("No idToken in Firebase response")
                plan = data.get("planName", "unknown")
                print(f"[merlin_proxy] Logged in as {email} (plan: {plan})", file=sys.stderr)
                _token_cache["token"] = token
                _token_cache["expires_at"] = now + TOKEN_LIFETIME
                _save_cached_token(token, _token_cache["expires_at"])
                _clear_rate_limit()
                return token
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(f"[merlin_proxy] Firebase login error: {e.code} {err_body[:300]}", file=sys.stderr)
            if e.code == 400 and "TOO_MANY_ATTEMPTS_TRY_LATER" in err_body:
                _mark_rate_limited()
                raise RateLimitedError(RATE_LIMIT_COOLDOWN) from e
            raise


def build_merlin_request(openai_req):
    """Convert OpenAI chat request to Merlin format"""
    messages = openai_req.get("messages", [])
    model = openai_req.get("model", "minimax-m2.5")

    context_lines = []
    for msg in messages[:-1]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                [c.get("text", "") if isinstance(c, dict) else str(c) for c in content]
            )
        context_lines.append(f"{role}: {content}")

    last_msg = messages[-1] if messages else {"role": "user", "content": ""}
    last_content = last_msg.get("content", "")
    if isinstance(last_content, list):
        last_content = " ".join(
            [c.get("text", "") if isinstance(c, dict) else str(c) for c in last_content]
        )

    return {
        "attachments": [],
        "chatId": str(uuid.uuid4()),
        "language": "AUTO",
        "message": {
            "childId": str(uuid.uuid4()),
            "content": last_content,
            "context": "\n".join(context_lines),
            "id": str(uuid.uuid4()),
            "parentId": "root",
        },
        "mode": "UNIFIED_CHAT",
        "model": model,
        "metadata": {
            "noTask": True,
            "isWebpageChat": False,
            "deepResearch": False,
            "webAccess": True,
            "proFinderMode": False,
            "mcpConfig": {"isEnabled": False},
            "merlinMagic": False,
        },
    }


def parse_merlin_sse(data):
    """Parse Merlin SSE response and return list of text chunks"""
    chunks = []
    parts = data.split("\n\n")
    for part in parts:
        lines = part.split("\n")
        event_type = ""
        data_lines = []

        for line in lines:
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:].strip())

        if not event_type or not data_lines:
            continue

        if event_type == "message":
            data_str = "\n".join(data_lines)
            try:
                msg = json.loads(data_str)
                msg_data = msg.get("data", {})
                text = msg_data.get("text") or msg_data.get("content") or ""
                status = msg.get("status", "")
                if status == "system":
                    continue
                if text and text != " ":
                    text_type = msg_data.get("type", "")
                    if not text_type or text_type == "text":
                        chunks.append(text)
            except json.JSONDecodeError:
                pass

    return chunks


class MerlinProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP server that translates OpenAI -> Merlin API"""

    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_stream(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write("data: [DONE]\n\n".encode())
        self.wfile.flush()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-api-key")
        self.end_headers()

    def do_GET(self):
        path = self.path
        if path == "/v1/models" or path.endswith("/v1/models"):
            self._handle_models()
        elif path in ("/", "/health"):
            self._send_json(200, {"status": "ok", "service": "merlin-proxy"})
        else:
            self._send_json(404, {"error": f"Not found: {self.path}"})

    def do_POST(self):
        path = self.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"

        if path.endswith("/v1/chat/completions") or path.endswith("/chat/completions"):
            self._handle_chat(body)
        else:
            self._send_json(404, {"error": f"Not found: {self.path}"})

    def _handle_models(self):
        with MODEL_CACHE["lock"]:
            now = time.time()
            if not MODEL_CACHE["models"] or now > MODEL_CACHE["expires_at"]:
                MODEL_CACHE["models"] = fetch_models()
                MODEL_CACHE["expires_at"] = now + MODEL_CACHE_TTL

        models = MODEL_CACHE["models"]
        openai_models = []
        for m in models:
            openai_models.append({
                "id": m.get("id"),
                "object": "model",
                "created": int(time.time()),
                "owned_by": "merlin",
            })

        self._send_json(200, {"object": "list", "data": openai_models})

    def _handle_chat(self, body=None):
        if body is None:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
        openai_req = json.loads(body)

        stream = openai_req.get("stream", False)
        model = openai_req.get("model", "minimax-m2.5")

        try:
            token = get_firebase_token()
            merlin_req = build_merlin_request(openai_req)
            print(f"[merlin_proxy] Chat: model={merlin_req['model']}, stream={stream}", file=sys.stderr)

            merlin_body = json.dumps(merlin_req).encode()
            req = urllib.request.Request(
                MERLIN_API_URL,
                data=merlin_body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {token}",
                    "X-Merlin-Version": "web-merlin",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                    "Origin": "https://www.getmerlin.in",
                    "Referer": "https://www.getmerlin.in/chat",
                },
            )

            with urllib.request.urlopen(req, timeout=120) as resp:
                raw_data = resp.read().decode("utf-8", errors="replace")
                text_chunks = parse_merlin_sse(raw_data)
                full_text = "".join(text_chunks)

                created = int(time.time())
                resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

                if stream:
                    first_chunk = {
                        "id": resp_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    }
                    content_chunks = [first_chunk]
                    if full_text:
                        for i, char in enumerate(full_text):
                            content_chunks.append({
                                "id": resp_id, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [{"index": 0, "delta": {"content": char}, "finish_reason": None}],
                            })
                    content_chunks.append({
                        "id": resp_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    })
                    self._send_stream(content_chunks)
                else:
                    openai_resp = {
                        "id": resp_id, "object": "chat.completion",
                        "created": created, "model": model,
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": full_text},
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
                    self._send_json(200, openai_resp)

        except RateLimitedError as e:
            retry_after = int(e.retry_after) + 1
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "Firebase rate-limited; login is in cooldown",
                "detail": str(e),
                "retry_after_seconds": retry_after,
            }).encode())
            print(f"[merlin_proxy] 503: rate limit cooldown ({retry_after}s remaining)", file=sys.stderr)
        except ValueError as e:
            self._send_json(500, {"error": str(e)})
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(f"[merlin_proxy] Merlin API error: {e.code} {err_body[:300]}", file=sys.stderr)
            self._send_json(e.code, {"error": f"Merlin API: {e.code}", "detail": err_body[:500]})
        except Exception as e:
            print(f"[merlin_proxy] Error: {e}", file=sys.stderr)
            self._send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        sys.stderr.write(f"[merlin_proxy] {args[0]} {args[1]} {args[2]}\n")


def main():
    parser = argparse.ArgumentParser(description="Merlin AI OpenAI-Compatible Proxy")
    parser.add_argument("--port", type=int, default=20133, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    if not os.environ.get("MERLIN_EMAIL") or not os.environ.get("MERLIN_PASSWORD"):
        print(
            "[merlin_proxy] ERROR: MERLIN_EMAIL and MERLIN_PASSWORD env vars are required.\n"
            "  Set them before starting the proxy, e.g.:\n"
            "    export MERLIN_EMAIL='your-merlin-email@example.com'\n"
            "    export MERLIN_PASSWORD='your-merlin-password'\n"
            "  Then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    server = http.server.HTTPServer((args.host, args.port), MerlinProxyHandler)
    print(f"[merlin_proxy] Listening on http://{args.host}:{args.port}", file=sys.stderr)
    print(f"[merlin_proxy] OpenAI API at http://{args.host}:{args.port}/v1", file=sys.stderr)
    print(f"[merlin_proxy] Pro account: {os.environ.get('MERLIN_EMAIL')}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[merlin_proxy] Shutting down...", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
