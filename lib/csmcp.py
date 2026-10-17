"""Minimal Chainstack MCP client over HTTP (JSON-RPC + SSE). Key read from keys/chainstack_api, never printed."""
import json, os, sys, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = open(os.path.join(ROOT, "keys", "chainstack_api")).read().strip()
URL = "https://mcp.chainstack.com/mcp"
HDR = {"User-Agent": "dd-bot/1.0 (curl-compatible)", "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {KEY}"}

def _post(body, sid=None):
    h = dict(HDR)
    if sid:
        h["MCP-Session-Id"] = sid
    req = urllib.request.Request(URL, json.dumps(body).encode(), h)
    with urllib.request.urlopen(req, timeout=60) as r:
        sid = r.headers.get("mcp-session-id") or sid
        txt = r.read().decode()
    data = [l[5:].strip() for l in txt.splitlines() if l.startswith("data:")]
    return sid, (json.loads(data[-1]) if data else (json.loads(txt) if txt.strip() else None))

def session():
    sid, _ = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "dd-bot", "version": "1"}}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid

def call(sid, name, args=None):
    _, r = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}, sid)
    if r and "result" in r:
        out = []
        for c in r["result"].get("content", []):
            if c.get("type") == "text":
                try:
                    out.append(json.loads(c["text"]))
                except Exception:
                    out.append(c["text"])
        return out[0] if len(out) == 1 else out
    return r

if __name__ == "__main__":
    sid = session()
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(call(sid, sys.argv[1], args), indent=1)[:int(os.environ.get("MAXC", "6000"))])
