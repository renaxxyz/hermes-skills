#!/usr/bin/env python3
"""
Idempotent registration of Merlin AI -> 9router.

Usage:
    python3 register_merlin_to_9router.py
    python3 register_merlin_to_9router.py --list         # show current state
    python3 register_merlin_to_9router.py --node-only    # create node + conn, skip aliases
    python3 register_merlin_to_9router.py --aliases-only # just refresh alias list

What it does (each step is idempotent — skips if already done):
    1. Logs into 9router (POST /api/auth/login with password=123456)
    2. Creates provider node 'merlin' -> baseUrl=http://127.0.0.1:20133/v1
    3. Creates provider connection 'merlin' -> isActive=true
    4. Adds 16 model aliases: claude-4.x, gpt-5.x, gemini-2/3.x, kimi, grok, deepseek, glm, minimax

Env vars (with defaults):
    ROUTER_URL    http://localhost:20128
    ROUTER_PASS   123456
    PROXY_URL     http://127.0.0.1:20133/v1
    PROXY_NAME    merlin
"""
import os
import sys
import json
import argparse
import urllib.request
import urllib.error

DEFAULT_ROUTER_URL = "http://localhost:20128"
DEFAULT_ROUTER_PASS = "123456"
DEFAULT_PROXY_URL = "http://127.0.0.1:20133/v1"
DEFAULT_PROXY_NAME = "merlin"

# Models to register (Pro plan). Add new ones here as Merlin expands the catalog.
MODELS = [
    "minimax-m2.5", "minimax-m2.7",
    "gemini-2.5-flash-lite", "gemini-3.0-flash", "gemini-3.1-flash-lite",
    "gemini-3.1-pro", "gemini-3.5-flash",
    "claude-4.5-haiku", "claude-4.6-sonnet", "claude-4.8-opus",
    "gpt-5.4", "gpt-5.5",
    "kimi-k2.6", "grok-4.3", "deepseek-v4-pro", "glm-5.1",
]


def login(router_url, password):
    req = urllib.request.Request(
        f"{router_url}/api/auth/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        for h in resp.headers.get_all("set-cookie") or []:
            if "auth_token" in h:
                return h.split(";")[0]
    raise RuntimeError("No auth_token cookie returned — wrong password?")


def call(method, path, body=None, auth="", router_url=DEFAULT_ROUTER_URL):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{router_url}/api{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "Cookie": auth},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def find_existing_node(auth, router_url, base_url):
    status, data = call("GET", "/provider-nodes", auth=auth, router_url=router_url)
    if status != 200 or not data:
        return None
    nodes = data.get("nodes", data) if isinstance(data, dict) else data
    if not isinstance(nodes, list):
        return None
    for n in nodes:
        if n.get("baseUrl") == base_url:
            return n
    return None


def find_existing_connection(auth, router_url, node_id, name):
    status, data = call("GET", "/providers", auth=auth, router_url=router_url)
    if status != 200 or not data:
        return None
    conns = data.get("connections", data) if isinstance(data, dict) else data
    if not isinstance(conns, list):
        return None
    for c in conns:
        if c.get("name") == name or c.get("provider") == node_id:
            return c
    return None


def find_existing_aliases(auth, router_url):
    status, data = call("GET", "/models/alias", auth=auth, router_url=router_url)
    if status != 200 or not data:
        return {}
    if isinstance(data, dict):
        return data.get("aliases", data)
    if isinstance(data, list):
        return {a.get("alias"): a.get("model") for a in data if a.get("alias")}
    return {}


def ensure_node(auth, router_url, base_url, name):
    existing = find_existing_node(auth, router_url, base_url)
    if existing:
        return existing.get("id", ""), "exists"
    status, data = call("POST", "/provider-nodes", {
        "apiType": "chat", "baseUrl": base_url, "prefix": name, "name": name,
    }, auth=auth, router_url=router_url)
    if status in (200, 201) and data:
        node_id = (data.get("node") or {}).get("id") or data.get("id", "")
        return node_id, "created"
    raise RuntimeError(f"Failed to create node: {status} {data}")


def ensure_connection(auth, router_url, node_id, name, base_url):
    existing = find_existing_connection(auth, router_url, node_id, name)
    if existing:
        conn_id = existing.get("id", "")
        if not existing.get("isActive"):
            call("PUT", f"/providers/{conn_id}", {"isActive": True}, auth=auth, router_url=router_url)
        return conn_id, "exists"
    status, data = call("POST", "/providers", {
        "name": name, "provider": node_id, "authType": "apikey",
        "isActive": True, "priority": 5, "defaultModel": MODELS[0],
        "providerSpecificData": {
            "nodeName": name, "prefix": name, "apiType": "chat",
            "baseUrl": base_url, "apiKey": "sk-merlin-proxy",
        },
        "apiKey": "sk-merlin-proxy",
    }, auth=auth, router_url=router_url)
    if status in (200, 201) and data:
        conn_id = (data.get("connection") or {}).get("id") or data.get("id", "")
        return conn_id, "created"
    raise RuntimeError(f"Failed to create connection: {status} {data}")


def ensure_aliases(auth, router_url, node_id):
    existing = find_existing_aliases(auth, router_url)
    result = {}
    for m in MODELS:
        fq = f"{node_id}/{m}"
        if existing.get(m) == fq:
            result[m] = "exists"
            continue
        status, data = call("PUT", "/models/alias", {"alias": m, "model": fq},
                            auth=auth, router_url=router_url)
        if status in (200, 201, 204):
            result[m] = "created"
        else:
            result[m] = f"failed:{status}"
    return result


def print_summary(node_id, node_status, conn_id, conn_status, alias_results):
    sep = "-" * 60
    print(sep)
    print("MERLIN -> 9ROUTER REGISTRATION SUMMARY")
    print(sep)
    print(f"Node      : {node_status.upper():<8} {node_id}")
    print(f"Conn      : {conn_status.upper():<8} {conn_id}")
    print(sep)
    counts = {"created": 0, "exists": 0}
    for v in alias_results.values():
        if v in counts:
            counts[v] += 1
    print(f"Aliases   : {counts['created']} created, {counts['exists']} already exist")
    print(sep)
    for m, v in alias_results.items():
        marker = "+" if v == "created" else ("v" if v == "exists" else "x")
        print(f"  {marker} {m:<28} {v}")
    print(sep)


def main():
    p = argparse.ArgumentParser(description="Idempotent Merlin -> 9router registration")
    p.add_argument("--list", action="store_true", help="List current state and exit")
    p.add_argument("--node-only", action="store_true", help="Skip alias registration")
    p.add_argument("--aliases-only", action="store_true", help="Skip node/conn creation")
    args = p.parse_args()

    router_url = os.environ.get("ROUTER_URL", DEFAULT_ROUTER_URL)
    router_pass = os.environ.get("ROUTER_PASS", DEFAULT_ROUTER_PASS)
    proxy_url = os.environ.get("PROXY_URL", DEFAULT_PROXY_URL)
    proxy_name = os.environ.get("PROXY_NAME", DEFAULT_PROXY_NAME)

    try:
        auth = login(router_url, router_pass)
    except Exception as e:
        print(f"LOGIN FAILED: {e}", file=sys.stderr)
        sys.exit(2)

    if args.list:
        node = find_existing_node(auth, router_url, proxy_url)
        aliases = find_existing_aliases(auth, router_url)
        merlin_aliases = {k: v for k, v in aliases.items() if proxy_name in str(v)}
        print(f"Node exists: {bool(node)}")
        if node:
            print(f"  id: {node.get('id')}")
            print(f"  baseUrl: {node.get('baseUrl')}")
        print(f"Aliases pointing to merlin: {len(merlin_aliases)}")
        for k, v in merlin_aliases.items():
            print(f"  {k} -> {v}")
        return

    if args.aliases_only:
        node = find_existing_node(auth, router_url, proxy_url)
        if not node:
            print("ERROR: no merlin node found. Run without --aliases-only first.", file=sys.stderr)
            sys.exit(3)
        results = ensure_aliases(auth, router_url, node["id"])
        print_summary(node["id"], "exists", "(n/a)", "exists", results)
        return

    node_id, node_status = ensure_node(auth, router_url, proxy_url, proxy_name)
    conn_id, conn_status = ensure_connection(auth, router_url, node_id, proxy_name, proxy_url)

    if args.node_only:
        print_summary(node_id, node_status, conn_id, conn_status, {})
        return

    results = ensure_aliases(auth, router_url, node_id)
    print_summary(node_id, node_status, conn_id, conn_status, results)


if __name__ == "__main__":
    main()
