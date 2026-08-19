#!/usr/bin/env python3
"""
ingest_sources.py  —  Populate CyberQwen's RAG store (rag.py) from live cybersecurity sources.

Keep this file in the SAME directory as rag.py (the PAI project root), so `import rag`
works. It does NOT create its own database or embedding logic — it reuses rag.py's
RAGStore.ingest(), so embeddings, normalization, chunking and the SQLite schema stay
identical to what build_rag_context() reads back. The actual DB lives wherever rag.py
points (default ~/.cyberqwen/rag.db); this script never touches that path directly.

SOURCES
  1. CVEs      fkie-cad reconstructed NVD yearly feeds (last N years), CVSS-filtered.
  2. CISA KEV  Known-Exploited-Vulnerabilities catalog (small, high value, full).
  3. News RSS  The Hacker News, BleepingComputer, Krebs, Dark Reading, ...
              (RSS returns only each site's RECENT items — re-run on a schedule to grow it.)

IDEMPOTENT
  Each document is stored under a stable `source` key (the CVE id, "KEV:<id>", or the
  article URL). Before ingesting we check if that source already exists, so re-runs only
  add new material. Safe to cron daily.

USAGE
  python3 ingest_sources.py --all
  python3 ingest_sources.py --cve --years 2 --min-cvss 7.0
  python3 ingest_sources.py --kev
  python3 ingest_sources.py --news
  python3 ingest_sources.py --cve --min-cvss 0      # every severity (~90k docs, slow)

DEPS
  pip install requests feedparser        # numpy/urllib/sqlite come via rag.py / stdlib
"""

import argparse
import lzma
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

# import rag.py from this script's own directory regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rag  # noqa: E402  — provides store().ingest(), the shared embedding + schema

try:
    import feedparser
except ImportError:
    feedparser = None  # only needed for --news

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
YEARS_BACK   = 2
CVE_MIN_CVSS = 7.0        # HIGH + CRITICAL only. Set 0.0 to ingest every severity.

CVE_FEED_URL = "https://github.com/fkie-cad/nvd-json-data-feeds/releases/latest/download/CVE-{year}.json.xz"
KEV_URL      = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

RSS_FEEDS = [
    ("The Hacker News",   "https://feeds.feedburner.com/TheHackersNews"),
    ("BleepingComputer",  "https://www.bleepingcomputer.com/feed/"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
    ("Dark Reading",      "https://www.darkreading.com/rss.xml"),
    ("Security Affairs",  "https://securityaffairs.com/feed"),
]

CUTOFF = date.today() - timedelta(days=365 * YEARS_BACK)
HTTP   = requests.Session()
HTTP.headers.update({"User-Agent": "CyberQwen-PAI-ingest/1.0"})


@dataclass
class Doc:
    source: str      # stable unique key -> also the citation label in build_rag_context
    doc_date: str    # ISO date, passed to rag.ingest's doc_date
    text: str        # body to embed


# ----------------------------------------------------------------------------
# 1) CVEs — fkie-cad reconstructed NVD yearly feeds  {"cve_items": [ <api-2.0 obj>, ... ]}
# ----------------------------------------------------------------------------
def _cvss(metrics: dict):
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        arr = metrics.get(key) or []
        if arr:
            m = arr[0]
            data = m.get("cvssData", {})
            return data.get("baseScore"), (data.get("baseSeverity") or m.get("baseSeverity") or "UNKNOWN")
    return None, "UNKNOWN"


def _cve_to_doc(cve: dict, min_cvss: float):
    published = (cve.get("published") or "")[:10]
    if not published or published < CUTOFF.isoformat():
        return None
    score, sev = _cvss(cve.get("metrics", {}))
    if score is not None and score < min_cvss:
        return None
    if score is None and min_cvss > 0:
        return None

    cid = cve.get("id", "CVE-UNKNOWN")
    desc = next((d["value"] for d in cve.get("descriptions", []) if d.get("lang") == "en"), "")
    cwes = sorted({d["value"]
                   for w in cve.get("weaknesses", [])
                   for d in w.get("description", [])
                   if d.get("value", "").startswith("CWE")})
    refs = [r.get("url") for r in cve.get("references", []) if r.get("url")][:6]

    text = (
        f"{cid} — {sev}" + (f" (CVSS {score})" if score is not None else "") + "\n"
        f"Published: {published}\n"
        + (f"CWE: {', '.join(cwes)}\n" if cwes else "")
        + f"URL: https://nvd.nist.gov/vuln/detail/{cid}\n\n"
        + desc
        + ("\n\nReferences:\n" + "\n".join(refs) if refs else "")
    )
    return Doc(source=cid, doc_date=published, text=text)


def iter_cve_docs(years: int, min_cvss: float):
    this_year = date.today().year
    for year in range(this_year - years + 1, this_year + 1):
        url = CVE_FEED_URL.format(year=year)
        print(f"[cve] downloading {year} feed ...", flush=True)
        try:
            with tempfile.NamedTemporaryFile(suffix=".xz", delete=False) as tmp:
                with HTTP.get(url, stream=True, timeout=180) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(1 << 16):
                        tmp.write(chunk)
                tmp_path = tmp.name
        except Exception as e:
            print(f"[cve] {year} download failed: {e}", file=sys.stderr)
            continue
        try:
            with lzma.open(tmp_path) as fh:
                data = json.load(fh)           # one year in RAM; use ijson if low on memory
            items = data.get("cve_items", data if isinstance(data, list) else [])
            kept = 0
            for cve in items:
                d = _cve_to_doc(cve, min_cvss)
                if d:
                    kept += 1
                    yield d
            print(f"[cve] {year}: kept {kept}/{len(items)}", flush=True)
        finally:
            os.unlink(tmp_path)


# ----------------------------------------------------------------------------
# 2) CISA KEV
# ----------------------------------------------------------------------------
def iter_kev_docs():
    print("[kev] downloading CISA KEV catalog ...", flush=True)
    try:
        data = HTTP.get(KEV_URL, timeout=60).json()
    except Exception as e:
        print(f"[kev] failed: {e}", file=sys.stderr)
        return
    for v in data.get("vulnerabilities", []):
        cid = v.get("cveID", "CVE-UNKNOWN")
        text = (
            f"{cid} — KNOWN EXPLOITED (CISA KEV)\n"
            f"Name: {v.get('vulnerabilityName','')}\n"
            f"Vendor/Product: {v.get('vendorProject','')} / {v.get('product','')}\n"
            f"Date added: {v.get('dateAdded','')} | Due: {v.get('dueDate','')} | "
            f"Ransomware: {v.get('knownRansomwareCampaignUse','Unknown')}\n\n"
            f"{v.get('shortDescription','')}\n\n"
            f"Required action: {v.get('requiredAction','')}"
        )
        yield Doc(source=f"KEV:{cid}", doc_date=v.get("dateAdded", ""), text=text)


# ----------------------------------------------------------------------------
# 3) News RSS
# ----------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html or "")).strip()

def iter_news_docs(feeds):
    if feedparser is None:
        print("[news] feedparser not installed — `pip install feedparser`", file=sys.stderr)
        return
    for site, url in feeds:
        print(f"[news] {site} ...", flush=True)
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[news] {site} failed: {e}", file=sys.stderr)
            continue
        for e in feed.entries:
            pub = time.strftime("%Y-%m-%d", e.published_parsed) if getattr(e, "published_parsed", None) else ""
            if pub and pub < CUTOFF.isoformat():
                continue
            title = _clean(getattr(e, "title", ""))
            link  = getattr(e, "link", "")
            if not (title and link):
                continue
            summary = _clean(getattr(e, "summary", "") or
                             (e.content[0].value if getattr(e, "content", None) else ""))
            yield Doc(source=link, doc_date=pub,
                      text=f"{title}\nSource: {site} | {pub}\n\n{summary}")


# ----------------------------------------------------------------------------
# Ingest via rag.py (dedup by `source` first, so re-runs only add new docs)
# ----------------------------------------------------------------------------
def run(docs):
    st = rag.store()
    con = st.conn
    added = skipped = chunks = 0
    for d in docs:
        if con.execute("SELECT 1 FROM chunks WHERE source=? LIMIT 1", (d.source,)).fetchone():
            skipped += 1
            continue
        try:
            n = st.ingest(d.text, source=d.source, doc_date=d.doc_date)
        except Exception as e:
            print(f"  ingest fail [{d.source}]: {e}", file=sys.stderr)
            continue
        chunks += n
        added += 1
        if added % 100 == 0:
            print(f"  ... {added} docs / {chunks} chunks embedded", flush=True)
    print(f"\nDONE  new_docs={added}  chunks_added={chunks}  skipped_existing={skipped}")
    print("Store stats:", st.stats())


def main():
    ap = argparse.ArgumentParser(description="Ingest cybersecurity sources into CyberQwen's RAG store.")
    ap.add_argument("--all", action="store_true", help="CVE + KEV + news")
    ap.add_argument("--cve", action="store_true")
    ap.add_argument("--kev", action="store_true")
    ap.add_argument("--news", action="store_true")
    ap.add_argument("--years", type=int, default=YEARS_BACK)
    ap.add_argument("--min-cvss", type=float, default=CVE_MIN_CVSS)
    args = ap.parse_args()

    do_cve, do_kev, do_news = (args.cve or args.all), (args.kev or args.all), (args.news or args.all)
    if not (do_cve or do_kev or do_news):
        ap.error("pick a source: --all  or  --cve / --kev / --news")

    print(f"Cutoff: {CUTOFF.isoformat()} (last {args.years}y) | embed model: {rag.EMBED_MODEL}")
    print(f"RAG DB: {rag.DB_PATH}\n")

    def stream():
        if do_kev:
            yield from iter_kev_docs()
        if do_cve:
            yield from iter_cve_docs(args.years, args.min_cvss)
        if do_news:
            yield from iter_news_docs(RSS_FEEDS)

    run(stream())


if __name__ == "__main__":
    main()
