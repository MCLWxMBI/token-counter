import argparse
import os
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv


DEFAULT_URL = (
    "https://engage.vic.gov.au/"
    "energy-on-pty-ltd-electricity-retail-licence-application"
)
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_INPUT_PRICE = 3.0

load_dotenv(Path(__file__).with_name(".env"))


def fetch_raw_html(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": "token-counter/0.1"},
        timeout=30,
    )
    response.raise_for_status()
    return response.text


def count_input_tokens(html: str, model: str) -> int:
    client = anthropic.Anthropic()
    result = client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": html}],
    )
    return result.input_tokens


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a raw web page and estimate its Claude input cost."
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
        print(f"Fetching {args.url}...", flush=True)
        html = fetch_raw_html(args.url)
        print(f"Counting tokens with {args.model}...", flush=True)
        input_tokens = count_input_tokens(html, args.model)
    except requests.RequestException as exc:
        raise SystemExit(f"Could not fetch {args.url}: {exc}") from exc
    except anthropic.APIError as exc:
        raise SystemExit(f"Anthropic API error: {exc}") from exc

    estimated_cost = input_tokens / 1_000_000 * args.input_price

    print(f"URL: {args.url}")
    print(f"Downloaded: {len(html.encode('utf-8')):,} bytes of raw HTML")
    print(f"Model: {args.model}")
    print(f"Input tokens: {input_tokens:,}")
    print(f"Estimated input cost: ${estimated_cost:.6f} USD")


if __name__ == "__main__":
    main()
