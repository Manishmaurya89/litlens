#!/usr/bin/env python3
"""Paper search and full-text retrieval for the LitLens skill. Standard library only.

  search  Query arXiv, Crossref, OpenAlex and Semantic Scholar, merge and dedupe the
          results, fill in citation counts and abstracts by ID, and add the papers
          the candidates keep citing.
  fetch   Download the full text of candidates from an earlier search.

OpenAlex and Semantic Scholar throttle anonymous searches heavily. Setting
OPENALEX_API_KEY and S2_API_KEY (both optional) avoids that.
"""
import argparse
import collections
import concurrent.futures
import datetime
import html
import html.parser
import http.client
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER_AGENT = "LitLens/1.0 (+https://github.com/Manishmaurya89/litlens)"
SOURCE_BUDGET_S = 75  # longest one source may spend waiting out rate limits
MAX_DOWNLOAD = 60 * 1024 * 1024
MIN_FULLTEXT_WORDS = 1500  # anything shorter is a landing page or abstract, not a paper
COCITED_TOP = 5  # foundational papers each lookup may add from the candidates' reference lists

S2_API = "https://api.semanticscholar.org/graph/v1/paper"
S2_FIELDS = "title,abstract,year,venue,citationCount,influentialCitationCount,externalIds,openAccessPdf,authors"
S2_REFERENCE_FIELDS = ",".join(f"references.{field}" for field in ["paperId", *S2_FIELDS.split(",")])
OPENALEX_API = "https://api.openalex.org/works"
OPENALEX_FIELDS = (
    "id,doi,title,publication_year,cited_by_count,authorships,primary_location,"
    "locations,best_oa_location,abstract_inverted_index,referenced_works"
)
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
CROSSREF_API = "https://api.crossref.org/works"
CROSSREF_FIELDS = "DOI,title,author,published,container-title,is-referenced-by-count,abstract"

PREPRINT = re.compile(r"arxiv|\bcorr\b|biorxiv|medrxiv|chemrxiv|techrxiv|ssrn|research square|preprint|figshare|zenodo", re.I)
STOPWORDS = {"a", "an", "and", "are", "by", "for", "from", "how", "in", "is", "of", "on", "or", "the", "to", "using", "via", "vs", "what", "with"}
OUTLINE = re.compile(r"#{1,4} |#+ abstract|table\s+[\dIVX]+\b", re.I)  # deeper headings are theorems and proofs


class Unavailable(Exception):
    """A source refused the request or could not be reached after retries."""


def retry_window(err, body):
    hint = err.headers.get("Retry-After") or ""
    if hint.isdigit():
        return int(hint)
    match = re.search(rb'"retryAfter":\s*(\d+)', body)
    return int(match.group(1)) if match else None


def request(url, data=None, headers=None, tries=4, deadline=None):
    """Return (body, content type), retrying throttling and transient failures within the deadline."""
    delay = 3
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read(MAX_DOWNLOAD + 1)
                if len(body) > MAX_DOWNLOAD:
                    raise Unavailable("response too large")
                return body, resp.headers.get_content_type()
        except urllib.error.HTTPError as err:
            if err.code != 429 and err.code < 500:
                raise Unavailable(f"HTTP {err.code}") from None
            reason = "rate-limited" if err.code == 429 else f"HTTP {err.code}"
            wait = retry_window(err, err.read()) or delay
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as err:
            reason, wait = f"network error ({type(err).__name__})", delay
        delay *= 2
        if attempt == tries - 1 or (deadline is not None and time.monotonic() + wait > deadline):
            raise Unavailable(reason)
        time.sleep(wait + random.random())


def make_record(source, title, *, doi=None, arxiv=None, abstract=None, **fields):
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi or "", flags=re.I).strip().lower() or None
    if doi and doi.startswith("10.48550/arxiv."):  # an arXiv DOI is the arXiv ID, not a version of record
        arxiv, doi = arxiv or doi.removeprefix("10.48550/arxiv."), None
    return {
        "title": " ".join((title or "").split()),
        "authors": [],
        "year": None,
        "venue": "",
        "citations": None,
        "influential": None,
        "cocited": None,
        "pdfs": [],
        **fields,
        "doi": doi,
        "arxiv": re.sub(r"v\d+$", "", arxiv) if arxiv else None,
        "abstract": " ".join(html.unescape(abstract or "").split()),
        "sources": [source],
        "vectors": [],
        "rank": 999,
    }


def from_s2(paper):
    ids = paper.get("externalIds") or {}
    return make_record(
        "semantic_scholar", paper.get("title"),
        authors=[a["name"] for a in paper.get("authors") or [] if a.get("name")],
        year=paper.get("year"), venue=paper.get("venue") or "",
        citations=paper.get("citationCount"), influential=paper.get("influentialCitationCount"),
        pdfs=[url] if (url := (paper.get("openAccessPdf") or {}).get("url")) else [],
        doi=ids.get("DOI"), arxiv=ids.get("ArXiv"), abstract=paper.get("abstract"),
    )


def from_openalex(work):
    locations = [work.get("primary_location") or {}, *(work.get("locations") or [])]
    hosts = [loc.get("source") or {} for loc in locations]
    venue = next(
        (h["display_name"] for h in hosts
         if h.get("type") in ("journal", "conference") and h.get("display_name") and not PREPRINT.search(h["display_name"])),
        hosts[0].get("display_name") or "",
    )
    arxiv = next(
        (m.group(1) for loc in locations
         if (m := re.search(r"arxiv\.org/(?:abs|pdf)/(.+?)(?:\.pdf)?$", loc.get("landing_page_url") or ""))),
        None,
    )
    inverted = work.get("abstract_inverted_index") or {}
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return make_record(
        "openalex", work.get("title"),
        authors=[name for a in work.get("authorships") or [] if (name := (a.get("author") or {}).get("display_name"))],
        year=work.get("publication_year"), venue=venue, citations=work.get("cited_by_count"),
        pdfs=list(dict.fromkeys(  # best first; publishers often refuse scripts where a repository copy doesn't
            url for loc in [work.get("best_oa_location") or {}, *locations] if (url := loc.get("pdf_url"))
        )),
        doi=work.get("doi"), arxiv=arxiv, abstract=" ".join(word for _, word in words),
    )


def from_crossref(item):
    date = (item.get("published") or {}).get("date-parts") or [[None]]
    return make_record(
        "crossref", (item.get("title") or [""])[0],
        authors=[" ".join(filter(None, (a.get("given"), a.get("family")))) for a in item.get("author") or []],
        year=date[0][0], venue=(item.get("container-title") or [""])[0],
        citations=item.get("is-referenced-by-count"),
        doi=item.get("DOI"), abstract=re.sub(r"<[^>]+>", " ", item.get("abstract") or ""),  # JATS XML
    )


def from_arxiv(entry):
    def text(tag):
        return entry.findtext(tag, default="", namespaces=ATOM).strip()

    arxiv_id = re.search(r"/abs/(.+)$", text("a:id"))
    if not arxiv_id:  # the API reports query errors as an entry with no paper ID
        return None
    return make_record(
        "arxiv", text("a:title"),
        authors=[a.findtext("a:name", default="", namespaces=ATOM) for a in entry.findall("a:author", ATOM)],
        year=int(text("a:published")[:4] or 0) or None, venue=text("x:journal_ref") or "arXiv",
        doi=text("x:doi") or None, arxiv=arxiv_id.group(1), abstract=text("a:summary"),
    )


def parse_feed(body):
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:  # arXiv's feed declares neither, so refuse rather than expand them
        raise ValueError("feed declares a DTD")
    return ET.fromstring(body)


def search_arxiv(query, limit, since, deadline):
    phrases = re.findall(r'"([^"]+)"', query)
    words = re.findall(r"[\w-]+", re.sub(r'"[^"]+"', " ", query))
    terms = [f'all:"{t}"' for t in phrases + [w for w in words if w.lower() not in STOPWORDS]]
    if not terms:
        return []
    dates = f" AND submittedDate:[{since}01010000 TO 299912312359]" if since else ""
    for joiner in (" AND ", " OR "):  # AND is precise but finds nothing for over-specific queries
        params = {"search_query": f"({joiner.join(terms)}){dates}", "max_results": limit, "sortBy": "relevance"}
        body, _ = request(f"{ARXIV_API}?{urllib.parse.urlencode(params)}", deadline=deadline)
        records = [r for r in map(from_arxiv, parse_feed(body).findall("a:entry", ATOM)) if r]
        if records:
            return records
        time.sleep(3)
    return []


def search_crossref(query, limit, since, deadline):
    filters = "type:journal-article,type:proceedings-article" + (f",from-pub-date:{since}" if since else "")
    params = {"query.bibliographic": query, "rows": limit, "filter": filters, "select": CROSSREF_FIELDS}
    body, _ = request(f"{CROSSREF_API}?{urllib.parse.urlencode(params)}", deadline=deadline)
    return [from_crossref(item) for item in json.loads(body)["message"]["items"]]


def openalex_works(params, deadline=None):
    key = os.environ.get("OPENALEX_API_KEY")
    query = {**params, "select": OPENALEX_FIELDS, **({"api_key": key} if key else {})}
    body, _ = request(f"{OPENALEX_API}?{urllib.parse.urlencode(query)}", deadline=deadline)
    return json.loads(body)["results"]


def search_openalex(query, limit, since, deadline):
    params = {"search": query, "per-page": limit}
    if since:
        params["filter"] = f"from_publication_date:{since}-01-01"
    return [from_openalex(work) for work in openalex_works(params, deadline)]


def s2_headers():
    key = os.environ.get("S2_API_KEY")
    return {"x-api-key": key} if key else {}


def search_s2(query, limit, since, deadline):
    params = {"query": query, "limit": limit, "fields": S2_FIELDS}
    if since:
        params["year"] = f"{since}-"
    # The anonymous pool rarely clears within a few retries, and the batch lookup later covers citations anyway.
    tries = 4 if s2_headers() else 2
    body, _ = request(f"{S2_API}/search?{urllib.parse.urlencode(params)}", headers=s2_headers(), tries=tries, deadline=deadline)
    return [from_s2(paper) for paper in json.loads(body).get("data") or []]


SOURCES = {  # name: (search function, seconds between queries)
    "arxiv": (search_arxiv, 3),
    "crossref": (search_crossref, 1),
    "openalex": (search_openalex, 1),
    "semantic_scholar": (search_s2, 1.1),
}


def failure(err):
    return str(err) if isinstance(err, Unavailable) else f"unreadable response ({type(err).__name__})"


def sweep(name, vectors, limit, since):
    """Run every vector against one source, stopping at the first refusal so a throttled source isn't hammered."""
    search, pause = SOURCES[name]
    deadline = time.monotonic() + SOURCE_BUDGET_S
    records = []
    for done, (vector, query) in enumerate(vectors):
        if done:
            time.sleep(pause)
        try:
            found = search(query, limit, since, deadline)
        except Exception as err:  # external data of unknown shape: report it and keep the other sources
            return records, f"{name} {len(records)} ({failure(err)} after {done}/{len(vectors)} queries)"
        for rank, rec in enumerate(found):
            rec.update(vectors=[vector], rank=rank)
        records += found
    return records, f"{name} {len(records)}"


def is_preprint(venue):
    return not venue or bool(PREPRINT.search(venue))


def absorb(into, rec):
    """Fold another record of the same paper into `into`."""
    # OpenAlex folds third-party uploads into its works and can inherit their metadata, so any other source wins.
    replace = into["sources"] == ["openalex"] and rec["sources"] != ["openalex"]
    for field in ("title", "authors", "abstract"):
        into[field] = (rec[field] or into[field]) if replace else (into[field] or rec[field])
    for field in ("doi", "arxiv"):
        into[field] = into[field] or rec[field]
    into["pdfs"] += [url for url in rec["pdfs"] if url not in into["pdfs"]]
    # Earliest year: age counts from first public appearance, so a later journal date can't inflate velocity.
    into["year"] = min(filter(None, (into["year"], rec["year"])), default=None)
    for field in ("citations", "influential", "cocited"):
        into[field] = max((v for v in (into[field], rec[field]) if v is not None), default=None)
    if rec["venue"] and (not into["venue"] or (is_preprint(into["venue"]) and not is_preprint(rec["venue"]))):
        into["venue"] = rec["venue"]
    into["sources"] = sorted({*into["sources"], *rec["sources"]})
    into["vectors"] = sorted({*into["vectors"], *rec["vectors"]})
    into["rank"] = min(into["rank"], rec["rank"])


def identity_keys(rec):
    keys = []
    if rec["doi"]:
        keys.append(f"doi:{rec['doi']}")
    if rec["arxiv"]:
        keys.append(f"arxiv:{rec['arxiv']}")
    title = re.sub(r"[^a-z0-9]", "", rec["title"].lower())
    if len(title) >= 20:  # short titles collide between unrelated papers
        keys.append(f"title:{title}")
    return keys


def merge(records):
    merged, index = [], {}
    for rec in records:
        keys = identity_keys(rec)
        target = next((index[k] for k in keys if k in index), None)
        if target is None:
            target = rec
            merged.append(rec)
        else:
            absorb(target, rec)
        for key in identity_keys(target):
            index[key] = target
    return merged


def lookup_s2(candidates):
    """Fill in metadata by ID. Return (how many candidates cite each paper, those papers), keyed by paper ID."""
    identified = [c for c in candidates if c["arxiv"] or c["doi"]]
    counts, papers = collections.Counter(), {}
    if not identified:
        return counts, papers
    ids = [f"ARXIV:{c['arxiv']}" if c["arxiv"] else f"DOI:{c['doi']}" for c in identified]
    url = f"{S2_API}/batch?{urllib.parse.urlencode({'fields': f'{S2_FIELDS},{S2_REFERENCE_FIELDS}'})}"
    headers = {"Content-Type": "application/json", **s2_headers()}
    body, _ = request(url, data=json.dumps({"ids": ids}).encode(), headers=headers, tries=5, deadline=time.monotonic() + SOURCE_BUDGET_S)
    for candidate, paper in zip(identified, json.loads(body)):
        if not paper:
            continue
        absorb(candidate, from_s2(paper))
        for ref in paper.get("references") or []:
            if ref and ref.get("paperId") and ref.get("title"):
                counts[ref["paperId"]] += 1
                papers.setdefault(ref["paperId"], from_s2(ref))
    return counts, papers


def lookup_openalex(candidates):
    """Same contract as lookup_s2. Holds reference lists for different papers, and rarely throttles lookups by ID."""
    by_doi = {}
    for c in candidates:
        if c["doi"] and not re.search(r"[,|]", c["doi"]):  # those characters are separators in OpenAlex filters
            by_doi[c["doi"]] = c
        if c["arxiv"]:
            by_doi[f"10.48550/arxiv.{c['arxiv']}"] = c
    dois = list(by_doi)
    deadline = time.monotonic() + SOURCE_BUDGET_S
    references = collections.defaultdict(set)  # by candidate, since preprint and published records overlap
    for start in range(0, len(dois), 50):
        filters = "doi:" + "|".join(dois[start:start + 50])
        for work in openalex_works({"filter": filters, "per-page": 50}, deadline):
            candidate = by_doi.get(re.sub(r"^https?://doi\.org/", "", work.get("doi") or "").lower())
            if candidate:
                absorb(candidate, from_openalex(work))
                references[id(candidate)].update(work.get("referenced_works") or [])
    counts = collections.Counter(work_id for works in references.values() for work_id in works)
    top = [work_id.rsplit("/", 1)[-1] for work_id, n in counts.most_common(COCITED_TOP * 4) if n >= 2]
    papers = {}
    if top:  # reference lists only hold IDs, so resolve the most-cited ones
        for work in openalex_works({"filter": "openalex:" + "|".join(top), "per-page": len(top)}, deadline):
            papers[work["id"]] = from_openalex(work)
    return counts, papers


def foundational_additions(counts, papers, candidates):
    """Record how often candidates cite each other, and return the outside papers they keep citing.

    These are a field's foundations, which keyword search tends to miss.
    """
    by_key = {key: c for c in candidates for key in identity_keys(c)}
    outsiders = []
    for paper_id, rec in papers.items():
        rec["cocited"] = counts[paper_id]
        match = next((by_key[key] for key in identity_keys(rec) if key in by_key), None)
        if match:
            match["cocited"] = max(match["cocited"] or 0, counts[paper_id])
        elif counts[paper_id] >= 2:
            outsiders.append(rec)
    # Discount ubiquitous papers so they can't crowd out the field's own foundations.
    outsiders.sort(key=lambda rec: rec["cocited"] / math.log10((rec["citations"] or 0) + 10), reverse=True)
    return outsiders[:COCITED_TOP]


def velocity(c):
    if c["citations"] is None or not c["year"]:
        return None
    age = max(1.0, datetime.date.today().year - c["year"] + 0.5)
    return round(c["citations"] / age, 1)


def describe(c):
    cites = "?" if c["citations"] is None else str(c["citations"])
    if c["influential"]:
        cites += f" ({c['influential']} influential)"
    rate = velocity(c)
    links = []
    if c["doi"]:
        links.append(f"https://doi.org/{c['doi']}")
    if c["arxiv"]:
        links.append(f"https://arxiv.org/abs/{c['arxiv']}")
    access = "arXiv" if c["arxiv"] else "open-access PDF" if c["pdfs"] else "none found"
    authors = ", ".join(c["authors"][:3]) + (" et al." if len(c["authors"]) > 3 else "")
    found = list(c["vectors"]) + ([f"cited by {c['cocited']} other candidates"] if c["cocited"] else [])
    venue = "venue unknown" if not c["venue"] else "PREPRINT" if is_preprint(c["venue"]) else c["venue"]
    return "\n".join([
        f"## [{c['n']}] {c['title']}",
        f"- {authors or 'authors unknown'} | first public {c['year'] or 'unknown'} | {venue}",
        f"- citations: {cites} | velocity: {'?' if rate is None else rate}/yr | full text: {access}",
        f"- found by: {', '.join(found)} | metadata from: {', '.join(c['sources'])}",
        f"- links: {' '.join(links) or 'none'}",
        "",
        c["abstract"] or "(no abstract from any source)",
        "",
    ])


def cmd_search(args):
    run = Path(args.run) if args.run else Path(tempfile.mkdtemp(prefix="litlens-"))
    store = run / "candidates.json"
    if args.run and not store.exists():
        sys.exit(f"No search results in {run}. Omit --run to start a new search.")
    known = json.loads(store.read_text(encoding="utf-8")) if args.run else []

    with concurrent.futures.ThreadPoolExecutor(len(SOURCES)) as pool:
        sweeps = list(pool.map(lambda name: sweep(name, args.vector, args.limit, args.since), SOURCES))
    candidates = merge(known + sorted(
        (rec for records, _ in sweeps for rec in records),
        key=lambda rec: (rec["rank"], -(rec["citations"] or 0)),
    ))

    lookups, cocitation_ok = [], False
    # OpenAlex first: Semantic Scholar's higher counts then also reach the papers OpenAlex added.
    for name, lookup in (("openalex", lookup_openalex), ("semantic_scholar", lookup_s2)):
        try:
            counts, papers = lookup(candidates)
        except Exception as err:
            lookups.append(f"{name} failed ({failure(err)})")
            continue
        lookups.append(f"{name} ok")
        cocitation_ok = True
        additions = foundational_additions(counts, papers, candidates)
        # Only a new search adds papers: repeating this when extending a run would pull in ever more generic references.
        # merge also collapses records that a lookup just revealed to be one paper.
        candidates = merge(candidates + ([] if args.run else additions))
    added = sum(not c["vectors"] and "n" not in c for c in candidates)

    next_n = max((c.get("n", 0) for c in candidates), default=0) + 1
    new = []
    for c in candidates:
        if "n" not in c:
            c["n"] = next_n
            next_n += 1
            new.append(c["n"])
    candidates.sort(key=lambda c: c["n"])
    store.write_text(json.dumps(candidates, indent=1), encoding="utf-8")
    listing = run / "candidates.md"
    entries = [describe(c) for c in candidates]
    listing.write_text("\n".join(entries), encoding="utf-8")

    print(f"Run: {run}")
    print("Searches: " + " | ".join(status for _, status in sweeps))
    print("Lookups by ID: " + " | ".join(lookups))
    print(f"Co-citation: {f'added {added} foundational papers' if cocitation_ok else 'UNAVAILABLE, both ID lookups failed'}")
    print(
        f"Candidates: {len(candidates)} total, {len(new)} new"
        f" ({sum(c['citations'] is None for c in candidates)} without citation data,"
        f" {sum(not c['abstract'] for c in candidates)} without abstract)"
    )
    if not candidates:
        sys.exit("No candidates: every source failed or found nothing.")
    if new:
        before = next(i for i, c in enumerate(candidates) if c["n"] == new[0])
        line = sum(entry.count("\n") + 1 for entry in entries[:before]) + 1
        print(f"Read {listing} from line {line} (new entries start at [{new[0]}])")
    if args.run:  # so a title lookup can be checked against what actually came back
        for c in candidates:
            if c["n"] in new:
                print(f"  [{c['n']}] {c['title']} ({c['year'] or 'year unknown'})")


class ReadableText(html.parser.HTMLParser):
    """Flatten an article page to text, keeping headings and table rows but not page furniture or the bibliography."""

    SKIPPED = {"script", "style", "nav", "header", "footer", "noscript", "button", "svg", "form"}
    BREAKS = {"p", "div", "section", "article", "li", "tr", "table", "figure", "figcaption", "blockquote", "pre", "br", "dt", "dd"}
    HEADINGS = {f"h{level}": level for level in range(1, 7)}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_tag = None
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            self.skip_depth += tag == self.skip_tag
            return
        attrs = dict(attrs)
        if tag == "math":  # the alt text is the LaTeX source; the MathML inside reads as noise
            self.parts.append(f" {attrs.get('alttext') or ''} ")
        if tag == "math" or tag in self.SKIPPED or "ltx_bibliography" in (attrs.get("class") or ""):
            self.skip_tag, self.skip_depth = tag, 1
        elif tag in self.HEADINGS:
            self.parts.append("\n" + "#" * self.HEADINGS[tag] + " ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if not self.skip_depth:
                    self.skip_tag = None
        elif tag in self.HEADINGS or tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_tag:  # source line breaks mean nothing in HTML; only block tags end a line
            self.parts.append(re.sub(r"\s+", " ", data))

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def full_text_urls(c):
    if c["arxiv"]:
        yield "arXiv HTML", f"https://arxiv.org/html/{c['arxiv']}"
        yield "ar5iv HTML", f"https://ar5iv.labs.arxiv.org/html/{c['arxiv']}"
        yield "arXiv PDF", f"https://arxiv.org/pdf/{c['arxiv']}"
    for url in c["pdfs"]:
        if url.startswith(("http://", "https://")):  # urllib would otherwise open file:// URLs from metadata
            yield "open-access copy", re.sub(r"^http://", "https://", url)


def save_full_text(c, folder):
    """Return (origin, path) for the first readable copy, or (None, None) if there is none."""
    for origin, url in full_text_urls(c):
        try:
            body, content_type = request(url, tries=2)
        except Unavailable:
            continue
        if body.startswith(b"%PDF"):
            path = folder / "paper.pdf"
            path.write_bytes(body)
            return origin, path
        if "html" in content_type:
            parser = ReadableText()
            parser.feed(body.decode("utf-8", "replace"))
            text = parser.text()
            if len(text.split()) >= MIN_FULLTEXT_WORDS:
                path = folder / "fulltext.txt"
                path.write_text(text, encoding="utf-8")
                return origin, path
    return None, None


def cmd_fetch(args):
    run = Path(args.run)
    try:
        candidates = {c["n"]: c for c in json.loads((run / "candidates.json").read_text(encoding="utf-8"))}
    except FileNotFoundError:
        sys.exit(f"No search results in {run}. Run search first.")
    for n in args.numbers:
        c = candidates.get(n)
        if c is None:
            print(f"[{n}] no such candidate\n")
            continue
        print(f"[{n}] {c['title']}")
        folder = run / "papers" / str(n)
        folder.mkdir(parents=True, exist_ok=True)
        origin, path = save_full_text(c, folder)
        if path is None:
            print("    NO FULL TEXT: no open-access copy could be retrieved\n")
        elif path.suffix == ".pdf":
            print(f"    {path} ({path.stat().st_size // 1024} KB, from {origin}; read with the pages parameter)\n")
        else:
            text = path.read_text(encoding="utf-8")
            print(f"    {path} ({len(text.split())} words, from {origin})")
            for number, line in enumerate(text.splitlines(), 1):
                if OUTLINE.match(line):
                    print(f"    L{number}: {line[:100]}")
            print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="find candidates and write them to a run directory")
    search.add_argument("--vector", nargs=2, action="append", required=True, metavar=("NAME", "QUERY"), help="repeat for each search vector")
    search.add_argument("--limit", type=int, default=3, help="results per query per source (default: %(default)s)")
    search.add_argument("--since", type=int, metavar="YEAR", help="only papers published from this year on")
    search.add_argument("--run", help="add to the candidates of an earlier run instead of starting a new one")
    search.set_defaults(func=cmd_search)

    fetch = commands.add_parser("fetch", help="download the full text of candidates")
    fetch.add_argument("--run", required=True, help="run directory printed by search")
    fetch.add_argument("numbers", type=int, nargs="+", metavar="N", help="candidate numbers")
    fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
