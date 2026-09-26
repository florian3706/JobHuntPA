> [!WARNING]
> This was vibe-coded in its entirety, as a way to get experience with LLM-based
> development (and save my sanity while job hunting).
> 
> I'm making this public so I can more easily share this with friends who are also job hunting.
> This is provided as is with no warranty. Do whatever you want with this, but don't come crying to
> me if it doesn't work. It's a vibe-coded throwaway tool --- nothing more.

# JobHuntPA

Personal job search dashboard. It collects jobs from SEEK and company careers pages, filters them
against your search setup and location pins, and scores the rest against your uploaded documents
with an LLM of your choice (any OpenAI-compatible API).

## Run

```
pip install -r requirements.txt
playwright install chromium        # optional, for JavaScript-only careers pages
cp .env.example .env               # set LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Tests: `python3 -m unittest discover -s tests -t .`

## How a search run works

`Run search` starts a background run (`backend/tasks.py`); the page polls its progress.

1. **Listing** (`backend/adapters/`): SEEK search pages, then each enabled company source. A
   source URL is resolved in order: a known ATS board (Workable, Greenhouse, Lever, Ashby,
   SmartRecruiters, Workday) in the URL or linked from the page; schema.org `JobPosting` data;
   HTML job links with pagination; headless-browser rendering for JavaScript-only pages.
2. **Caching** (`backend/pipeline.py`): jobs are upserted by URL. A job stored with a full
   description is never fetched again. Listing pages are cached 30-60 minutes in `http_cache`.
   Detail pages are fetched only for new jobs that pass the cheap filters, at most 60 per source
   per run (the rest follow on the next run).
3. **Filtering** (`backend/filters.py`): dealbreakers, titles/keywords, salary floor, work mode,
   location pins. Excluded jobs stay in the DB with a reason ("Show excluded").
4. **Scoring** (`backend/scorer.py`): new jobs that passed the filters are scored. Failures are
   stored as errors (not as 0) and retried next time. A rejected API key stops the batch after
   one request.

## Scraping rules

- Every request goes through `backend/scraping/http.py`: robots.txt is checked for every URL
  (wildcards, longest match), `Crawl-delay` is honoured (at least 2 s per host), and the
  User-Agent identifies the app honestly. There is no browser impersonation, and blocks and
  CAPTCHAs are never worked around.
- SEEK's robots.txt allows search result pages but not job pages, so SEEK jobs carry the listing
  summary only. SEEK may also refuse automated access outright (HTTP 403). In both cases, save
  SEEK pages from your own browser and use *Search setup → Import saved pages*.
- Geocoding uses OpenStreetMap Nominatim under its usage policy (≤1 request/s, cached forever).
