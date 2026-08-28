import argparse
import os
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import anthropic
import matplotlib
import pdfplumber

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_INPUT_PRICE = 3.0
DEFAULT_OUTPUT_DIR = Path("tmp")
PDF_SOURCE_NAMES = frozenset(
    {
        "Victorian Environmental Water Holder",
        "Scrutiny of Acts and Regulations Committee – environment filter",
        "Joint Treaties – environment-related only",
    }
)

STABLE_CONTENT_SCRIPT = """
({ minimumCharacters, quietMilliseconds, timeoutMilliseconds }) =>
  new Promise((resolve, reject) => {
    const selectors = ["main", "[role='main']", "article", "#app", "body"];
    const target = selectors
      .map((selector) => document.querySelector(selector))
      .find((element) => element !== null);

    if (!target) {
      reject(new Error("No page content root was found"));
      return;
    }

    const loadingPlaceholders = [
      "loading",
      "slow connection",
      "try refresh",
      "please wait",
      "enable javascript",
    ];
    let quietTimer;
    let timeoutTimer;
    let lastText = "";

    const normalizedText = () =>
      (target.innerText || "").replace(/\\s+/g, " ").trim();

    const isMeaningful = (text) => {
      if (text.length < minimumCharacters) {
        return false;
      }
      const lowerText = text.toLowerCase();
      return !loadingPlaceholders.some(
        (placeholder) =>
          lowerText === placeholder ||
          (lowerText.length < 500 && lowerText.includes(placeholder)),
      );
    };

    const cleanup = () => {
      observer.disconnect();
      clearTimeout(quietTimer);
      clearTimeout(timeoutTimer);
    };

    const checkContent = () => {
      clearTimeout(quietTimer);
      lastText = normalizedText();
      if (!isMeaningful(lastText)) {
        return;
      }
      quietTimer = setTimeout(() => {
        const currentText = normalizedText();
        if (currentText === lastText && isMeaningful(currentText)) {
          cleanup();
          resolve(true);
        }
      }, quietMilliseconds);
    };

    const observer = new MutationObserver(checkContent);
    observer.observe(target, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: false,
    });

    timeoutTimer = setTimeout(() => {
      cleanup();
      const preview = lastText.slice(0, 160);
      reject(
        new Error(
          `Page content did not stabilize within ${timeoutMilliseconds}ms; ` +
          `last text length=${lastText.length}; preview=${JSON.stringify(preview)}`,
        ),
      );
    }, timeoutMilliseconds);

    checkContent();
  })
"""

load_dotenv(Path(__file__).with_name(".env"))


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class SourceResult:
    source: Source
    processed_bytes: int
    input_tokens: int
    estimated_cost: float
    output_path: Path


@dataclass(frozen=True)
class SourceFailure:
    source: Source
    error: str


# Victoria: sources
SOURCES = (
    # Parliament of Victoria – inquiries
    Source("Parliament of Victoria – inquiries", "https://www.parliament.vic.gov.au/apartmentrenewables"),
    # Melbourne Water
    Source("Melbourne Water", "https://letstalk.melbournewater.com.au/psp"),
    # Greater Western Water
    Source("Greater Western Water", "https://www.gww.com.au/faults-works/upgrades-projects/planned-works-projects/romsey-water-filtration-plant"),
    # South East Water
    Source("South East Water", "https://southeastwater.com.au/faults-and-works/works/projects/aquarevo/welcome-to-aquarevo/project/"),
    # Yarra Valley Water
    Source("Yarra Valley Water", "https://engage.yvw.com.au/projects"),
    # Barwon Water
    Source("Barwon Water", "https://www.barwonwater.vic.gov.au/about-us/news-and-events/news/major-water-storage-site-to-double-in-capacity"),
    # Coliban Water
    Source("Coliban Water", "https://connect.coliban.com.au/PS28"),
    # East Gippsland Water
    Source("East Gippsland Water", "https://egwater.vic.gov.au/community/news/share-your-thoughts-on-our-future-services"),
    # Goulburn Valley Water
    Source("Goulburn Valley Water", "https://watermatters.gvwater.vic.gov.au/shepparton-clear-water-storage-tank"),
    # Gippsland Water
    Source("Gippsland Water", "https://www.gippswater.com.au/outages-works-and-projects/major-projects/current-major-projects/new-treated-water-storage-traralgon"),
    # Grampians Wimmera Mallee Water
    Source("Grampians Wimmera Mallee Water", "https://gwmwater.org.au/building-and-development/projects/current-projects/east-grampians-rural-pipeline/"),
    # Lower Murray Water
    Source("Lower Murray Water", "https://yoursay.lmw.vic.gov.au/price-submission-2028-2033"),
    # North East Water
    Source("North East Water", "https://haveyoursay.newater.com.au/projects"),
    # South Gippsland Water
    Source("South Gippsland Water", "https://www.sgwater.com.au/news/wosup-wonthaggi/"),
    # Wannon Water
    Source("Wannon Water", "https://engage.wannonwater.com.au/share-your-thoughts"),
    # Western Port Water
    Source("Western Port Water", "https://www.westernportwater.com.au/our-community/projects/current-projects/nextgenerationupgrade/"),
    # Southern Rural Water
    Source("Southern Rural Water", "https://www.srw.com.au/initiatives/projects/avon-valley-water-security-project"),
    # Goulburn-Murray Water
    Source("Goulburn-Murray Water", "https://yoursay.gmwater.com.au/price-plan-2028"),
    # Essential Services Commission
    Source("Essential Services Commission", "https://www.esc.vic.gov.au/current-consultations"),
    # Victorian Auditor-General's Office
    Source("Victorian Auditor-General's Office", "https://www.audit.vic.gov.au/annual-plan"),
    # Commissioner for Environmental Sustainability Victoria
    Source("Commissioner for Environmental Sustainability Victoria", "https://www.ces.vic.gov.au/news/celebrate-national-science-week-2026"),
    # Vic Catchments
    Source("Vic Catchments", "https://viccatchments.com.au/priority-projects/"),
    # Victorian Environmental Water Holder
    Source("Victorian Environmental Water Holder", "https://www.vewh.vic.gov.au/planning-and-reporting/seasonal-watering-plan"),
    # Engage Victoria
    Source("Engage Victoria", "https://www.vic.gov.au/notice-preparation-regulatory-impact-statement-labour-hire-licensing-amendment-regulations-2026"),

    # Federal agencies

    # Department of Industry, Science and Resources
    Source("Department of Industry, Science and Resources", "https://consult.industry.gov.au/horizon-europe-implementation"),
    # Productivity Commission
    Source("Productivity Commission", "https://www.pc.gov.au/inquiries-and-research/gst-reforms/"),
    # Climate Change Authority
    Source("Climate Change Authority", "https://consult.climatechangeauthority.gov.au/2026-apa-consultation-paper"),
    # Australian Energy Market Commission
    Source("Australian Energy Market Commission", "https://www.aemc.gov.au/market-reviews-advice/electricity-network-regulation-review-package-2"),
    # Department of Climate Change, Energy, the Environment and Water
    Source("Department of Climate Change, Energy, the Environment and Water", "https://consult.dcceew.gov.au/australia-singapore-transboundary-carbon-capture-and-sequestration-bilateral-instrument"),
    # Clean Energy Regulator
    Source("Clean Energy Regulator", "https://cer.gov.au/news-and-media/public-consultations"),
    # Department of Infrastructure, Transport, Regional Development, Communications, Sport and the Arts
    Source("Department of Infrastructure, Transport, Regional Development, Communications, Sport and the Arts", "https://www.infrastructure.gov.au/department/media/news/consultation-opens-new-aviation-disability-standards"),

    # Victorian parliamentary committees

    # Legislative Assembly Environment and Planning Committee
    Source("Legislative Assembly Environment and Planning Committee", "https://www.parliament.vic.gov.au/apartmentrenewables"),
    # Legislative Council Environment and Planning Committee
    Source("Legislative Council Environment and Planning Committee", "https://www.parliament.vic.gov.au/2026firesinquiry"),
    # Scrutiny of Acts and Regulations Committee – environment filter
    Source("Scrutiny of Acts and Regulations Committee – environment filter", "https://www.legislation.vic.gov.au/bills/domestic-gas-choice-repeal-gas-appliance-ban-bill-2026"),

    # Commonwealth parliamentary committees

    # Senate Environment and Communications
    Source("Senate Environment and Communications", "https://www.aph.gov.au/Parliamentary_Business/Committees/Senate/Environment_and_Communications/AIdatacentres48P"),
    # Senate Rural and Regional Affairs and Transport
    Source("Senate Rural and Regional Affairs and Transport", "https://www.aph.gov.au/Parliamentary_Business/Committees/Senate/Rural_and_Regional_Affairs_and_Transport/"),
    # House Climate Change, Energy, Environment and Water
    Source("House Climate Change, Energy, Environment and Water", "https://www.aph.gov.au/Parliamentary_Business/Committees/House/Climate_Change_Energy_Environment_and_Water/Solarpanelrecycling"),
    # House Industry, Innovation and Science
    Source("House Industry, Innovation and Science", "https://www.aph.gov.au/Parliamentary_Business/Committees/House/Industry_Innovation_and_Science/AustralianTyreIndustry"),
    # House Regional Development, Infrastructure and Transport
    Source("House Regional Development, Infrastructure and Transport", "https://www.aph.gov.au/Parliamentary_Business/Committees/House/Regional_Development_Infrastructure_and_Transport/LocalGovernmentFunding"),
    # House Primary Industries
    Source("House Primary Industries", "https://www.aph.gov.au/Parliamentary_Business/Committees/House/Primary_Industries/CriticalMinerals"),
    # Joint Aboriginal and Torres Strait Islander Affairs
    Source("Joint Aboriginal and Torres Strait Islander Affairs", "https://www.aph.gov.au/Parliamentary_Business/Committees/Joint/Aboriginal_and_Torres_Strait_Islander_Affairs/Responsestoracism"),
    # Joint Treaties – environment-related only
    Source("Joint Treaties – environment-related only", "https://www.aph.gov.au/Parliamentary_Business/Committees/Joint/Treaties/2025CITESAmendments/Treaty_being_considered"),
    # Joint Northern Australia
    Source("Joint Northern Australia", "https://www.aph.gov.au/Parliamentary_Business/Committees/Joint/Northern_Australia/Industries"),
)


def fetch_rendered_html(page: Page, url: str) -> str:
    """Load a URL in an existing browser page and return its rendered DOM."""
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function(
        "document.body && document.body.innerText.trim().length > 0",
        timeout=30_000,
    )
    return page.content()


def wait_for_stable_page_content(
    page: Page,
    minimum_characters: int = 200,
    quiet_milliseconds: int = 750,
    timeout_milliseconds: int = 30_000,
) -> None:
    """Wait until meaningful page text stops receiving relevant DOM mutations."""
    page.evaluate(
        STABLE_CONTENT_SCRIPT,
        {
            "minimumCharacters": minimum_characters,
            "quietMilliseconds": quiet_milliseconds,
            "timeoutMilliseconds": timeout_milliseconds,
        },
    )


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
        "input",
        "button",
        "select",
        "textarea",
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


def normalized_label(value: str) -> str:
    """Normalize human-readable labels for resilient source-specific matching."""
    return " ".join(value.split()).casefold()


def is_pdf_link(href: str | None) -> bool:
    return bool(href and href.casefold().split("?", 1)[0].endswith(".pdf"))


def extract_pdf_text(page: Page, pdf_url: str) -> str:
    """Download a PDF through Playwright and return normalized embedded text."""
    absolute_url = urljoin(page.url, pdf_url)
    response = page.context.request.get(absolute_url, timeout=30_000)
    if not response.ok:
        raise ValueError(
            f"PDF download failed with HTTP {response.status} "
            f"{response.status_text}: {absolute_url}"
        )

    try:
        with pdfplumber.open(BytesIO(response.body())) as pdf:
            extracted_pages = [pdf_page.extract_text() or "" for pdf_page in pdf.pages]
    except Exception as exc:
        raise ValueError(f"Could not parse PDF {absolute_url}: {exc}") from exc

    lines = (
        " ".join(line.split())
        for page_text in extracted_pages
        for line in page_text.splitlines()
    )
    text = "\n".join(line for line in lines if line)
    if not text:
        raise ValueError(f"PDF contained no extractable text: {absolute_url}")
    return text


def extract_victorian_sarc_text(page: Page, rendered_html: str) -> str:
    """Extract the Victorian SARC Introduction print – Bill PDF text."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    target_label = normalized_label("Introduction print – Bill")
    heading = next(
        (
            candidate
            for candidate in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
            if normalized_label(candidate.get_text(" ", strip=True)) == target_label
        ),
        None,
    )
    if heading is None:
        raise ValueError("Victorian SARC: 'Introduction print – Bill' panel not found")

    panel = heading.find_parent(
        "div", class_="tide-bill-intro__section-document"
    ) or heading.parent
    pdf_anchor = next(
        (
            anchor
            for anchor in panel.find_all("a", href=True)
            if is_pdf_link(anchor.get("href"))
        ),
        None,
    )
    if pdf_anchor is None:
        raise ValueError(
            "Victorian SARC: PDF link not found in 'Introduction print – Bill' panel"
        )
    return extract_pdf_text(page, pdf_anchor["href"])


def extract_vewh_text(page: Page, rendered_html: str) -> str:
    """Extract the VEWH Section 1 Introduction PDF text."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    target_label = normalized_label("Section 1 Introduction")
    pdf_anchor = next(
        (
            anchor
            for anchor in soup.find_all("a", href=True)
            if is_pdf_link(anchor.get("href"))
            and target_label
            in {
                normalized_label(anchor.get("title", "")),
                normalized_label(anchor.get_text(" ", strip=True)),
            }
        ),
        None,
    )
    if pdf_anchor is None:
        raise ValueError("VEWH: 'Section 1 Introduction' PDF link not found")
    return extract_pdf_text(page, pdf_anchor["href"])


def extract_joint_treaties_text(page: Page, rendered_html: str) -> str:
    """Extract the Joint Treaties National Interest Analysis PDF text."""
    soup = BeautifulSoup(rendered_html, "html.parser")
    target_label = normalized_label("National Interest Analysis")
    paragraph = next(
        (
            candidate
            for candidate in soup.find_all("p")
            if normalized_label(candidate.get_text(" ", strip=True)).startswith(
                target_label
            )
        ),
        None,
    )
    if paragraph is None:
        raise ValueError("Joint Treaties: 'National Interest Analysis' paragraph not found")

    pdf_anchor = next(
        (
            anchor
            for anchor in paragraph.find_all("a", href=True)
            if is_pdf_link(anchor.get("href"))
        ),
        None,
    )
    if pdf_anchor is None:
        raise ValueError(
            "Joint Treaties: National Interest Analysis PDF link not found"
        )
    return extract_pdf_text(page, pdf_anchor["href"])


def extract_source_text(source: Source, page: Page, rendered_html: str) -> str:
    """Dispatch sources that require targeted documents to dedicated extractors."""
    if source.name == "Victorian Environmental Water Holder":
        return extract_vewh_text(page, rendered_html)
    if source.name == "Scrutiny of Acts and Regulations Committee – environment filter":
        return extract_victorian_sarc_text(page, rendered_html)
    if source.name == "Joint Treaties – environment-related only":
        return extract_joint_treaties_text(page, rendered_html)
    return extract_page_text(rendered_html)


def is_pdf_source(source: Source) -> bool:
    return source.name in PDF_SOURCE_NAMES


def process_source_content(source: Source, page: Page) -> str:
    """Render one source, wait for stability, and return token-count text."""
    fetch_rendered_html(page, source.url)
    wait_for_stable_page_content(page)
    rendered_html = page.content()
    page_text = extract_source_text(source, page, rendered_html)

    if not page_text:
        raise ValueError("rendered page contained no readable text")
    if not is_pdf_source(source) and len(page_text) < 200:
        preview = page_text[:160]
        raise ValueError(
            "rendered page contained fewer than 200 extracted characters; "
            f"length={len(page_text)}; preview={preview!r}"
        )
    return page_text


def source_slug(source: Source) -> str:
    """Return a stable, filesystem-safe slug for a source."""
    normalized = unicodedata.normalize("NFKD", source.name).encode("ascii", "ignore")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.decode().lower()).strip("-")
    if not slug:
        raise ValueError(f"Source name does not produce a usable filename: {source.name!r}")
    return slug


def validate_sources(sources: tuple[Source, ...]) -> None:
    """Reject duplicate names or output slugs before starting the batch."""
    names: set[str] = set()
    slugs: set[str] = set()
    for source in sources:
        slug = source_slug(source)
        if source.name in names:
            raise ValueError(f"Duplicate source name: {source.name}")
        if slug in slugs:
            raise ValueError(f"Duplicate source output slug: {slug}")
        names.add(source.name)
        slugs.add(slug)


def source_output_path(
    source: Source,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    return output_dir / f"{source_slug(source)}.txt"


def write_processed_text(
    source: Source,
    page_text: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    """Write processed text to a stable, per-source UTF-8 file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = source_output_path(source, output_dir)
    output_path.write_text(page_text, encoding="utf-8")
    return output_path


def count_input_tokens(
    content: str,
    model: str,
    client: anthropic.Anthropic,
) -> int:
    result = client.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": content}],
    )
    return result.input_tokens


def write_token_chart(
    results: list[SourceResult],
    output_path: Path,
    model: str = DEFAULT_MODEL,
) -> None:
    """Write a horizontal bar chart of successful source token counts."""
    if not results:
        return

    ordered = sorted(results, key=lambda result: result.input_tokens)
    names = [result.source.name for result in ordered]
    counts = [result.input_tokens for result in ordered]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(14, max(7, len(ordered) * 0.38)))
    bars = axis.barh(names, counts, color="#3973ac")
    axis.set_xlabel(f"Input tokens ({model})")
    axis.set_title("Processed article input token count by source")
    axis.bar_label(bars, labels=[f"{count:,}" for count in counts], padding=3)
    axis.grid(axis="x", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close(figure)


def process_sources(
    sources: tuple[Source, ...],
    model: str,
    input_price: float,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[list[SourceResult], list[SourceFailure]]:
    """Render, extract, save, and count every source in a single browser run."""
    validate_sources(sources)
    client = anthropic.Anthropic()
    results: list[SourceResult] = []
    failures: list[SourceFailure] = []
    cache: dict[str, tuple[str, int] | Exception] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for source in sources:
                cached = cache.get(source.url)
                if isinstance(cached, Exception):
                    failures.append(SourceFailure(source, str(cached)))
                    continue

                try:
                    if cached is None:
                        page = browser.new_page()
                        try:
                            page_text = process_source_content(source, page)
                        finally:
                            page.close()

                        output_path = write_processed_text(source, page_text, output_dir)
                        input_tokens = count_input_tokens(page_text, model, client)
                        cache[source.url] = (page_text, input_tokens)
                    else:
                        page_text, input_tokens = cached
                        output_path = write_processed_text(source, page_text, output_dir)

                    results.append(
                        SourceResult(
                            source=source,
                            processed_bytes=len(page_text.encode("utf-8")),
                            input_tokens=input_tokens,
                            estimated_cost=input_tokens / 1_000_000 * input_price,
                            output_path=output_path,
                        )
                    )
                except (PlaywrightError, anthropic.APIError, OSError, ValueError) as exc:
                    cache[source.url] = exc
                    failures.append(SourceFailure(source, str(exc)))
        finally:
            browser.close()

    return results, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render configured websites and compare their Claude input cost."
    )
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

    print(f"Processing {len(SOURCES)} configured sources...", flush=True)
    try:
        results, failures = process_sources(
            SOURCES,
            model=args.model,
            input_price=args.input_price,
        )
    except PlaywrightError as exc:
        raise SystemExit(f"Could not start or use Chromium: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"Invalid source configuration: {exc}") from exc

    chart_path = DEFAULT_OUTPUT_DIR / "input-token-counts.png"
    write_token_chart(results, chart_path, args.model)

    for result in results:
        print(f"\n{result.source.name}")
        print(f"  URL: {result.source.url}")
        print(f"  Processed: {result.processed_bytes:,} bytes")
        print(f"  Text: {result.output_path}")
        print(f"  Input tokens: {result.input_tokens:,}")
        print(f"  Estimated cost: ${result.estimated_cost:.6f} USD")

    total_tokens = sum(result.input_tokens for result in results)
    total_cost = sum(result.estimated_cost for result in results)
    print("\nBatch summary")
    print(f"  Successful: {len(results)}/{len(SOURCES)}")
    print(f"  Total input tokens: {total_tokens:,}")
    print(f"  Total estimated cost: ${total_cost:.6f} USD")
    if results:
        print(f"  Chart: {chart_path}")

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"  - {failure.source.name}: {failure.error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
