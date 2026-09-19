# Chromium in the Crabbox Hermes environment

Hermes and its terminal tools run inside this same task container. Use `/workspace`
for project files, downloads and screenshots. Internet is available. Python
Playwright and Chromium are already installed:

```sh
/opt/hermes/venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page()
    page.goto('https://example.com', wait_until='domcontentloaded', timeout=30000)
    page.screenshot(path='/workspace/page.png', full_page=True)
    browser.close()
PY
```

Use the URL relevant to the actual task. There is no graphical desktop/VNC or
personal browser profile. Keep supplied credentials private. Do not read or
copy `/agent-state` into the workspace. Browser binaries are in `/opt/playwright`;
`PLAYWRIGHT_BROWSERS_PATH` is already set. Install project Python dependencies
in `/workspace/.venv` and use the bundled Python for browser work.
