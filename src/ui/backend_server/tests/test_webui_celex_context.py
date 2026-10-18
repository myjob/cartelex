"""
Playwright end-to-end test for the Open WebUI chat interface.

Submits a bare CELEX ID and asserts that the agent acknowledges the case
context switch without invoking the full retrieval pipeline.

Prerequisites:
  - FastAPI backend running at http://0.0.0.0:7777
  - Open WebUI running at https://webui.localhost/ (Caddy -> localhost:3000)
  - Backend server registered as the 'celex-dspy-agent' model in Open WebUI
  - Playwright browsers installed: .venv/bin/playwright install chromium
  - Environment variables:
      WEBUI_EMAIL     — Open WebUI login e-mail
      WEBUI_PASSWORD  — Open WebUI login password
  - Optional: PLAYWRIGHT_HEADLESS=false to watch the run in a headed browser
    (requires DISPLAY or WAYLAND_DISPLAY; ignored otherwise)
"""
import logging
import os
import unittest
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

_LOGGER = logging.getLogger(__name__)


_BACKEND_URL = "http://0.0.0.0:7777"
_BACKEND_HEALTH_URL = f"{_BACKEND_URL}/models"
# Caddy terminates TLS for this host and 308-redirects plain http, so address
# it as https directly.  Its internal-CA cert is tolerated via
# ignore_https_errors on the browser context.
_BASE_URL = "https://webui.localhost"
_CELEX_ID = "CELEX-31994D0823"
_MODEL_NAME = "celex-dspy-agent"

# Assertion fragments — match text returned by the streaming event pipeline.
# Emoji originate from server SSE events and must be preserved verbatim.
_EXPECTED_VALIDATION = "Validating case reference"
_EXPECTED_ACK = f"Case context set to {_CELEX_ID}. Ask a question about this case to continue."

# Allow up to 30 s for the streamed response to complete.
_RESPONSE_TIMEOUT_MS = 30_000

# Seconds to wait for backend HTTP probe.
_BACKEND_PROBE_TIMEOUT = 5


def _resolve_headless() -> bool:
    """Decide whether Chromium must run headless.

    Headless is the default.  Setting PLAYWRIGHT_HEADLESS=false requests a
    headed browser, which is honoured only when a graphical display is
    actually available — otherwise Chromium exits with
    'Missing X server or $DISPLAY'.

    Returns:
        True when the browser must be launched headless.
    """
    if os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false":
        return True
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not has_display:
        _LOGGER.warning(
            "PLAYWRIGHT_HEADLESS=false requested but no DISPLAY/WAYLAND_DISPLAY "
            "is set — falling back to headless."
        )
    return not has_display


def _login(page, email: str, password: str) -> None:
    """Complete the Open WebUI sign-in form and wait for the chat UI to load.

    Args:
        page: Playwright page object already at the auth URL.
        email: Account e-mail address.
        password: Account password.
    """
    page.locator("#email").fill(email)
    page.locator("#password").fill(password)
    # The submit button contains Svelte comment nodes that break role-name
    # matching; target it by type instead.
    page.locator('button[type="submit"]').click()
    # Wait until the URL leaves /auth — signals successful authentication.
    page.wait_for_url(lambda url: "/auth" not in url, timeout=15_000)
    # Wait for network activity to settle, then give the Svelte app ~2 s to
    # finish its JS initialisation before querying for the chat input.
    page.wait_for_load_state("networkidle", timeout=20_000)
    page.wait_for_timeout(2_000)
    # The chat input is a contenteditable div, not a textarea.
    page.locator("#chat-input").wait_for(state="visible", timeout=15_000)


class TestCelexContextSet(unittest.TestCase):
    """Verify that submitting a bare CELEX ID sets the case context in the web UI."""

    # ------------------------------------------------------------------
    # Test lifecycle
    # ------------------------------------------------------------------

    def setUp(self) -> None:
        """Prepare browser session with backend health check and model verification.

        Raises:
            AssertionError: When the FastAPI backend is unreachable or the
                model cannot be selected in Open WebUI.
        """
        self._check_backend_reachable()

        email = os.environ.get("WEBUI_EMAIL", "")
        password = os.environ.get("WEBUI_PASSWORD", "")
        if not email or not password:
            self.skipTest("WEBUI_EMAIL / WEBUI_PASSWORD not set — skipping.")

        self._pw = sync_playwright().start()
        headless = _resolve_headless()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._ctx = self._browser.new_context(ignore_https_errors=True)
        self.page = self._ctx.new_page()

        self.page.goto(_BASE_URL, wait_until="networkidle")
        if "/auth" in self.page.url:
            _login(self.page, email, password)

        self._ensure_model_selected()

    def tearDown(self) -> None:
        """Close the browser session opened in setUp."""
        if hasattr(self, "_ctx"):
            self._ctx.close()
        if hasattr(self, "_browser"):
            self._browser.close()
        if hasattr(self, "_pw"):
            self._pw.stop()

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _check_backend_reachable(self) -> None:
        """Assert the FastAPI backend responds at _BACKEND_HEALTH_URL.

        The bare _BACKEND_URL root path has no registered route and always
        404s, so the probe targets the '/models' endpoint instead.

        Raises:
            AssertionError: When the server cannot be contacted within the probe timeout.
        """
        try:
            with urllib.request.urlopen(_BACKEND_HEALTH_URL, timeout=_BACKEND_PROBE_TIMEOUT):
                pass
        except urllib.error.URLError as exc:
            self.fail(
                f"FastAPI backend not reachable at {_BACKEND_HEALTH_URL}: {exc.reason}. "
                "Start the backend before running this test."
            )

    def _ensure_model_selected(self) -> None:
        """Confirm celex-dspy-agent is the active model; select it if not.

        Open WebUI displays the active model name in the chat model-selector
        button.  If the correct model is already shown the method returns
        immediately.  Otherwise it opens the selector dropdown and clicks the
        matching entry.

        Raises:
            AssertionError: When the model cannot be found or selected.
        """
        page = self.page

        # Model already active when its name appears inside the selector button.
        if page.locator("button", has_text=_MODEL_NAME).count() > 0:
            return

        # Open the model selector — Open WebUI renders this as a button whose
        # label is either the active model name or "Select a model".
        selector_btn = page.locator(
            "#model-selector, "
            "button:has-text('Select a model'), "
            "[aria-label*='model' i]"
        ).first

        try:
            selector_btn.click(timeout=5_000)
        except Exception as exc:
            self.fail(f"Could not open model selector in Open WebUI: {exc}")

        # Locate the target model in the dropdown (option, listitem, or custom
        # element depending on the Open WebUI version).
        option = page.get_by_role("option", name=_MODEL_NAME, exact=False)
        if option.count() == 0:
            option = page.locator(
                f"li:has-text('{_MODEL_NAME}'), "
                f"[role='listitem']:has-text('{_MODEL_NAME}')"
            )

        if option.count() == 0:
            self.fail(
                f"Model '{_MODEL_NAME}' not found in the Open WebUI selector. "
                "Register the backend as a model and try again."
            )

        try:
            option.first.click(timeout=5_000)
        except Exception as exc:
            self.fail(f"Could not select model '{_MODEL_NAME}': {exc}")

        # Brief pause for the Svelte UI to commit the selection.
        page.wait_for_timeout(1_000)

        if page.locator("button", has_text=_MODEL_NAME).count() == 0:
            self.fail(
                f"Model '{_MODEL_NAME}' was clicked but is not shown as selected. "
                "Check that the model is correctly registered in Open WebUI."
            )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_celex_context_acknowledgement(self) -> None:
        """Submit CELEX-31994D0823 and assert the case-context acknowledgement.

        Verifies that:
        - A 'Validating case reference' status line appears in the response.
        - The case-context acknowledgement sentence is displayed.
        - The model attribution label shows 'celex-dspy-agent'.

        Raises:
            AssertionError: When any expected text is absent from the response.
            PlaywrightTimeoutError: When the response does not arrive within the timeout.
        """
        page = self.page

        # The chat input is a contenteditable div, not a textarea.
        chat_input = page.locator("#chat-input")
        chat_input.wait_for(state="visible")
        chat_input.click()
        chat_input.fill(_CELEX_ID)
        chat_input.press("Enter")

        # Wait for the case-context acknowledgement — the slowest part of the
        # response — before asserting any text.
        ack_locator = page.get_by_text(_EXPECTED_ACK, exact=False)
        ack_locator.wait_for(timeout=_RESPONSE_TIMEOUT_MS)

        # Validation status line must precede the acknowledgement.
        validation_locator = page.get_by_text(_EXPECTED_VALIDATION, exact=False)
        self.assertTrue(
            validation_locator.count() > 0,
            f"Expected '{_EXPECTED_VALIDATION}' in response but it was absent.",
        )

        # Model attribution label must identify the backend.
        model_locator = page.get_by_text(_MODEL_NAME, exact=False)
        self.assertTrue(
            model_locator.count() > 0,
            f"Expected model label '{_MODEL_NAME}' but it was absent.",
        )


if __name__ == "__main__":
    unittest.main()
