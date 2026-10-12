# Ghost-job signs (backend/ghost.py)

**What the user asked for (2026-10-08):** "tell me if the job post is suspected to be a ghost job."

**Added 2026-10-10:** a ghost emoji and a rating for each job, under the score, coloured from red (likely a ghost job)
through yellow (unsure) to green (not); "a section on the job page that goes into detail of why this rating was given";
and 🎯 for the fit score. The first version showed a percentage worked out from the weights. The user then asked:
"Instead of assigning a percentage, use coloured circle emoji and give hover info." So there is no percentage, in the
pages or in the JSON: the level is the rating. See [The circle](#the-circle) and [The job page](#the-job-page).

A ghost job is an ad that isn't a real, current vacancy: an evergreen "talent pool" ad, a role posted again and again,
an ad left open for months, or one placed to collect resumes. The app can't know, so it reports **signs**, each with a
plain reason, and a **level**: `likely` (strong evidence), `possible`, or none. A sign never hides, excludes, scores or
down-ranks a job. The words describe what was seen ("Open for 74 days"), never accuse an employer, and the job page
says it may be a real job and suggests what to ask.

## Signals

A signal is `{code, weight, text, detail?}`. Weights are internal (not in the JSON); the JSON lists signals strongest
first. "Now" is UTC.

| code | fires when | weight | text |
|---|---|---|---|
| `long_open` | the posting is not closed, a listing showed it in the last 14 days, and it has been listed 45+ days. The start is when the pool first saw it, or its posted date if that is earlier (a posting that closed and came back starts again at the day it came back, and its old posted date is ignored; the posted date of a source that stores the ad's last-edit date is not read, see Known limits) | 45-59 days 1, 60-89 days 3, 90+ days 5 | `Open for 74 days` |
| `reposted` | the same role was listed before and had ended when this ad first appeared: same normalised company and title (two or more title words), or the same description (400+ characters), in the same place (or either has none). Earlier ads that overlap in time are one appearance. A copy the pool found through another source (`postings.source`) is not an earlier appearance: first seen is when the pool crawled it, so the same ad added from a second board later looks like a repost. A posting that closed (`gone` or `unlisted`) and was listed again counts its earlier run as one; one dropped as `stale` (not listed for 45 days, usually because the search changed) does not. Only appearances that ended in the last 180 days | 2 appearances 1, 3 or more 5 | `Posted 3 times since 12 June` |
| `date_refreshed` | the posted date is 21+ days after the pool first saw the ad (renewed or bumped while it stayed listed). Not for Greenhouse and Atlassian, whose date is the last edit | 1 | `Posted date renewed (first listed 29 August)` |
| `evergreen_wording` | the title or description has wording from a curated list (below) | see below, 1 to 5 | `Mentions "talent pool"` |
| `recruiter_no_employer` | the company looks like a recruiter (`offices.looks_like_recruiter`: company research, else its name) and the ad says "our client", "my client", "client of ours" or "confidential/undisclosed client, company, employer, organisation, business". It does not check that no employer is named (an agency ad that says "our client, Acme" matches), so the text only says it may not be | 1 | `Recruiter ad: the employer may not be named` |
| `many_similar` | the same normalised company has 5+ open postings (listed in the last 14 days) with the same normalised title (two or more words), counting this one | 1 | `5 open ads with this title at this company` |
| `company_news_cuts` | the company's research (`company_profiles.controversies_json`, status done) has an item, from the last 12 months, whose title or summary mentions layoffs, redundancies, retrenchments, job cuts, a hiring freeze, headcount reduction or downsizing. An undated item counts if the news research is under 12 months old. The one-line note is never read ("no layoffs found") | 2 | `Recent news about job cuts at this company` |
| `ad_signs` | the scorer's LLM listed phrases for wording in this ad that suggests it isn't a current vacancy (quotes of the ad's own wording that says it is not a current vacancy or collects resumes for future roles) | 1 phrase 2, 2 or more 3 | `AI read of the ad: <first two phrases>` |

**Evergreen wording.** Matching is case-insensitive with word boundaries; a space matches spaces or hyphens.
- Strong phrases: talent community, network or bench; "general application(s)" (not "general application
  development/support/..."); "evergreen role/position/requisition/vacancy/ad/posting"; "not a current vacancy"; "no
  current vacancy"; "not (currently) an active/open/actual/advertised vacancy/role/position"; "not currently hiring
  for a specific ...".
- Ambiguous phrases: talent pool, talent pipeline, "pipeline of candidates/applicants/talent/people/professionals",
  "ongoing recruitment". They are also the duties of a recruiter, HR or sales ad ("build a pipeline of candidates") and
  ordinary wording for pooled teaching and nursing roles. They are skipped when the title (without the phrase) matches
  `recruit|talent|sourc|HR|human resources|sales|business development`. Otherwise: 5 in the title, or in the body with
  an apply cue (apply, submit, register, join, "not hiring for a specific", "not for a specific role") within 80
  characters ("Join our talent pool"); 3 in the body without one; 1 further down a long ad.
- Moderate phrases: "register (your) interest", "registration(s) of interest", "future
  opportunities/vacancies/openings/roles/positions that may arise / become available", "should any arise", "always
  hiring/recruiting", "ongoing hiring", "continuous hiring/recruitment", "year-round hiring", "keep your
  CV/resume/details on file"; and "expression(s) of interest", which gets no weight when the title or text mentions
  acting, higher duties, secondment or a closing date (the usual title of a real acting role in the public service).
- Left out on purpose because real ads use them: "rolling basis", "ongoing role/opportunity" (permanent in the public
  service), "talented people", "pool of ...", "future needs", a bare "always looking" (everyday prose).
- Position matters, because "Not the right role? Join our talent community." is a footer on real ads. Strong phrase:
  5 in the title, in the first 600 characters, or anywhere in an ad under 1,500 characters; 1 further down a long
  ad. Ambiguous: above. Moderate phrase: 2 in the title, the start or a short ad (so it reaches `possible` only with a
  second sign), 1 further down. The strongest hit is the one reported.

## Levels

Total = sum of the weights.
- `likely`: any one signal of weight 5 (long_open at 90+ days, reposted 3+ times, a strong phrase up front, a talent-pool call to apply), or a total of 6+ with at least two signals of weight 2+.
- `possible`: a total of 3+.
- none: otherwise. The JSON still lists the weak signals that fired: the circle's hover text and the job page list them. The card's "why?" list is for a flagged job only.

All five weak signals together total 5 and have no weight-2 signal, so a few weak signs never reach `likely`. A renewed SEEK ad
(posted date moved forward) alone is 1, and at 50 days 2: no flag; at 60 days it is `possible` ("Open for 60 days" 3 +
renewed 1), which is the point where it has been renewed more than once. The LLM's reading alone is at most `possible`, and one phrase alone is not even that. Company news alone (2) never flags; it
only adds to other signs.

## The circle

`ghostMeter(job)` in `frontend/common.js`: 👻 and a coloured circle under the fit score, from the level.

| level | circle | words |
|---|---|---|
| none | 🟢 | No strong signs of a ghost job |
| `possible` | 🟡 | Possible ghost job: some signs |
| `likely` | 🔴 | Possible ghost job: several signs |

- **No number.** The level is the rating: the app puts no percentage on it.
- **Hover text** (`ghostHover`, a `title`): the words, then each sign found, one a line, then "Open the job to see why."
  A job under the flag with a weak sign or two says so ("No strong signs of a ghost job. One weak sign: ..."). A `title`
  is the browser's own tooltip, as on the fit score: nothing to dismiss, nothing that covers the page.
- **Never colour alone.** The circles differ only in colour, and an emoji keeps its colours in every theme (the
  colour-blind and monochrome ones too). So the words are always there: hidden beside the circle for screen readers, in
  the hover text, and in view wherever a job is flagged (the card's "Possible ghost job (some signs): why?" line, the
  same words in the map list's row, the job page's section). A touch screen has no hover: it has those words.

## The job page

`ghost.assess(..., explain=True)` (only `GET /api/jobs/{id}`) adds `"checks"`: all eight kinds of sign, in a fixed order
(`ghost.CHECKS`), each `{code, label, state, note?}`.
- `found`: it is in the signals (the page shows the sign's text, detail and strength).
- `clear`: looked for, not there. `long_open` adds what it saw ("Listed for 12 days.", "The ad is closed."); the
  wording check says when it read only a title or a listing summary.
- `unknown`: it could not be checked, and the note says why: the job didn't come from a search (no posting, so no
  listing history), the pool half hasn't been worked out yet (`ghost_checked_at` is NULL: "at the end of the next
  search"), the posted date can't be read or is an edit date, the AI hasn't read the ad (`ad_ghost_json` NULL), the
  company isn't researched, or no employer is named.

A green circle means less when half the checks could not be made, so the page counts them ("2 checks could not be
made, so the rating rests on less"). Under the checks the page says what the three colours mean, and that the rating
is not proof. A signal on the job page also carries `strength`: `strong` (weight 5+),
`moderate` (2 to 4) or `weak` (1). The weights themselves stay out of the JSON.

**Where I changed the orchestrator's defaults, and why.**
- `possible` needs a total of 3, not 2. With 2, a 45-day ad that was once reposted, or any ad with company layoffs
  in the news, would be flagged, and those are common among real ads. `long_open` therefore weighs 1 at 45 days, 3 at 60, 5 at 90.
- `reposted` ignores earlier ads that were still listed when this one appeared (the same ad on two boards at once, or a
  multi-site ad), requires the same place, and needs a title of two or more words. A retailer with a "Sales Assistant"
  in every suburb is not re-posting. Description matches need 400+ characters (a listing teaser proves nothing).
- Evergreen wording is position-aware (above) so the common "join our talent community" footer does not flag.
- `many_similar` needs 5 open ads with the same normalised title (word order, seniority kept, contract terms and a city appended to the title dropped).

## Storage

Everything the pool can know is derived from public ads, so it is plaintext on the posting (accounts design §14.1).

`postings` (new columns; `_add_missing_columns` adds them, `_ensure_indexes` the index):
- `repost_key` (indexed): normalised `company|title words` (lower case, no legal suffixes, words sorted, contract terms and durations dropped), `""` when it can't be made. Set by `ghost.stamp` whenever the content changes (`pool._changed`).
- `ghost_json`: `{"text": [signals from the ad's wording, set at the same time], "pool": [reposted, many_similar]}`. A text signal for a recruiter carries `"when": "recruiter"`: it counts only when the company is a recruiter, which is read when the job is read.
- `ghost_checked_at`: when the pool half was last worked out.
- `listed_since`: start of the current run of listing. Set when the posting is created; `pool.upsert_scraped` resets it (and clears `ghost_checked_at`) when a closed posting is listed again. This is what lets "closed and reopened" differ from "open for 100 days".
- `ad_ghost_json`: JSON list of the phrases the LLM read in the public ad (NULL: not read; `[]`: read, nothing found). Cleared by `pool._changed` when the ad's description hash changes (a rewrite, or a summary replaced by the full ad); a title-only or source-only change keeps it.
- `reopened_from`: the `close_reason` of the run before the current one, set when a closed posting is listed again. Only `gone` and `unlisted` make `reposted` count the earlier run.

`jobs.ad_ghost_json`: the same, for a workspace that reads its own ad (a privately uploaded full ad, or a private job). **Sealed**
(`SealedText`, under the owner's key). It is not `SealedAlways` on purpose: a column added to a database that is already in strict mode
would be NULL in every existing row and be refused. The cost is that whether a link has an LLM-read private ad shows as NULL or not; the
content is sealed. It is left out of the job list's query (`pool.CARD_UNUSED`) and loaded in one batch by `ghost.assess_jobs`.

The scorer stores it like `employment_type`: `pool.share_ad_fact(job, "ad_ghost_json", ...)`, so the posting gets it when the LLM
read the public ad, and the workspace's own link when it read its own ad (§14.4). Only for a full description (`detail_status == "full"`);
a listing summary is too little to judge, and the prompt says so. One short rule (12) and one JSON field in `SYSTEM_PROMPT`; no extra
LLM call. The rule asks for quotes copied from the ad, says a missing team or start date is not a sign, and that it never changes the score. The phrases are cleaned by `ghost.clean_phrases` (at most 4, 100 characters, no duplicates) and, before they are stored, kept only if at least 80% of their content words are in the ad the model was given: the model also saw the candidate's documents, and the stored phrases are shown to every workspace on the posting. Phrases with an accusing word (scam, fake, fraud, ghost) are dropped always, also when read.

**When it is computed**
- At search time, in `pool._changed` (a posting is new or its content changed): `repost_key` and the text signals. Cost: a few regexes on one description.
- At the end of each search run, after `pool.sweep` (`pipeline.refresh_ghost_signs` -> `ghost.refresh`): the pool half for open postings never worked out or not for a day, oldest first, at most 3,000 per run. Postings from before this feature are filled in first (`_backfill`: key and text half, 500 at a time, closed ones too, because they are history). A failure there is logged by class name and costs only the signs until the next run; it never fails a search.
- When a job is read (`ghost.assess`): `long_open`, `date_refreshed`, `company_news_cuts`, `ad_signs`, and the stored halves.

**Cost of the pool half.** Per 400 distinct keys and per 400 distinct description hashes, one indexed `IN` query returning columns
only (no descriptions; `length(description)` stands in for the text). Then everything is decided in Python. For 5,000 open postings due at once that is about 25 queries. After the first run only
postings new that day, or a day old, are due. History is kept as long as the pool keeps closed postings (180 days, `pool.PURGE_CLOSED_AFTER`), which is also the repost window.

**Cost of the job list** (`tools/bench_jobs_list.py --jobs 5000 --listed 0.2`, this dev machine, median of 9): the default list (1,000 jobs) went from about 0.34 s to 0.36 s,
the full list (5,000) from about 1.34 s to 1.45 s: about 7% for the whole request. `ghost.assess_jobs` adds one query per 500 jobs (the jobs' own description and LLM reading: two sealed values a job, almost all
the NULL marker) and no query per job. `parse_posted` is cached, the empty stored value is not parsed, and a job with nothing to report returns early. A test asserts one statement for 50 jobs.

## What reads it

- **Job JSON** (`app.job_to_dict`): `"ghost": {"level": "likely" | "possible" | null, "signals": [{"code", "text"}]}` on every job in `GET /api/jobs` and `GET /api/jobs/{id}`. The job page also gets `"detail"` (one sentence) and `"strength"` in each signal, and `"checks"` (above).
- **Pages** (`frontend/common.js`): `ratingsHtml(job)` is the fit score (🎯) with `ghostMeter(job)` (👻 and the circle) under it, on job cards, the map list and the job page's header (there a link to the section). A flagged job's card also has the `ghostWhy` disclosure, "Possible ghost job (some signs): why?". `job.js` `renderGhost` draws the job page's section for every job. Merged copies are not listed, as before; a merged copy's posting counts toward `reposted` as any posting does: only when it was listed earlier and had ended, not when it is the same ad on another board at once.
- **Digest** (`digest.cards` -> `email_templates.digest_card`): the first reason of a flagged job as one line, `Possible ghost job: Open for 74 days` (both levels use "Possible"), in the text and HTML parts.
- **Export** (`backend/export.py`): a flagged job's file gets `## Is this a real vacancy?` (the signs, one line each with its detail, and a sentence saying it may be real and what to ask), and `Jobs.md` gets a column `Ghost job?` (`Likely` / `Possible` / empty).

## Privacy and the rules in CLAUDE.md

- Only public ads feed the pool half; a private job (no posting) and a workspace's private upload never enter it. Another workspace's private jobs are never counted (they are not postings). The same-role lookup does show a user "Posted 3 times since 12 June" derived from pool postings other workspaces' searches found: the same public information the pool already shares (§14.2 pool matching).
- No SQL filters, sorts, groups or DISTINCTs on a sealed column. The only `jobs` columns `ghost.py` names are `id` (the filter), `_description` and `ad_ghost_json` (selected). A test records every statement of a search, a list and a detail request and checks the WHERE / ORDER BY / GROUP BY / DISTINCT clauses.
- Scraping stays in `backend/scraping/http.py` (nothing here fetches). LLM reading stays in `scorer.score_one` through `llm_providers.chat`. Nothing here lists, scores or researches jobs, so the `duplicate_of` rule is kept by the callers: `list_jobs` and the digest already skip merged copies, and the export gives them no file.
- No new dependency, setting, folder, port or entry point, so the setup, launch and uninstall scripts and the installer are unchanged.

## Known limits

- It reads the signs the pool has seen. A new install has no history, so `reposted` and `long_open` grow with use; `long_open` also uses a parseable posted date at once.
- A role that is genuinely hard to fill, or a high-turnover one at one place, can show `reposted`. The text says so.
- A free-text posted date ("3 days ago") is ignored; only ISO, common written dates and RFC 822 dates (RSS) are read. A slash date is read only when it can't be day/month or month/day ("10/01/2026" is ignored, since a wrong guess shows an ad months older than it is).
- Greenhouse (`updated_at`) and Atlassian (`updatedDate`) store the ad's last-edit date as `posted_at`, so their posted date is not read: no `date_refreshed`, and it never moves the start of `long_open` back (the pool's first sighting is used). The SEEK sentence in the `long_open` detail is shown only for SEEK ads.
- Date signs never fire for Lever (no posted date is stored) or Workday (`postedOn` is "Posted 30+ Days Ago"); `long_open` there counts from the pool's first sighting.
- `first_seen` is when the pool crawled the ad, not when the employer posted it. `reposted` ignores copies found through another source for that reason; a real repost of the same role on a different board is therefore not counted.
- If the same ad is listed under two company names, `reposted` finds it only through its description.
