from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeNetBox:
    """Small paginated NetBox HTTP fake with latency and per-endpoint counters."""

    def __init__(self, latency: float = 0.03):
        self.latency = latency
        self.counts = Counter()
        self._next_id = 10_000
        self.object_changes = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _items(self, path, query):
                def ids_for(name):
                    values = query.get(name, [])
                    return [int(item) for value in values for item in value.split(",") if item]

                if path == "/api/status/":
                    return {"netbox-version": "4.6.8", "python-version": "3.12", "plugins": {}}
                if path == "/api/core/object-changes/":
                    changes = list(owner.object_changes)
                    # Match Django's single-value content-type filter: when a
                    # client repeats it, only the last value is effective.
                    if query.get("changed_object_type"):
                        wanted = query["changed_object_type"][-1]
                        changes = [item for item in changes if item["changed_object_type"] == wanted]
                    if query.get("time_after"):
                        threshold = datetime.fromisoformat(query["time_after"][-1].replace("Z", "+00:00"))
                        changes = [item for item in changes if datetime.fromisoformat(
                            item["time"].replace("Z", "+00:00")
                        ) > threshold]
                    reverse = query.get("ordering", ["-time"])[-1] != "time"
                    changes.sort(key=lambda item: item["time"], reverse=reverse)
                    limit = int(query.get("limit", [len(changes) or 1])[-1])
                    return changes[:limit]
                if path.endswith("/api-tokens/"):
                    return [{"id": 1, "key": "redacted", "expires": None}]
                if path.endswith("/manufacturers/"):
                    return [{"id": 1, "name": query.get("name", ["Vendor"])[0], "slug": "vendor"}]
                if path.endswith("/device-types/"):
                    requested = ids_for("id")
                    if not requested and "slug" not in query:
                        requested = list(range(1, 201))
                    requested = requested or [1]
                    return [{"id": item_id, "manufacturer": {"id": 1, "name": "Vendor"},
                             "model": "Switch", "slug": query.get("slug", [f"switch-{item_id - 1:04d}"])[0],
                             "part_number": None, "u_height": 1, "is_full_depth": True,
                             "subdevice_role": None, "weight": None, "weight_unit": None,
                             "comments": "", "custom_fields": {}, "front_image": None,
                             "rear_image": None, "last_updated": "2026-01-01T00:00:00Z"}
                            for item_id in requested]
                if path.endswith("/tenants/"):
                    return [{"id": index, "name": f"Tenant {index}", "slug": f"tenant-{index}"}
                            for index in range(1, 11)]
                if path.startswith("/api/"):
                    parent_ids = ids_for("device_type_id") or [1]
                    return [{"id": index, "name": f"item-{index}", "display": f"item-{index}",
                             "url": f"{owner.base_url}{path}{index}/", "address": f"192.0.2.{index}/24",
                             "prefix": f"192.0.{index}.0/24", "mac_address": f"02:00:00:00:00:{index:02x}",
                             "serial": f"SERIAL-{index}", "status": {"value": "active", "label": "Active"},
                             "site": "Site", "role": "Role",
                             "device": "Device", "virtual_machine": "VM", "assigned_object": None,
                             "device_type": {"id": index, "model": "Switch"},
                             "type": None, "label": "", "description": "", "mgmt_only": False,
                             "poe_mode": None, "poe_type": None, "maximum_draw": None,
                             "allocated_draw": None, "power_port": None, "feed_leg": None,
                             "positions": None, "rear_port": None, "rear_port_position": None,
                             "position": None}
                            for index in parent_ids]
                return []

            def do_GET(self):
                parsed = urlparse(self.path); owner._record("GET", parsed.path)
                items = self._items(parsed.path, parse_qs(parsed.query))
                if isinstance(items, dict):
                    self._reply(200, items)
                else:
                    self._reply(200, {"count": len(items), "next": None, "previous": None, "results": items})

            def _write(self):
                parsed = urlparse(self.path); owner._record(self.command, parsed.path)
                length = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(length) if length else b"{}"
                try: payload = json.loads(raw)
                except Exception: payload = {}
                owner._next_id += 1
                if isinstance(payload, list):
                    result = [{**item, "id": owner._next_id + index} for index, item in enumerate(payload)]
                else:
                    result = {**payload, "id": payload.get("id", owner._next_id)}
                self._reply(200 if self.command in {"PUT", "PATCH"} else 201, result)

            do_POST = _write
            do_PUT = _write
            do_PATCH = _write
            do_DELETE = _write

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _record(self, method, path):
        time.sleep(self.latency)
        self.counts[(method, path)] += 1

    @property
    def total_requests(self):
        return sum(self.counts.values())

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()
