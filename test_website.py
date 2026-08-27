"""Diagnose rendering and text extraction for one website.

Usage:
    uv run python test_website.py https://example.com/article
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Browser, Page, Request, Response, sync_playwright

from token_counter.counter import extract_page_text, fetch_rendered_html


OUTPUT_DIR = Path("tmp/test-website")
ARTIFACT_NAMES = (
    "rendered.html",
    "extracted.txt",
    "screenshot.png",
    "console.log",
    "failed-requests.log",
    "navigation.log",
    "summary.txt",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_output_directory(output_dir: Path) -> None:
    """Remove known artifacts so a failed run cannot expose stale evidence."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for artifact_name in ARTIFACT_NAMES:
        (output_dir / artifact_name).unlink(missing_ok=True)


def safe_page_url(page: Page | None) -> str:
    if page is None:
        return "unavailable"
    try:
        return page.url
    except Exception as exc:  # The browser may already have terminated.
        return f"unavailable ({type(exc).__name__}: {exc})"


def navigation_line(response: Response) -> str | None:
    """Describe document navigation responses, including redirect origins."""
    try:
        request = response.request
        if not request.is_navigation_request():
            return None
        redirected_from = request.redirected_from
        redirect_detail = (
            f" redirected_from={redirected_from.url}" if redirected_from else ""
        )
        return f"{utc_now()} status={response.status} url={response.url}{redirect_detail}"
    except Exception as exc:
        return f"{utc_now()} response-inspection-error={type(exc).__name__}: {exc}"


def failed_request_line(request: Request) -> str:
    try:
        return f"{utc_now()} url={request.url} failure={request.failure or 'unknown'}"
    except Exception as exc:
        return f"{utc_now()} request-inspection-error={type(exc).__name__}: {exc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose Chromium rendering and production text extraction for one URL."
    )
    parser.add_argument("url", help="website URL to diagnose")
    return parser.parse_args()


def run_diagnostic(url: str, output_dir: Path = OUTPUT_DIR) -> int:
    prepare_output_directory(output_dir)

    started_at = utc_now()
    stage = "starting Chromium"
    browser: Browser | None = None
    page: Page | None = None
    rendered_html = ""
    page_text = ""
    console_lines: list[str] = []
    failed_request_lines: list[str] = []
    navigation_lines = [f"{started_at} requested_url={url}"]
    errors: list[str] = []
    screenshot_written = False

    print(f"[1/5] Starting Chromium for {url}", flush=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()

            page.on(
                "console",
                lambda message: console_lines.append(
                    f"{utc_now()} console.{message.type}: {message.text}"
                ),
            )
            page.on(
                "pageerror",
                lambda error: console_lines.append(
                    f"{utc_now()} pageerror: {type(error).__name__}: {error}"
                ),
            )
            page.on(
                "requestfailed",
                lambda request: failed_request_lines.append(
                    failed_request_line(request)
                ),
            )

            def record_navigation(response: Response) -> None:
                line = navigation_line(response)
                if line:
                    navigation_lines.append(line)

            page.on("response", record_navigation)

            try:
                stage = "rendering page"
                print("[2/5] Rendering with fetch_rendered_html()", flush=True)
                rendered_html = fetch_rendered_html(page, url)

                stage = "extracting page text"
                print("[3/5] Extracting with extract_page_text()", flush=True)
                page_text = extract_page_text(rendered_html)
                if not page_text:
                    raise ValueError("extract_page_text() returned an empty string")
            except Exception as exc:
                errors.append(f"{stage}: {type(exc).__name__}: {exc}")
            finally:
                stage = "capturing diagnostic artifacts"
                print("[4/5] Capturing DOM and screenshot", flush=True)

                if not rendered_html:
                    try:
                        rendered_html = page.content()
                    except Exception as exc:
                        errors.append(
                            f"capturing partial DOM: {type(exc).__name__}: {exc}"
                        )

                if rendered_html and not page_text:
                    try:
                        page_text = extract_page_text(rendered_html)
                    except Exception as exc:
                        errors.append(
                            f"extracting partial DOM: {type(exc).__name__}: {exc}"
                        )

                try:
                    page.screenshot(
                        path=str(output_dir / "screenshot.png"),
                        full_page=True,
                    )
                    screenshot_written = True
                except Exception as exc:
                    errors.append(f"capturing screenshot: {type(exc).__name__}: {exc}")

                navigation_lines.append(
                    f"{utc_now()} final_url={safe_page_url(page)}"
                )
                page.close()
                browser.close()
                browser = None
    except Exception as exc:
        errors.append(f"{stage}: {type(exc).__name__}: {exc}")
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                errors.append(f"closing browser: {type(exc).__name__}: {exc}")

    stage = "writing diagnostic artifacts"
    print("[5/5] Writing diagnostic bundle", flush=True)

    (output_dir / "rendered.html").write_text(rendered_html, encoding="utf-8")
    (output_dir / "extracted.txt").write_text(page_text, encoding="utf-8")
    (output_dir / "console.log").write_text(
        "\n".join(console_lines), encoding="utf-8"
    )
    (output_dir / "failed-requests.log").write_text(
        "\n".join(failed_request_lines), encoding="utf-8"
    )
    (output_dir / "navigation.log").write_text(
        "\n".join(navigation_lines), encoding="utf-8"
    )

    finished_at = utc_now()
    success = not errors and bool(page_text)
    summary_lines = [
        f"status: {'SUCCESS' if success else 'FAILED'}",
        f"requested_url: {url}",
        f"final_url: {safe_page_url(page)}",
        f"started_at: {started_at}",
        f"finished_at: {finished_at}",
        f"last_stage: {stage}",
        f"rendered_html_bytes: {len(rendered_html.encode('utf-8'))}",
        f"extracted_text_bytes: {len(page_text.encode('utf-8'))}",
        f"console_entries: {len(console_lines)}",
        f"failed_requests: {len(failed_request_lines)}",
        f"screenshot_written: {screenshot_written}",
        "errors:",
    ]
    summary_lines.extend(f"- {error}" for error in errors)
    if not errors:
        summary_lines.append("- none")
    (output_dir / "summary.txt").write_text(
        "\n".join(summary_lines) + "\n", encoding="utf-8"
    )

    print(f"Status: {'SUCCESS' if success else 'FAILED'}")
    print(f"Final URL: {safe_page_url(page)}")
    print(f"Rendered HTML: {len(rendered_html.encode('utf-8')):,} bytes")
    print(f"Extracted text: {len(page_text.encode('utf-8')):,} bytes")
    print(f"Artifacts: {output_dir}")
    if errors:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")

    return 0 if success else 1


def main() -> None:
    args = parse_args()
    raise SystemExit(run_diagnostic(args.url))


if __name__ == "__main__":
    main()
