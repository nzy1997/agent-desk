# Dashboard Service Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dashboard Restart button that restarts the `agent-desk serve` process without touching detached issue worker supervisors.

**Architecture:** The dashboard exposes a server-level `/api/actions/restart` POST action. The handler responds with JSON, then invokes an injected restart callback that stops the scheduler, shuts down the HTTP server, and re-execs the current Python process with the original arguments. The UI uses the existing `action()` helper to call the route.

**Tech Stack:** Python 3.11 standard library, `http.server`, `unittest`, existing HTML/vanilla JS dashboard.

## Global Constraints

- Zero runtime dependencies: only the Python standard library plus `gh`, `git`, and `codex`.
- Tests use stdlib `unittest`, not pytest.
- Do not add Python dependencies; `pyproject.toml` keeps `dependencies = []`.
- Restart must not restart active issue workers, kill detached supervisors, or retry failed work automatically.
- The restart action must return a response before shutdown/re-exec begins.

---

### Task 1: Restart Route And Callback

**Files:**
- Modify: `agent_desk/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `serve_dashboard(..., on_serving: Callable[[str, int], None] | None = None)`
- Produces: `serve_dashboard(..., restart_callback: Callable[[], None] | None = None) -> None`
- Produces: `make_handler(..., restart_callback: Callable[[], None] | None = None) -> type[BaseHTTPRequestHandler]`
- Produces: `restart_process(scheduler: Scheduler | None, server: ThreadingHTTPServer) -> None`

- [ ] **Step 1: Write the failing endpoint test**

Add this test method to `DashboardTests` in `tests/test_dashboard.py`:

```python
    def test_restart_route_returns_ok_and_invokes_restart_callback(self):
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            restarted = threading.Event()
            bound: dict[str, int] = {}
            ready = threading.Event()

            thread = threading.Thread(
                target=serve_dashboard,
                kwargs={
                    "host": host,
                    "port": 0,
                    "store": store,
                    "on_serving": lambda _h, port: (bound.update(port=port), ready.set()),
                    "restart_callback": restarted.set,
                },
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")

            request = urllib.request.Request(
                f"http://{host}:{bound['port']}/api/actions/restart",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "restart")
            self.assertTrue(restarted.wait(timeout=5), "restart callback was not invoked")
```

- [ ] **Step 2: Run the failing endpoint test**

Run: `python3 -m unittest tests.test_dashboard.DashboardTests.test_restart_route_returns_ok_and_invokes_restart_callback -v`

Expected: FAIL with `TypeError: serve_dashboard() got an unexpected keyword argument 'restart_callback'`.

- [ ] **Step 3: Implement the restart route plumbing**

In `agent_desk/dashboard.py`, add imports:

```python
import os
import sys
import time
```

Change the handler factory signature:

```python
def make_handler(
    store: Store,
    scheduler: Scheduler | None = None,
    config_path: Path | None = None,
    restart_callback: Callable[[], None] | None = None,
):
```

Add this branch in `Handler.do_POST`, next to the pause/resume action branches:

```python
            if path == "/api/actions/restart":
                self._send_json({"ok": True, "action": "restart"})
                if restart_callback is not None:
                    threading.Thread(target=restart_callback, daemon=True).start()
                return
```

Add this helper near `serve_dashboard`:

```python
def restart_process(scheduler: Scheduler | None, server: ThreadingHTTPServer) -> None:
    if scheduler is not None:
        scheduler.stop()
    server.shutdown()
    time.sleep(0.05)
    try:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except Exception as exc:
        print(f"agent-desk: restart failed: {exc}", file=sys.stderr, flush=True)
        os._exit(1)
```

Extend `serve_dashboard`:

```python
def serve_dashboard(
    host: str,
    port: int,
    store: Store,
    scheduler: Scheduler | None = None,
    config_path: Path | None = None,
    port_attempts: int = 20,
    on_serving: Callable[[str, int], None] | None = None,
    restart_callback: Callable[[], None] | None = None,
) -> None:
```

Create the handler after the server is bound so the callback can close over the actual server:

```python
    handler = None
    server = None
```

After creating the `ThreadingHTTPServer`, set the request handler class:

```python
            server = ThreadingHTTPServer((host, candidate), BaseHTTPRequestHandler)
            break
```

After the binding loop succeeds:

```python
    actual_restart_callback = restart_callback or (lambda: restart_process(scheduler, server))
    handler = make_handler(store, scheduler, config_path, actual_restart_callback)
    server.RequestHandlerClass = handler
```

- [ ] **Step 4: Run the endpoint test**

Run: `python3 -m unittest tests.test_dashboard.DashboardTests.test_restart_route_returns_ok_and_invokes_restart_callback -v`

Expected: PASS.

- [ ] **Step 5: Run dashboard tests**

Run: `python3 -m unittest tests.test_dashboard -v`

Expected: PASS.

### Task 2: Restart Button

**Files:**
- Modify: `agent_desk/static/dashboard.html`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `HTML` loaded by `agent_desk.dashboard._load_page`
- Produces: Header button markup containing `/api/actions/restart`

- [ ] **Step 1: Write the failing HTML test**

Add this test method to `DashboardTests` in `tests/test_dashboard.py`:

```python
    def test_dashboard_html_includes_restart_button(self):
        self.assertIn("Restart", HTML)
        self.assertIn("/api/actions/restart", HTML)
```

- [ ] **Step 2: Run the failing HTML test**

Run: `python3 -m unittest tests.test_dashboard.DashboardTests.test_dashboard_html_includes_restart_button -v`

Expected: FAIL because the current static dashboard HTML does not include `Restart`.

- [ ] **Step 3: Add the button**

In `agent_desk/static/dashboard.html`, update the header button group:

```html
      <button onclick="action('/api/actions/pause')">Pause</button>
      <button onclick="action('/api/actions/resume')">Resume</button>
      <button onclick="action('/api/actions/restart')">Restart</button>
      <button class="primary" onclick="action('/api/actions/run-next')">Run next</button>
```

- [ ] **Step 4: Run the HTML test**

Run: `python3 -m unittest tests.test_dashboard.DashboardTests.test_dashboard_html_includes_restart_button -v`

Expected: PASS.

- [ ] **Step 5: Run full tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_desk/dashboard.py agent_desk/static/dashboard.html tests/test_dashboard.py docs/superpowers/plans/2026-06-30-dashboard-service-restart.md
git commit -m "Add dashboard service restart action"
```
