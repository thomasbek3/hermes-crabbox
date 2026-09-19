# Chromium on the Crabbox desktop

Hermes and its terminal tools run inside the same task container. The desktop
profile supplies XFCE on `DISPLAY=:99`, viewed through Crabbox's VNC/noVNC
connection. Use `/workspace` for project files, downloads and screenshots.

For work the user needs to watch or interact with, launch Chromium with
`headless=False`. Headless work remains available when no visible window is
needed. Python Playwright, Chromium and the desktop packages are preinstalled:

```sh
/opt/hermes/venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=['--no-sandbox', '--disable-dev-shm-usage'],
    )
    page = browser.new_page()
    page.goto('https://example.com', wait_until='domcontentloaded', timeout=30000)
    page.screenshot(path='/workspace/page.png', full_page=True)
    browser.close()
PY
```

Use the task's actual URL. This example closes its window after the screenshot;
keep the browser process running while a user needs to interact with it. To
open a regular graphical window, run `chromium URL` from the task terminal.
The root-owned `chromium` wrapper invokes the existing Playwright Chromium at
`/opt/playwright/chromium-1243/chrome-linux64/chrome`; it does not install or
substitute Firefox. `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright` is already set.

The guest accepts the desktop environment only when the launch supplies
`CRABBOX_DESKTOP=1` and `DISPLAY=:99`. It keeps the job's private `HOME` and
Hermes state. Crabbox's XFCE VNC server uses the local X socket without an
Xauthority cookie; do not copy a personal `.Xauthority` or browser profile.
VNC authentication is managed by Crabbox and is separate from X display access.
Do not copy VNC passwords, model credentials or `/agent-state` into task outputs.

Install project dependencies in `/workspace/.venv` and use the bundled Python
for browser automation. Chromium's internal sandbox is disabled by this
container recipe; the task container remains the execution boundary. No host
Docker socket is supplied to this environment.

The Dockerfile installs the package list used by Crabbox v0.61.0's default
XFCE bootstrap. Upstream still runs its desktop package installation commands
at startup. Building this image does not start a desktop or browser and does
not establish that remote viewing or a particular website works.
