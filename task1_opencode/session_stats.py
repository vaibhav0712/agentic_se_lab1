import json
import sys

with open(sys.argv[1], "r") as f:
    data = json.load(f)

info = data["info"]
messages = data["messages"]

# Count user prompts
prompts = sum(1 for m in messages if m["info"]["role"] == "user")

# Sum assistant usage
assistant = [m["info"] for m in messages if m["info"]["role"] == "assistant"]

cost = sum(m.get("cost", 0) or 0 for m in assistant)

tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}

for m in assistant:
    t = m.get("tokens", {}) or {}
    tokens["input"] += t.get("input", 0) or 0
    tokens["output"] += t.get("output", 0) or 0
    tokens["reasoning"] += t.get("reasoning", 0) or 0

    cache = t.get("cache", {}) or {}
    tokens["cache_read"] += cache.get("read", 0) or 0
    tokens["cache_write"] += cache.get("write", 0) or 0

print("\n========== SESSION STATISTICS ==========")
print("Session:", info.get("title", "Unknown"))
print("Prompts:", prompts)
print("API calls:", len(assistant))
print(f"Cost: ${cost:.6f}")
print("Tokens:")

for key, value in tokens.items():
    print(f"  {key}: {value:,}")

print("========================================")
