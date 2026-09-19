# Chromium in the Hermes terminal sandbox

This image adds Playwright Python and Chromium to the existing Hermes image.
Use the terminal tool inside its separate tool container:

```sh
PLAYWRIGHT_BROWSERS_PATH=/opt/playwright /opt/hermes/venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
    print(page.title())
    page.screenshot(path="/workspace/page.png", full_page=True)
    browser.close()
PY
```

The executable and installed package versions are recorded in
`/opt/cloud-tools/browser-installation.json`. The browser cache is shared,
root-owned and readable/executable by tool UID 1000. Browser profiles and
downloads belong in the task's writable workspace or temporary directory;
do not reuse personal browser profiles or copy coordinator credentials.

Internet access is controlled by the tool container's runtime network policy.
Installing this image does not change that policy or activate the image.
Playwright's default Chromium launch disables Chromium's own sandbox; Docker
is still the tool isolation boundary. Enabling Chromium's internal sandbox
requires a separately configured compatible runtime policy. No privileged
container, host IPC or added Linux capabilities are required by this recipe.

Build with a local tag already bound to the exact base image:

```sh
docker build --pull=false \
  --build-arg BASE_IMAGE=cwb-hermes-browser-base:5a03c9d2d1fc \
  -f Dockerfile.hermes-browser \
  -t cloud-workbench-hermes-browser:20260918 .
```

The build installs packages and browser binaries only. It does not launch a
browser, run acceptance tests, or establish that a particular site works.
Installation follows [Playwright's browser instructions](https://playwright.dev/python/docs/browsers).
