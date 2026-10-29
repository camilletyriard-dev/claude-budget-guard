"""Test doubles that mimic the parts of the Anthropic SDK and Admin API we use."""
from __future__ import annotations

from types import SimpleNamespace as NS


class FakeMessages:
    def __init__(self, input_tokens=1000, usage=None, error=None, count_error=None):
        self.input_tokens, self.error, self.count_error = input_tokens, error, count_error
        self.usage = usage or dict(input_tokens=input_tokens, output_tokens=200)
        self.calls, self.count_calls = [], []

    def count_tokens(self, **kw):
        self.count_calls.append(kw)
        if self.count_error:
            raise self.count_error
        return NS(input_tokens=self.input_tokens)

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return NS(id=f"msg_{len(self.calls)}", usage=NS(**self.usage), content=[])

    batches = "batches-passthrough"


class FakeClient:
    def __init__(self, **kw):
        self.messages = FakeMessages(**kw)
        self.models = "models-passthrough"


def usage_row(key_id, model="claude-sonnet-5-5", uncached=0, output=0, cache_read=0, w5=0, w1=0,
              searches=0, tier="standard", geo="global", speed="standard"):
    return {"api_key_id": key_id, "model": model, "uncached_input_tokens": uncached, "output_tokens": output,
            "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
            "server_tool_use": {"web_search_requests": searches}, "service_tier": tier,
            "inference_geo": geo, "speed": speed}


class FakeAdmin:
    """Serves keys and usage (split over two pages to exercise pagination)."""

    def __init__(self, keys, rows):
        self.keys = [dict(k, status=k.get("status", "active")) for k in keys]
        self.rows, self.updates, self.requests = rows, [], []

    def request(self, method, path, params=None, body=None):
        self.requests.append((method, path, params, body))
        params = dict(params or [])
        if path == "/api_keys":
            if "after_id" not in params:
                return {"data": self.keys[:1], "has_more": len(self.keys) > 1, "last_id": "k1"}
            return {"data": self.keys[1:], "has_more": False, "last_id": None}
        if path == "/usage_report/messages":
            half = len(self.rows) // 2
            if "page" not in params:
                return {"data": [{"results": self.rows[:half]}], "has_more": True, "next_page": "p2"}
            return {"data": [{"results": self.rows[half:]}], "has_more": False, "next_page": None}
        if path.startswith("/api_keys/") and method == "POST":
            key_id = path.rsplit("/", 1)[1]
            self.updates.append((key_id, body["status"]))
            next(k for k in self.keys if k["id"] == key_id)["status"] = body["status"]
            return {"id": key_id, "status": body["status"]}
        raise AssertionError(f"unexpected request {method} {path}")
