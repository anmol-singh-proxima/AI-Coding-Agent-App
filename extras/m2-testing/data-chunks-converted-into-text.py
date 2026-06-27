import json

content_parts = []
with open("/Users/proxima/Drive/AI-Coding-Agent-App/response_1782569048456.txt") as f:
    for line in f:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            delta = chunk["choices"][0]["delta"].get("content", "")
            if delta:
                content_parts.append(delta)
        except Exception:
            pass

print("".join(content_parts))
