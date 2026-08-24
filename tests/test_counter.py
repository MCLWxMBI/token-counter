import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import Error as PlaywrightError

from token_counter.counter import (
    extract_page_text,
    fetch_rendered_html,
    write_processed_text,
)


class FetchRenderedHtmlTests(unittest.TestCase):
    @patch("token_counter.counter.sync_playwright")
    def test_returns_rendered_dom_and_closes_browser(self, mock_sync_playwright) -> None:
        playwright = mock_sync_playwright.return_value.__enter__.return_value
        browser = playwright.chromium.launch.return_value
        page = browser.new_page.return_value
        page.content.return_value = "<html><body><main>Rendered content</main></body></html>"

        result = fetch_rendered_html("https://example.com")

        self.assertEqual(result, page.content.return_value)
        playwright.chromium.launch.assert_called_once_with(headless=True)
        page.goto.assert_called_once_with(
            "https://example.com",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        page.wait_for_function.assert_called_once_with(
            "document.body && document.body.innerText.trim().length > 0",
            timeout=30_000,
        )
        browser.close.assert_called_once_with()

    @patch("token_counter.counter.sync_playwright")
    def test_closes_browser_when_rendering_fails(self, mock_sync_playwright) -> None:
        playwright = mock_sync_playwright.return_value.__enter__.return_value
        browser = playwright.chromium.launch.return_value
        page = browser.new_page.return_value
        page.goto.side_effect = PlaywrightError("navigation failed")

        with self.assertRaisesRegex(PlaywrightError, "navigation failed"):
            fetch_rendered_html("https://example.com")

        browser.close.assert_called_once_with()


class ExtractPageTextTests(unittest.TestCase):
    def test_removes_code_and_page_chrome_but_keeps_main_content(self) -> None:
        html = """
        <html>
          <head><title>Metadata title</title><style>.hidden { display: none; }</style></head>
          <body>
            <header>Site banner</header>
            <nav>Home Products Contact</nav>
            <main>
              <h1>Energy licence</h1>
              <p>Public <strong>submissions</strong> are open.</p>
              <ul><li>Read the application</li><li>Submit feedback</li></ul>
            </main>
            <form><label>Email</label><input></form>
            <script>window.trackingPayload = {"large": "value"};</script>
            <footer>Copyright notice</footer>
          </body>
        </html>
        """

        text = extract_page_text(html)

        self.assertEqual(
            text,
            "Energy licence\nPublic submissions are open.\n"
            "Read the application\nSubmit feedback",
        )
        self.assertNotIn("trackingPayload", text)
        self.assertNotIn("Site banner", text)

    def test_normalizes_whitespace(self) -> None:
        html = "<main><p>  Too    many\tspaces  </p>\n\n<p>Next line</p></main>"

        self.assertEqual(extract_page_text(html), "Too many spaces\nNext line")

    def test_handles_empty_and_malformed_html(self) -> None:
        self.assertEqual(extract_page_text(""), "")
        self.assertEqual(extract_page_text("<main><p>Still useful"), "Still useful")

    def test_extracts_content_from_a_rendered_spa_dom(self) -> None:
        rendered_html = """
        <html><body><div id="app">
          <main><h1>Rendered project</h1><p>Loaded by JavaScript.</p></main>
        </div></body></html>
        """

        self.assertEqual(
            extract_page_text(rendered_html),
            "Rendered project\nLoaded by JavaScript.",
        )


class WriteProcessedTextTests(unittest.TestCase):
    def test_writes_timestamped_utf8_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "nested"

            output_path = write_processed_text("Useful page text — ready", output_dir)

            self.assertEqual(output_path.parent, output_dir)
            self.assertRegex(
                output_path.name,
                r"^output-\d{8}-\d{6}-\d{6}\.txt$",
            )
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "Useful page text — ready",
            )


if __name__ == "__main__":
    unittest.main()
