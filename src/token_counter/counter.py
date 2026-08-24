import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


DEFAULT_URL = (
    "https://engage.vic.gov.au/"
    "energy-on-pty-ltd-electricity-retail-licence-application"
)
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_INPUT_PRICE = 3.0

load_dotenv(Path(__file__).with_name(".env"))


def fetch_rendered_html(url: str) -> str:
    """Load a URL in headless Chromium and return its rendered DOM."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(
                "document.body && document.body.innerText.trim().length > 0",
                timeout=30_000,
            )
            return page.content()
        finally:
            browser.close()


def extract_page_text(html: str) -> str:
    """Extract compact, human-readable content from rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")

    non_content_tags = (
        "head",
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "dialog",
    )
    for element in soup.find_all(non_content_tags):
        element.decompose()

    block_tags = (
        "address",
        "blockquote",
        "br",
        "dd",
        "dt",
        "figcaption",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "pre",
        "td",
        "th",
    )
    for element in soup.find_all(block_tags):
        element.append("\n")

    lines = (" ".join(line.split()) for line in soup.get_text(" ").splitlines())
    return "\n".join(line for line in lines if line)


def write_processed_text(
    page_text: str,
    output_dir: Path = Path("tmp"),
) -> Path:
    """Write processed page text to a uniquely timestamped UTF-8 file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    output_path = output_dir / f"output-{timestamp}.txt"
    output_path.write_text(page_text, encoding="utf-8")
    return output_path


def count_input_tokens(content: str, model: str) -> int:
    client = anthropic.Anthropic()
    result = client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": content}],
    )
    return result.input_tokens


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a web page and estimate its Claude input cost."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--input-price",
        type=float,
        default=DEFAULT_INPUT_PRICE,
        metavar="USD_PER_MTOK",
        help="model input price in USD per million tokens (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY before running this command.")

    try:
        print(f"Rendering {args.url}...", flush=True)
        rendered_html = fetch_rendered_html(args.url)
        page_text = extract_page_text(rendered_html)
        if not page_text:
            raise SystemExit(f"Rendered page contained no readable text: {args.url}")
        output_path = write_processed_text(page_text)
        print(f"Counting tokens with {args.model}...", flush=True)
        input_tokens = count_input_tokens(page_text, args.model)
    except PlaywrightError as exc:
        raise SystemExit(f"Could not render {args.url}: {exc}") from exc
    except anthropic.APIError as exc:
        raise SystemExit(f"Anthropic API error: {exc}") from exc

    estimated_cost = input_tokens / 1_000_000 * args.input_price

    print(f"URL: {args.url}")
    print(f"Rendered DOM: {len(rendered_html.encode('utf-8')):,} bytes")
    print(f"Processed: {len(page_text.encode('utf-8')):,} bytes of page text")
    print(f"Processed text: {output_path}")
    print(f"Model: {args.model}")
    print(f"Input tokens: {input_tokens:,}")
    print(f"Estimated input cost: ${estimated_cost:.6f} USD")


if __name__ == "__main__":
    main()
