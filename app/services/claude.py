import re
from functools import lru_cache
from string import Template

import anthropic

from app.config import settings

TOYOTA_PROMPT_TEMPLATE = Template("""# TASK: Exhaustive Python Scraper for Toyota/Lexus KSA (All Trims)

## CONTEXT:
The target website (toyota.com.sa) separates car trims into different tabs or sections (e.g., Gasoline vs. HEV/Hybrid). The goal is to extract EVERY available grade, not just the first three.

## INPUT PARAMETERS:
- Make: $make (toyota/lexus)
- Model: $model (e.g., camry)
- Year: $year (e.g., 2025)

## TECHNICAL REQUIREMENTS:
1. **Multi-Section Discovery:**
   - The script must iterate through ALL containers under "AVAILABLE GRADES".
   - It MUST check for both 'Gasoline' and 'Hybrid/HEV' sections.
   - If using BeautifulSoup, it should find all <li> or <div> elements within the grades carousel.
2. **Text-Based Fallback:** If selectors fail, use a regex search on the full page text to find all occurrences of "SAR" or "§" adjacent to trim names.
3. **VAT Handling:** Footnotes confirm 15% VAT is included. Set `vat_status: true`.

## OUTPUT SCHEMA (JSON):
{
    "metadata": { "source": "toyota.com.sa", "total_trims_found": "integer" },
    "results": [
        {
            "model_name": "string",
            "year": "integer",
            "trim_level": "string",
            "price_sar": "float",
            "vat_status": true
        }
    ]
}

## INSTRUCTIONS FOR SCRIPT GENERATION:
- **IMPORTANT:** Explicitly code the scraper to look for the "HEV" or "Hybrid" models which are often in a secondary <ul> or <div>.
- Use a single code block.
- No conversational filler.

## ⚠️ STRICT OUTPUT CONTROL:
- Return ONLY the Python code.
- Ensure the result list contains ALL 7+ trims if they exist on the page.
""")

HARAJ_PROMPT_TEMPLATE = Template("""# TASK: Per-Trim Haraj Marketplace Price Aggregator

## CONTEXT:
The Toyota.com.sa scraper produces a list of trims for a given vehicle. For each trim, search the Haraj classifieds marketplace (https://haraj.com.sa/en/) and compute the average market listing price for that specific trim.

## INPUT PARAMETERS:
- `make`: "$make"
- `model`: "$model"
- `year`: $year
- `trims`: $trims (list of trim names from the official Toyota scraper, e.g. ["GX", "GX2", "VX", "VXR"])

## TECHNICAL REQUIREMENTS:
1. **Engine:** Use `Playwright` (Python, async) to handle Haraj's dynamic JavaScript loading.
2. **Per-Trim Search Loop:** For EACH trim in the input list:
   - Build the search URL: `https://haraj.com.sa/en/tags/<model>%20<year>%20<trim>` (URL-encode spaces).
   - Load the page, scroll a few times to trigger infinite scroll, and collect at least 10-20 listings.
3. **Parsing Logic:**
   - Extract `title`, `price`, `city/location`, and listing `url` for each listing.
   - Use regex to clean prices (strip "SAR", "ر.س", "§", commas, whitespace). Convert to float.
   - Filter out obviously fake prices (e.g., 1, 1111111, anything < 5000 or > 1,000,000 SAR).
4. **Aggregation:** Per trim, compute `average_price_sar`, `min_price_sar`, `max_price_sar`, and `listing_count`.
5. **Resilience:** Stealthy `User-Agent`, randomized delay between scrolls (1-3s), graceful handling of empty results (return zero-listing trim entry, do not crash).

## OUTPUT SCHEMA (JSON):
{
    "source": "haraj.com.sa",
    "make": "string",
    "model": "string",
    "year": "integer",
    "trims": [
        {
            "trim": "string",
            "search_url": "string",
            "listing_count": "integer",
            "average_price_sar": 0.0,
            "min_price_sar": 0.0,
            "max_price_sar": 0.0,
            "sample_listings": [
                {
                    "title": "string",
                    "price_sar": 0.0,
                    "city": "string",
                    "url": "string"
                }
            ]
        }
    ]
}

## INSTRUCTIONS FOR CLAUDE:
- Provide a modular async Python class `HarajTrimAggregator` with an `aggregate(trims: list[str])` method.
- Iterate the input trims list and produce one entry per trim in the output.
- Include an `if __name__ == "__main__":` block using `asyncio.run` that demonstrates usage with the provided make/model/year/trims.
- Return ONLY the Python code block. No preamble. No postscript.
""")


@lru_cache
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def build_toyota_prompt(make: str, model: str, year: int) -> str:
    return TOYOTA_PROMPT_TEMPLATE.substitute(make=make, model=model, year=year)


def build_haraj_prompt(make: str, model: str, year: int, trims: list[str]) -> str:
    import json

    return HARAJ_PROMPT_TEMPLATE.substitute(
        make=make, model=model, year=year, trims=json.dumps(trims)
    )


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _generate(prompt: str) -> str:
    client = get_client()
    with client.messages.stream(
        model=settings.claude_model,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    text = "".join(b.text for b in response.content if b.type == "text")
    if not text:
        raise ValueError("model returned no text content")
    return _strip_code_fence(text)


def generate_toyota_scraper(make: str, model: str, year: int) -> str:
    return _generate(build_toyota_prompt(make, model, year))


def generate_haraj_scraper(make: str, model: str, year: int, trims: list[str]) -> str:
    return _generate(build_haraj_prompt(make, model, year, trims))
