#!/usr/bin/env python3
"""
search.py — Evidence-oriented Tavily search and webpage fetcher.

CLI:
  python3 search.py search "<query>"
  python3 search.py search "<query>" --profile cybersecurity
  python3 search.py search "<query>" --domains cisa.gov,nist.gov
  python3 search.py search "<query>" --format text
  python3 search.py fetch "<url>"

Important:
  Search results are candidate evidence, not verified facts.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except Exception as e:
    requests = None
    _requests_err = str(e)

try:
    from trafilatura import extract
except Exception as e:
    extract = None
    _trafilatura_err = str(e)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except Exception as e:
    sync_playwright = None
    _playwright_err = str(e)


SCRIPT_DIR = Path(__file__).parent.resolve()
TAVILY_ENDPOINT = "https://api.tavily.com/search"


def load_config():
    for path in (SCRIPT_DIR / "config.json", Path("/app/config.json")):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


CONFIG = load_config()
TAVILY_API_KEY = str(CONFIG.get("tavily_api_key", "")).strip()

FETCH_TIMEOUT = int(
    CONFIG.get(
        "web_fetch_timeout",
        os.environ.get("WEB_FETCH_TIMEOUT", "30")
    )
)

DEFAULT_MAX_RESULTS = min(
    max(int(CONFIG.get("web_max_results", 8)), 1),
    10
)


TRUSTED_DOMAINS = {
    "cisa.gov": ("government", 1.00),
    "cert-in.org.in": ("government", 1.00),
    "ncsc.gov.uk": ("government", 1.00),
    "enisa.europa.eu": ("government", 1.00),
    "nist.gov": ("standards", 1.00),
    "nvd.nist.gov": ("vulnerability_database", 1.00),
    "cve.org": ("vulnerability_database", 1.00),
    "owasp.org": ("security_guidance", 0.95),
    "attack.mitre.org": ("security_framework", 0.95),
    "cisecurity.org": ("security_guidance", 0.95),
    "usenix.org": ("academic", 0.95),
    "dl.acm.org": ("academic", 0.95),
    "ieeexplore.ieee.org": ("academic", 0.95),
    "arxiv.org": ("research", 0.85),
    "abuse.ch": ("threat_intelligence", 0.90),
    "shadowserver.org": ("threat_intelligence", 0.90),
    "openai.com": ("vendor_primary", 0.95),
    "huggingface.co": ("vendor_primary", 0.95),
    "microsoft.com": ("vendor_primary", 0.95),
    "cloud.google.com": ("vendor_primary", 0.95),
    "talosintelligence.com": ("vendor_research", 0.85),
    "unit42.paloaltonetworks.com": ("vendor_research", 0.85),
    "mandiant.com": ("vendor_research", 0.85),
    "welivesecurity.com": ("vendor_research", 0.85),
    "reuters.com": ("independent_news", 0.90),
    "apnews.com": ("independent_news", 0.90),
    "bbc.com": ("independent_news", 0.85),
    "bleepingcomputer.com": ("specialist_news", 0.75),
    "therecord.media": ("specialist_news", 0.75),
}


def domain_from_url(url):
    try:
        host = urlparse(url).hostname or ""
        host = host.lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def matching_trusted_domain(domain):
    if domain in TRUSTED_DOMAINS:
        return domain
    for trusted in TRUSTED_DOMAINS:
        if domain.endswith("." + trusted):
            return trusted
    return None


def classify_source(url):
    domain = domain_from_url(url)
    trusted = matching_trusted_domain(domain)
    if not trusted:
        return {
            "domain": domain,
            "source_type": "unknown",
            "trust_score": 0.20,
            "trusted": False,
        }
    source_type, trust_score = TRUSTED_DOMAINS[trusted]
    return {
        "domain": domain,
        "source_type": source_type,
        "trust_score": trust_score,
        "trusted": True,
    }


def validate_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def requests_ok():
    if requests is None:
        return False, f"(error: requests missing — pip install requests: {_requests_err})"
    return True, ""


def playwright_ok():
    if sync_playwright is None:
        return False, f"(error: playwright missing — pip install playwright: {_playwright_err})"
    return True, ""


def trafilatura_ok():
    if extract is None:
        return False, f"(error: trafilatura missing — pip install trafilatura: {_trafilatura_err})"
    return True, ""


def normalize_result(item, position):
    url = item.get("url", "")
    source = classify_source(url)
    return {
        "position": position,
        "title": item.get("title", ""),
        "url": url,
        "domain": source["domain"],
        "source_type": source["source_type"],
        "trust_score": source["trust_score"],
        "trusted_domain": source["trusted"],
        "relevance_score": item.get("score", 0),
        "published_date": item.get("published_date"),
        "snippet": item.get("content", ""),
        "raw_content": item.get("raw_content", ""),
        "verification_status": "unverified_candidate",
    }


def web_search(
    query,
    search_depth="advanced",
    max_results=8,
    profile=None,
    domains=None,
    include_raw=True,
):
    ok, msg = requests_ok()
    if not ok:
        return {"ok": False, "error": msg}

    if not TAVILY_API_KEY:
        return {
            "ok": False,
            "error": "tavily_api_key not found. Add it to config.json",
        }

    max_results = min(max(int(max_results), 1), 10)

    if search_depth not in {"basic", "advanced"}:
        search_depth = "advanced"

    # FIX: Tavily limits include_domains to ~10. Don't pass the full 28-domain list.
    # Only pass explicit domains, or none at all (search broadly, rank by trust after).
    include_domains = None
    if domains:
        include_domains = sorted(set(
            d.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
            for d in domains
            if d.strip()
        ))[:10]  # hard cap at 10

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": True if include_raw else False,  # FIX: boolean, not string
    }

    if include_domains:
        payload["include_domains"] = include_domains

    try:
        response = requests.post(TAVILY_ENDPOINT, json=payload, timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"Tavily request failed: {e}"}

    if response.status_code == 401:
        return {"ok": False, "error": "Tavily API 401: invalid API key"}
    if response.status_code == 429:
        return {"ok": False, "error": "Tavily API rate limit reached"}
    if response.status_code != 200:
        return {"ok": False, "error": f"Tavily HTTP {response.status_code}: {response.text[:300]}"}

    try:
        data = response.json()
    except Exception as e:
        return {"ok": False, "error": f"Invalid Tavily JSON: {e}"}

    raw_results = data.get("results", [])
    results = [normalize_result(item, index) for index, item in enumerate(raw_results, 1)]

    # Sort: trusted first, then by trust_score, then relevance
    results.sort(
        key=lambda x: (x["trusted_domain"], x["trust_score"], x["relevance_score"]),
        reverse=True,
    )
    for index, result in enumerate(results, 1):
        result["rank"] = index

    return {
        "ok": True,
        "query": query,
        "search_depth": search_depth,
        "profile": profile,
        "include_domains": include_domains or [],
        "answer_used": False,
        "evidence_policy": (
            "Results are unverified candidates. "
            "Fetch and inspect sources before making factual claims."
        ),
        "results": results,
    }


def extract_page_text(html):
    try:
        text = extract(
            html,
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=False,
            deduplicate=True,
            target_language="en",
        )
    except Exception:
        text = None

    if text and text.strip():
        return text.strip()

    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def web_fetch(url, max_chars=8000):
    if not validate_url(url):
        return {"ok": False, "error": f"Invalid HTTP(S) URL: {url}"}

    ok, msg = playwright_ok()
    if not ok:
        return {"ok": False, "error": msg}
    ok, msg = trafilatura_ok()
    if not ok:
        return {"ok": False, "error": msg}

    max_chars = max(int(max_chars), 1000)
    html = ""
    title = ""

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="CyberQwenResearchBot/1.0 (authorized security research)"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=FETCH_TIMEOUT * 1000)
            title = page.title()
            html = page.content()
            context.close()
            browser.close()
    except PWTimeout:
        return {"ok": False, "error": f"Page load timed out after {FETCH_TIMEOUT}s", "url": url}
    except Exception as e:
        return {"ok": False, "error": f"Playwright fetch failed: {e}", "url": url}

    if not html:
        return {"ok": False, "error": "Page returned empty content", "url": url}

    text = extract_page_text(html)
    if not text:
        return {"ok": False, "error": "Could not extract readable page content", "url": url}

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} characters]"

    source = classify_source(url)
    return {
        "ok": True,
        "url": url,
        "title": title,
        "domain": source["domain"],
        "source_type": source["source_type"],
        "trust_score": source["trust_score"],
        "trusted_domain": source["trusted"],
        "retrieval_status": "fetched",
        "truncated": truncated,
        "content": text,
    }


def format_search_text(data):
    """Convert JSON search result to human-readable text for the model."""
    if not data.get("ok"):
        return f"(search error: {data.get('error', 'unknown')})"

    lines = [f"Tavily search: {data['query']}", "=" * 50]
    if data.get("evidence_policy"):
        lines.append(f"Note: {data['evidence_policy']}\n")

    for r in data.get("results", []):
        trust = f"trust:{r['trust_score']:.2f}"
        lines.append(f"\n[{r['rank']}] {r['title']}  ({trust} | {r['source_type']})")
        lines.append(f"    URL: {r['url']}")
        lines.append(f"    {r['snippet'][:400]}{'...' if len(r['snippet']) > 400 else ''}")
        raw = r.get("raw_content", "")
        if raw:
            lines.append(f"    [Raw content: {len(raw)} chars]")
    return "\n".join(lines)


def format_fetch_text(data):
    """Convert JSON fetch result to human-readable text."""
    if not data.get("ok"):
        return f"(fetch error: {data.get('error', 'unknown')})"

    lines = [
        f"Fetched: {data['url']}",
        f"Title: {data.get('title', '')}",
        f"Domain: {data['domain']} ({data['source_type']}, trust:{data['trust_score']:.2f})",
        "=" * 50,
        data.get("content", ""),
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["json", "text"], default="json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query", nargs="+")
    search_parser.add_argument("--profile", choices=["cybersecurity"], default=None)
    search_parser.add_argument("--domains", default=None, help="Comma-separated domain list")
    search_parser.add_argument("--depth", choices=["basic", "advanced"], default="advanced")
    search_parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    search_parser.add_argument("--no-raw", action="store_true")

    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("url")
    fetch_parser.add_argument("--max-chars", type=int, default=8000)

    args = parser.parse_args()

    if args.command == "search":
        explicit_domains = args.domains.split(",") if args.domains else None
        result = web_search(
            query=" ".join(args.query),
            search_depth=args.depth,
            max_results=args.max_results,
            profile=args.profile,
            domains=explicit_domains,
            include_raw=not args.no_raw,
        )
        if args.format == "text":
            print(format_search_text(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        result = web_fetch(url=args.url, max_chars=args.max_chars)
        if args.format == "text":
            print(format_fetch_text(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
