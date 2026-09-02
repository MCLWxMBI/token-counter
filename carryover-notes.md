# Reusing the website text extraction helpers

Copy the complete marked block from `src/token_counter/counter.py`, starting at
`BEGIN REUSABLE WEBSITE TEXT EXTRACTION HELPERS` and ending at the matching
`END` marker. The block is self-contained and can become a `helpers.py` module.

`SOURCES` and `PDF_SOURCE_NAMES` are intentionally near the top so a consuming
project can customize them. Add ordinary HTML sources directly to `SOURCES`.
To add another specially selected PDF source, add its name constant, include it
in `PDF_SOURCE_NAMES`, implement its dedicated extractor, and add a direct
name-comparison branch in `extract_source_text`.

The caller creates and shuts down Chromium. `process_single_source(source,
browser)` creates and closes one page, renders and stabilizes the landing page,
chooses HTML or source-specific PDF extraction, and returns text. It deliberately
does not validate text length, count tokens, write output, or close the browser.
Those policies belong to the caller.

Required packages:

- Playwright (including an installed Chromium runtime)
- Beautiful Soup 4
- pdfplumber

Minimal use:

```python
from playwright.sync_api import sync_playwright

from helpers import SOURCES, process_single_source

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        for source in SOURCES:
            text = process_single_source(source, browser)
            # Validate and consume text here.
    finally:
        browser.close()
```
