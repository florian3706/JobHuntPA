# JobHuntPA design: user accounts, per-user encryption, admin panel, Stripe memberships and email digests

**Base.** `main` at 48fb435, plus Phase 1 of the documents work (d8eea88 on branch `worktree-agent-a3d7c32a94762a7dd`). Phase 1 adds notes and summaries as `Document` rows (`filetype` "note"/"summary"), `concise_text`, `use_concise`, `summary_of`, `updated_at`, the "preferences" kind and `llm_usage`. Rebase every phase below on it.

**Threat model.** The goal is that someone holding only the database, its backups or the `data/` folder cannot read a user's searches, documents, jobs or personal details. This holds even if that person is the operator browsing with `sqlite3`, as long as the user has not opted in to background access.

It does not protect against an operator who changes the running code or reads process memory, and nothing at rest can. Section 2.5 says this plainly, and the user-facing text in section 6 says it too.

**Overall shape (the simplest design that meets the requirements):**
- **One data key per user.** Each user has one random data key (DEK) covering all their workspaces.
- **Column-level encryption.** Encryption happens per database column through SQLAlchemy `TypeDecorator`s. The key comes from a context variable that is set per request or per background run. Most code keeps reading `job.title` exactly as it does today.
- **Sessions.** Sessions live in the database. The DEK in a session row is wrapped by a secret that exists only in the user's cookie. Restarting the server does not log anyone out, and the database alone cannot unwrap the key.
- **Admin personal details.** User details for admins are encrypted with an X25519 key pair. Writing needs only the public key, so sign-ups and the Stripe sync work with no admin logged in.
- **Stripe.** Payments use Stripe Checkout and the customer portal. Status is kept current by checking on the return redirect and by polling. Webhooks are added later and reuse the same `apply_subscription()` function.
- **Email.** Email uses the standard library's `smtplib`. Templates are plain Python functions, with no template engine.

---

## 1. Key hierarchy and crypto choices

### 1.1 Primitives
New dependencies: `cryptography` and `argon2-cffi`.

| Purpose | Choice |
|---|---|
| Encrypting values and wrapping keys | AES-256-GCM (`cryptography...aead.AESGCM`) with random 96-bit nonces. Envelope v1: `0x01 \| kid(4) \| nonce(12) \| ciphertext+tag(16)`, which adds 33 bytes per value. `kid = HMAC-SHA256(DEK,"kid")[:4]`, so decrypting with the wrong user's key fails fast with `KeyMismatch`. The version byte allows a later switch, for example to ChaCha20-Poly1305 if Pi 4 benchmarks are poor. |
| Deriving a key from the password | argon2id raw output (`argon2.low_level.hash_secret_raw`) gives a 32-byte master value. HKDF-SHA256 splits it into `auth` and `kek`. One argon2 run per login does both jobs, which halves the cost on a Pi. |
| Sub-keys | HKDF-SHA256 with labelled `info` strings, for example `b"jh index v1"`. |
| Admin personal details | X25519 sealed boxes: ephemeral X25519, then HKDF over the shared secret and both public keys, then AES-GCM. |
| Associated data (AAD) | Columns use `b"jh/" + table.column`, set automatically by an `after_parent_attach` event so values can't be swapped between columns. Key wraps use their own labels, for example `pw\|{uid}`, `rc\|{uid}\|{code_id}`, `sess\|{sid}`, `srv\|{uid}\|{kek_id}`, `grant\|{uid}\|{apk_ver}`, `totp\|{uid}` and `file\|{stored_name}`. |

### 1.2 Hierarchy
```
password ─argon2id(salt, params)─► master ─HKDF "auth"─► pw_verifier (stored, compared in constant time)
                                         └HKDF "kek"──► KEK_pw ──wraps──► DEK
recovery code i (80-bit, 8 codes) ─HKDF(user salt)─► KEK_rc_i ──wraps──► DEK
session cookie secret (256-bit) ─HKDF─► KEK_sess ──wraps──► DEK            (one wrap per session row)
server KEK (key file) ──wraps──► DEK                                       (background opt-in users only)
DEK ──► AES-GCM for columns and uploaded files
    ├HKDF "index"──► blind-index key (jobs.url_index, company_sources.careers_url_index, commute cache keys)
    └encrypts──► the user's X25519 private key
Admin PII key pair (APK): public key in app_settings; private key sealed to each admin's X25519 public key
key file: pepper ─► email_hash = HMAC-SHA256(pepper, normalised email)
          signing ─► unsubscribe tokens
process-only random key (never on disk) ─► http_cache
```

**DEK lifetime.** The DEK is never rotated in place, because that would mean re-encrypting every row. It is replaced only when a password reset wipes the data. Random-nonce limits are not a concern: one user would need billions of encryptions to approach them. The `kid` and version byte would allow lazy re-encryption if it were ever needed.

### 1.3 The server key file, `data/server-keys.json`
- **Format.** Mode 0600, written atomically (temp file in the same folder, then `os.replace`, the same approach as `app_settings._replace_file`). `JOBHUNT_KEY_FILE` can point elsewhere, mainly for tests. Contents:
  `{"version":1,"kek":{"current":"k1","keys":{"k1":b64}},"pepper":{"current":"p1","keys":{"p1":b64}},"signing":{"current":"s1","keys":{"s1":b64}},"created_at":…}`.
- **Why a file in `data/` and not `.env`.**
  - The uninstaller backs up `data/`, and the Windows setup keeps `data/` on update.
  - `.env` holds settings that the app edits.
  - A file can be rotated atomically.
- **Creation and the fingerprint check.**
  - The file is created at startup only when the `users` table is empty.
  - `app_settings["server_keys_fingerprint"]` stores `HMAC(pepper,"fp")[:8]`.
  - If the database has users and the file is missing or does not match the fingerprint, startup stops with: "Restore data/server-keys.json from your backup (it belongs with this database)."
  - A new file is never generated silently, because that would break email lookup and orphan background keys.
- **What it encrypts.**
  - Opt-in users' `server_wrapped_dek`.
  - TOTP secrets (see 3.6 for why).
  - The pepper and the signing key.
- **Rotating the KEK.** Admin panel → Server → "Rotate server key" (requires recent re-authentication), or `python -m backend.keys rotate-kek` with the app stopped. The steps are:
  1. Write the file with both the old and new keys.
  2. Re-wrap every `server_wrapped_dek` and `totp_secret_enc` in one transaction.
  3. Set the new key as current.
  4. Write the file with only the new key.

  This is crash-safe: on startup, any rows still tagged with the old `kek_id` are re-wrapped first.
- **Rotating the pepper.** The pepper is not a decryption key; leaking it only lets someone confirm guessed email addresses.
  - It rotates the same way: a new pepper is added and lookup tries current first, then previous.
  - A successful login rewrites the hash. "Re-index emails" in the admin panel recomputes all hashes from `pii_admin`.
- **Rotating the signing key.** The previous key is kept for 30 days so unsubscribe links in old emails still work.
- **Lost key file (disaster recovery).** `python -m backend.keys recover-lost --admin-email X` asks for that admin's password. It:
  - finds the admin row by checking the password against the admin rows (there are only a few);
  - writes a new key file;
  - recomputes every `email_hash` from the decrypted `pii_admin`;
  - clears all `server_wrapped_dek` and TOTP secrets.

  Users then sign in normally. Background-access users must switch it on again, and MFA users enrol again; the README says this.

### 1.4 Admin PII key (APK)
- **Creation.** An X25519 key pair is created when the first admin is created. The public key and its `apk_version` live in `app_settings`. The private key is stored in `admin_key_grants` (one row per admin), sealed to that admin's X25519 public key.
- **Writing personal details needs only the public key.** Sign-ups, email changes and the Stripe sync can all write `users.pii_admin`, `memberships.billing_admin`, `membership_grants.note_admin` and `admin_audit.detail_admin` with no admin logged in.
- **Reading.** In an admin request: admin DEK → their X25519 private key → open their grant → APK private key. This is repeated for each request; nothing stays in memory.
- **Making someone an admin.** The promoting admin (logged in, so they have the APK private key) seals it to the new admin's public key. Every user gets a key pair at sign-up, so this works without the new admin being present.
- **Demoting or deleting an admin.** The APK is rotated automatically: a new key pair is made, every admin-sealed value is decrypted and re-sealed to the new public key, and the remaining admins get new grants. This is cheap at this scale.
- **The last admin loses both their password and every recovery code.**
  - Personal details become unreadable.
  - Users can still sign in, because `email_hash` doesn't need the APK, and membership gating keeps working because its fields are plaintext.
  - The operator runs `python -m backend.keys new-admin-key --email X` (making X an admin if needed). This creates a new APK and marks old `pii_admin` values as stale (`pii_admin_version` lower than current).
  - Stale details heal themselves: every user also has their own copy (`pii_self`, under their DEK), so their details are re-sealed at their next login, or straight away for background-access users. Until then the admin list shows "Details unavailable until this person signs in".
  - The admin panel warns while only one admin exists.

### 1.5 Finding an account by email
- `users.email_hash = HMAC-SHA256(pepper, NFKC(email).strip().lower())` is a unique 32-byte blob.
- Login, the forgot-password form, invites and the duplicate check at sign-up all look it up without decrypting anything.

### 1.6 Which key the server holds when it sends each email
| Email | Where the address comes from | Key available |
|---|---|---|
| Forgot password | The address the person just typed. It is sent only if its hash matches; nothing is decrypted. | None needed |
| Admin "send password reset" | `pii_admin.email` | The admin's session (APK) |
| Invite | The address the admin typed | — |
| Email verification and change of address | Typed, or `pii_self` | The user's session DEK |
| Security notices (password changed, MFA turned off by an admin) | `pii_self` (in session) or `pii_admin` (admin action) | Session DEK or APK |
| Job digest | `pii_self.email` and job data | The server KEK unwraps `server_wrapped_dek`. This is why digests require background access. |
| Receipts and failed payments | Stripe sends these itself | — |

---

## 2. What gets encrypted, and what stays plaintext

### 2.1 How it works (`backend/crypto.py`)
- **Types.** `SealedText`, `SealedJSON`, `SealedInt` and `SealedFloat` are `TypeDecorator(LargeBinary)` with `cache_ok=True`. Because SQLite is dynamically typed, existing TEXT, INTEGER and REAL columns can hold the new blobs with no table rebuilds.
- **NULL passes through**, so `IS NULL` filters still work. The fact that a value is null or not leaks; section 2.5 lists this.
- **Old plaintext is still read.** A value that is not a blob starting with `0x01` is returned as legacy plaintext and coerced to the column's type. This makes the migration resumable and lets `server_default` values still work.
- **Comparisons are blocked.** A custom `comparator_factory` raises `TypeError` for any operator except `IS` and `IS NOT`, including `==`, `in_`, `like` and `desc`. Comparing a ciphertext in SQL is therefore a loud bug, not a silent mismatch.
- **Key scope.** `crypto.as_user(user_id, dek)` is a context manager that sets the scope context variable. `crypto.current()` reads it. Sealing or opening with no scope raises `Locked`; a key mismatch raises `KeyMismatch`, which the exception handler turns into a 404.
- **API:**
  - `blind_index(purpose, value) -> bytes`: `HMAC(index_key, purpose\0value)[:16]`.
  - `wrap`/`unwrap(kek, secret, aad)`.
  - `seal_to(pub, plain, aad)`/`open_sealed(priv, blob, aad)`.
  - `seal_file(data, path, aad)`/`open_file(path, aad)`.
  - `submit(pool, fn, *args)`, which is `pool.submit(contextvars.copy_context().run, fn, *args)`.
- **Threads.** `tasks.start` captures `copy_context()` and runs the job inside it. The scorer's thread pool must use `crypto.submit`. The research and office-lookup thread pools write only to shared plaintext tables, but use `crypto.submit` anyway for consistency.
- **Rules to add to CLAUDE.md** (a proposal; I have not edited it):
  - Never filter, sort or `DISTINCT` on sealed columns.
  - Check ownership through plaintext `workspace_id` before loading a sealed row.
  - Use `crypto.submit` for thread pools.
  - Never log user data.

### 2.2 Table by table
"Sealed" means encrypted under the owner's DEK.

| Table | Sealed | Plaintext, and why |
|---|---|---|
| workspaces | name | id, `owner_id` (new; used to enforce ownership), created_at |
| jobs | source, external_id, company, title, location_text, country, lat, lng, work_mode, office_days, salary_min, salary_max, salary_text, url, description, description_hash, posted_at, industry, distance_km, status, excluded_reason, office_text, office_lat, office_lng, office_source, location_verdict | id, workspace_id, source_id, detail_status (filtered in SQL), hidden (filtered), closed_at, duplicate_of, first_seen, last_seen, detail_fetched_at, office_checked_at. New: `url_index` (blind index, unique with workspace_id) and `first_run_id` (for digests). |
| fit_results | score, evidence_json, error | job_id, status, profile_hash (a hash of high-entropy text, not reversible), model, created_at |
| job_duplicates | reasons_json | ids, status, decided_by, score (a similarity number), timestamps |
| documents (including notes and summaries) | filename (or note/summary title), text, concise_text | filetype, kind, stored_name (a random id), use_for_scoring, use_concise, concise_model, concise_at, summary_of (ids), timestamps |
| `data/uploads/` files | The whole file, under the DEK, as `<uuid>.sealed`. Text extraction runs on `BytesIO` in memory, so plaintext never touches disk. | — |
| user_profile | titles, keywords_include, keywords_exclude, salary_floor, max_office_days, seek_locations, commute_from, commute_arrive_by, commute_leave_at | the five work-mode and SEEK booleans, seek_max_pages |
| dealbreakers | industries, keywords | id |
| pins | label, lat, lng, radius_km (home location) | kind |
| company_sources | label, careers_url, url, adapter_key, last_error, and the legacy company/board/host/tenant/site/base_url columns (emptied) | source_type, enabled, last_run, last_count, last_method. New: `careers_url_index`. |
| cover_letters | text, instructions | model, reasoning_effort, edited, timestamps |
| chat_threads / chat_messages | title / content, sources_json | job_ids_json (ids), role, web_search, model, timestamps |
| search_runs | stage, progress_json, summary_json, error | kind, scheduled, state, timestamps. New: `error_code` (for example "interrupted", written without a key) and `free_search` (bool). |
| search_profile (legacy) | every content column (it may hold an old CV) | id |
| commute_cache | It becomes per user: `key` holds `blind_index("commute", key)` and result_json is sealed. Origins are home and stations. | New `owner_id`, fetched_at |
| search_schedules | — | Stays plaintext: the scheduler must read every schedule without a key. It is low sensitivity (times, days, state). |
| users / user_keys / memberships / grants / audit | See section 4: personal details sealed to the APK and/or the DEK | Gating, role and auth state |
| company_profiles, geocode_cache, app_settings, llm_usage | — | Shared or global. Profiles are public research, not tied to a user. llm_usage holds only counts (and gains `user_id`). |
| http_cache | `url` holds `HMAC(process key, url)` and the body is sealed with the process-only key. Every row is purged at startup because it can't be read after a restart. | status_code, fetched_at |

Notes:
- **Theme.** The theme is stored in `users.prefs_enc` under the DEK, not as plaintext. "Dyslexia" or a colour-vision theme is health-related information.
- **Leftover plaintext files.** Old `data/cache/*.html` files (from an earlier version, unused) and `data/jobhunt.db.bak-*` hold plaintext. The migration reports them and offers to delete them (section 5).

### 2.3 Code changes forced by sealing
- **`pipeline.upsert_posting`:** `Job.url == p.url` becomes `Job.url_index == blind_index("url", p.url)`.
- **Closing jobs no longer listed** (`pipeline._process_source`):
  - `Job.url.notin_(seen)` becomes a `url_index` NOT IN.
  - `Job.source == adapter.source` becomes `source_id IS NULL` plus a Python check.
- **`sources.create_source`:** the duplicate check uses `careers_url_index`.
- **`excluded_reason`:**
  - Normalise `""` to `None` (a `@validates` hook) and replace `== ""` with `IS NULL` in scorer, research and offices.
  - `filters.softened_job_ids`: replace `like(prefix)` with "excluded" (IS NOT NULL) plus a prefix check in Python.
- **`DISTINCT` on company/source** (`research._board_labels`, `duplicates.Ranking`): de-duplicate in Python.
- **`owned()`:** first `SELECT workspace_id` (plaintext) and only then `db.get`. `/api/runs/{id}` gets an ownership check; it has none today, which is a bug.
- **`/api/companies`:** return only the profiles for companies in the current workspace's jobs. Today it would show every user which companies others researched.
- **`tasks`:**
  - `active_run()` returns plaintext fields only.
  - Refuse only when the same workspace has an active run; otherwise queue first-in-first-out on the single worker.
  - `mark_interrupted` writes `error_code`.
- **`db._fix_legacy_data`:** run its `jobs.source LIKE` repair only before encryption.
- **`init_db`:** create workspace 1 only on a fresh database with no users.

### 2.4 Performance (Raspberry Pi)
- **Cost.** `list_jobs` decrypts about 25 sealed columns per job. On a Pi 4 (no AES instructions) a small value costs roughly 4–8 µs including Python overhead, so 5,000 jobs would add about 0.5–1 s.
- **Mitigation, part of Phase D:** with no `include` and `office_slack=0`, filter `excluded_reason IS NULL AND hidden = 0` in SQL. Count the omitted jobs with `COUNT(*)`, and load merged copies with `duplicate_of IN (...)`. Excluded and hidden jobs are most of the list, so this cuts decryption by about 80%.
- **Benchmark.** Add `tools/bench_jobs_list.py`. Target: under 1.5 s for 5,000 jobs on a Pi 4.
- **Fallback if it fails:** seal the card fields as one JSON blob per row (`jobs.card_enc`). Simplicity: per-column first.

### 2.5 What still leaks, and the real limits
**Metadata the operator can see:**
- Row counts per workspace and per user.
- Ciphertext lengths, which equal plaintext length + 33 (for example the length of a CV).
- All timestamps: when jobs were seen or closed, when runs, chats and sign-ins happened. `last_active_on` is stored by day only.
- Whether each sealed column is null (for example whether a job is excluded or has an office set).
- The plaintext flags: hidden, detail_status, document kind and filetype, pin kind, use_for_scoring, run kind and state, fit status, and model names.
- Schedules, including the Australian state.
- Membership status, plan, period and free searches used; role, MFA on or off, background access on or off; failed-login counters.
- Shared caches: which companies were researched (not by whom) and which places were geocoded.
- llm_usage token counts per user and purpose.
- Uploaded file sizes.

**Job postings** are public, but the operator can't tell which ones a user has: URL, title and company are sealed, and `url_index` is keyed per user.

**The LLM provider** sees resumes, jobs, chats and documents in plaintext during scoring, chat, cover letters and research, under the operator's API account and the provider's retention policy. Prompt-cache keys sent to providers are hashes of the user's documents.

**The running server** holds plaintext in memory while it serves a request or runs a background job, and memory can reach swap or core dumps.

**An operator who changes the code**, proxies traffic before TLS terminates, or reads process memory can capture passwords and data at the next login. Encryption at rest cannot prevent this.

**Users with background access:** the operator can decrypt their data using the key file.

**Third parties:**
- The email provider sees digest contents (job titles, companies and fit summaries).
- Stripe sees the email address and payment details.

**Logs must never contain plaintext.** Today they do in three places:
- httpx logs every request URL at INFO, which includes SEEK search terms, Nominatim queries and station names. Set the `httpx` and `httpcore` loggers to WARNING.
- `pipeline` logs source labels and `geo` logs geocode queries. Replace these with ids and exception class names.
- uvicorn access logs include query strings, which will carry tokens. Add a filter that strips queries.

`tasks` stores exception text only in the sealed `error` column and logs only the class name.

**Browser caching.** Send `Cache-Control: no-store` on every `/api` response so nothing stays in a shared computer's browser cache.

**Flash storage.** `PRAGMA secure_delete=ON` plus `VACUUM` zero freed SQLite pages, but SSD and SD-card wear levelling can keep old blocks. The README says so.

---

## 3. Sessions

1. **The key between requests.** The session row stores `wrapped_dek = AESGCM(HKDF(cookie_secret,"session dek v1"), DEK, aad=sid)`. The cookie carries the 32-byte secret; the database stores only `sid = SHA-256(secret)`.
   - Each request unwraps the DEK in the ASGI middleware (`backend/sessions.py`). The cost is one HKDF and one AES-GCM operation, a few tens of microseconds.
   - The middleware sets `crypto.as_user` for the request. Sync endpoints run in a copied context and see it; add a test that proves this.
   - No key stays in memory between requests. Restarting the server logs nobody out. The database alone cannot unwrap a key.
   - Background runs hold the key for as long as the run lasts, even if the user logs out meanwhile.
   - Background-access keys are unwrapped with the KEK each time they are needed, and never cached.
2. **Cookie:**
   - Name `__Host-jh_session` with `Secure` when the request is HTTPS. HTTPS means `request.url.scheme` after uvicorn's proxy headers (`--forwarded-allow-ips 127.0.0.1` is the default, and Tailscale serve connects from loopback), or a Host matching an `https` `JOBHUNT_PUBLIC_URL`.
   - On plain `http://127.0.0.1` (local installs; Safari rejects Secure cookies there) the name is `jh_session`.
   - Always HttpOnly, `SameSite=Lax` (so the "Open" link in a digest email arrives signed in) and `Path=/`.
   - `Max-Age` is set only when "Keep me signed in" is ticked.
3. **CSRF:**
   - Every POST, PUT, PATCH or DELETE needs `X-CSRF-Token == b64url(HMAC(cookie_secret,"csrf"))`. Nothing extra is stored; `/api/auth/me` returns the token, and `common.js:apiResponse` adds the header.
   - Requests whose `Origin` doesn't match the Host or the public URL are rejected.
   - The pre-session auth endpoints (`login`, `signup`, `first-run`, `forgot`, `reset`, `recover`) require `Content-Type: application/json` and the Origin check, which blocks cross-site form posts and login CSRF.
   - Exempt: `/api/email/unsubscribe` (signed token) and `/api/billing/webhook` (Stripe signature).
4. **Lifetimes:**

   | Session type | Idle limit | Absolute limit |
   |---|---|---|
   | Normal | 24 h | 7 d |
   | "Keep me signed in" | 30 d | 90 d |
   | Waiting for the MFA code | 10 min | — |

   - The 24-hour idle limit is longer than 20 hours, which falls under WCAG 2.2.1's 20-hour exception. Data is autosaved anyway.
   - Sensitive actions require re-entering credentials within the last 15 minutes ("recent auth").
   - `last_seen_at` is written at most every 5 minutes.
5. **Logout:**
   - Deletes the row, expires the cookie and sends `Clear-Site-Data: "cache"`. The JS clears `jobhunt.ws`.
   - "Sign out everywhere" deletes all of the user's sessions.
   - Password change, a reset, disabling the account and deleting it also delete every session.
6. **Rate limiting and lockout** (`backend/ratelimit.py`, in memory; the app is a single uvicorn process):
   - **Per IP** (the client address from X-Forwarded-For on loopback): auth endpoints allow 10 requests per 5 minutes and 60 per hour. Unsubscribe allows 30 per minute.
   - **Per account:**
     - After 5 failures in a row, wait 30 s, doubling up to 15 minutes (`users.locked_until`). There is no permanent lockout, which would be a denial-of-service vector. Admins can unlock.
     - 5 wrong MFA codes drop the pending session.
   - Responses are 429 with `Retry-After` and a plain message.
   - Unknown emails still run a dummy argon2 derivation, so responses can't be used to tell which accounts exist.
   - No CAPTCHA (WCAG 3.3.8).
   - TOTP secrets are encrypted under the server KEK, not the DEK. This is deliberate: the TOTP code can then be checked during an email reset, so someone who controls a user's mailbox cannot wipe an MFA user's locked data.
7. **argon2id settings:**
   - `JOBHUNT_KDF_PROFILE=default`: m=64 MiB, t=3, p=4 (RFC 9106's second recommendation and argon2-cffi's default). Estimated 0.4–0.9 s on a Pi 4 and about 0.3 s on a Pi 5; `python -m backend.keys bench-kdf` measures it.
   - `low`: m=19 MiB, t=2, p=1 (the OWASP minimum) for Pi 3 or Zero 2 W.
   - `test`: m=1 MiB, t=1, p=1, honoured only when `JOBHUNT_TESTING=1`.
   - Parameters are stored per user and updated at the next login when the profile changes.
   - At most 2 key derivations run at once (a semaphore), which caps memory at 128 MiB.
   - Passwords need at least 10 characters with no composition rules ("a few random words work well").

---

## 4. Data model (all models in `backend/db.py`)

**users:**
- `id`, `email_hash BLOB UNIQUE`, `role` ('user'|'admin'), `status` ('active'|'disabled'), `created_at`, `created_via` ('first_run'|'bootstrap'|'invite'|'open'), `last_active_on DATE`, `email_verified BOOL`, `onboarding_step INT NULL` (NULL means done).
- `kdf_salt`, `kdf_params TEXT`, `pw_verifier BLOB`.
- `mfa_enabled BOOL`, `totp_secret_enc BLOB` (KEK), `totp_pending_enc BLOB` (KEK, during enrolment), `totp_last_step INT` (replay protection).
- `failed_logins INT`, `locked_until`.
- `pii_admin BLOB` (sealed to the APK: {email, name}), `pii_admin_version INT`, `pii_self BLOB` (DEK: {email, name}), `prefs_enc BLOB` (DEK: {theme}).

**user_keys** (1:1 with users, ON DELETE CASCADE):
- `dek_kid`, `pw_wrapped_dek`.
- `server_wrapped_dek NULL`, `server_kek_id NULL`, `background_since NULL`.
- `x25519_pub`, `x25519_priv_enc` (DEK).
- `recovery_salt`, `pending_codes_enc NULL` (DEK: recovery codes still to be shown once).

**recovery_codes:** `id`, `user_id` (CASCADE), `wrapped_dek`, `created_at`, `used_at NULL`. Eight codes.

**sessions:** `id TEXT PK` (sid), `user_id` (CASCADE), `state` ('pending_mfa'|'active'), `wrapped_dek`, `persistent`, `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `reauth_at`.

**auth_tokens:** `id`, `kind` ('reset'|'invite'|'verify_email'|'email_change'), `token_hash UNIQUE`, `user_id NULL`, `created_by NULL`, `created_at`, `expires_at`, `used_at`, `payload_enc` (sealed with `HKDF(token)`. For example, a reset token carries the email address so the reset can rebuild `pii_self`; an invite carries the address to pre-fill).

**admin_key_grants:** `user_id PK`, `apk_version`, `sealed_priv`, `granted_by`, `granted_at`.

**admin_audit:** `id`, `at`, `admin_id`, `action`, `target_user_id`, `detail_admin` (APK).

**memberships** (1:1 with users):
- `exempt BOOL`, `free_searches_used INT`.
- `stripe_ref TEXT UNIQUE`: a random opaque id, used as `client_reference_id` and as `metadata.jh_ref`.
- `stripe_status`, `plan`, `current_period_end`, `cancel_at_period_end`, `stripe_synced_at`.
- `billing_admin BLOB` (APK: customer and subscription ids, card brand and last 4, amount and currency).
- `billing_self BLOB` (DEK: customer and subscription ids, so the user can open the portal).

**membership_grants:** `id`, `user_id`, `kind` ('free_period'|'manual'), `starts_at`, `ends_at`, `created_by`, `created_at`, `revoked_at`, `note_admin` (APK).

**email_prefs** (1:1 with users): `digest_enabled BOOL`, `digest_every_days INT` (1–7), `opted_in_at`, `last_sent_at`, `last_checked_at`, `unsubscribe_id TEXT UNIQUE` (16 random bytes).

**Changes to existing tables:**
- workspaces: `owner_id`.
- jobs: `url_index`, `first_run_id`.
- company_sources: `careers_url_index`.
- commute_cache: `owner_id`.
- search_runs: `error_code`, `free_search`.
- llm_usage: `user_id`, filled in `record_usage` from the workspace's owner, ON DELETE SET NULL.

`_add_missing_columns` adds these columns to existing databases (without the foreign-key clause, which SQLite can't add). A new `_ensure_indexes()` step runs `CREATE UNIQUE INDEX IF NOT EXISTS` on `(workspace_id, url_index)` and `(workspace_id, careers_url_index)`. The old `uq_*_url` indexes stay; random ciphertexts never collide.

**Activity:** the session middleware sets `last_active_on = today` (UTC) at most once a day per user. Background work doesn't count. Day precision is deliberate, to limit the leak.

**Workspace ownership:**
- Sign-up creates "My job search", owned by the new user.
- `create_workspace` sets `owner_id`. `copy_from` must be a workspace the user owns. "You can't delete the only workspace" is checked per owner.
- The first admin claims every workspace with `owner_id IS NULL`. Workspaces are never transferred.
- `current_workspace(x_workspace, ws, user=Depends(current_user), db)`:
  - With no header, it uses the user's lowest workspace id.
  - Otherwise it looks up the workspace's `owner_id`; anything other than the user gives a 404 (not a 403, so it doesn't reveal that the workspace exists).
- `list_workspaces` filters by owner.
- Defence in depth: the request's key is always the user's own, so any slip that loads another user's row fails with `KeyMismatch`, which becomes a 404.
- App-wide settings become admin-only through a `require_admin` dependency: `/api/settings/*` (LLM and commute keys), `/api/llm/*` (reasoning levels) and `/api/score/test`.

**`app_settings` keys:** `signup_mode` ('invite' by default | 'open' | 'closed'), `free_searches` (3), `apk_public`, `apk_version`, `server_keys_fingerprint`, `encryption_migration`.

---

## 5. Migrating existing installs

1. **Upgrade start.**
   - `init_db()` creates the new tables and columns. Nothing is encrypted yet.
   - `keys.load_or_create()` creates the key file, because there are no users yet.
   - It writes a first-run token to `data/first-run.txt` (0600) and logs `First run: open /first-run.html#token=…`.
   - Every API except `/api/health`, `/api/auth/state` and `/api/auth/first-run` answers 401 `{"code":"setup_required"}`, and the frontend redirects to the first-run page. The scheduler skips workspaces with no owner.
   - `launch.py`, after its health check, opens `first-run.html#token=…` when that file exists. The token is in the URL fragment, so it never reaches server logs.
2. **Without a browser (CI, servers).** If the process environment (not `.env`) has `JOBHUNT_ADMIN_EMAIL` and `JOBHUNT_ADMIN_PASSWORD` and there are no users, the lifespan creates that admin and runs step 3 straight away.
3. **`create_first_admin()`**, in one transaction:
   - Creates the user, keys, APK and grant, 8 recovery codes, membership and email_prefs.
   - Runs `UPDATE workspaces SET owner_id=:id WHERE owner_id IS NULL`, or creates a workspace if none exist.
   - Sets `encryption_migration={"state":"pending","owner":id}`.
   - Deletes `first-run.txt`.

   The first-run form also asks for background access, ticked by default, because this person runs the server themselves. That keeps their existing scheduled searches running.
4. **Encryption run** (`backend/migrate_encryption.py`, its own thread, using the admin's key from the first-run request):
   1. One plaintext backup, using SQLite's online backup API, to `data/jobhunt.db.bak-before-encryption`.
   2. **Uploads,** file by file:
      1. Write `<uuid>.sealed` to a temp file, fsync it, decrypt it again and compare a hash with the original.
      2. Rename it into place, then update `documents.stored_name` and commit.
      3. Delete the original.

      A plaintext file left beside a matching `.sealed` file after a crash is deleted when the run resumes.
   3. **Tables,** in batches of 200 rows per transaction. Select rows where `typeof(col) NOT IN ('blob','null')` for any sealed column, then encrypt and compute `url_index`, `careers_url_index` and the commute keys. Old `commute_cache` and `http_cache` rows are simply deleted.
   4. **Check:** zero non-blob values in every sealed column, and a sample of rows in each table decrypts.
   5. `PRAGMA wal_checkpoint(TRUNCATE)` and `VACUUM`, best effort (`VACUUM` needs free space equal to the database size).
   6. `state="done"`.

   **Resuming:** rows are selected by whether they are still plaintext, so a restart just carries on. At startup, `running` becomes `pending`, and the run resumes at that admin's next login, when their key is available.

   **While it runs:** data APIs answer 423 `{"code":"encrypting","done","total"}` and the page shows accessible progress (a `role="progressbar"` plus text). Sign-up is blocked.
5. **After the run:** the completion screen lists leftover plaintext files: the new backup, older `jobhunt.db.bak-*` files and `data/cache/*.html`. It offers "Delete them now (recommended)", and the admin panel keeps showing a warning until they are gone.
   - From this release on, every connection uses `PRAGMA secure_delete=ON`.
   - Going back to an older version is not supported; the README explains how to restore a backup.
6. **Local single-user installs** follow the same code path: the first launch after updating opens the first-run page, and accounts are always on.
7. **Tests:** `tests/helpers.py` sets `JOBHUNT_TESTING=1`, `JOBHUNT_KDF_PROFILE=test` and the bootstrap admin environment before importing the app. It provides:
   - `client(email=ADMIN)`: a logged-in `TestClient` with the CSRF header set.
   - `make_user()`.
   - `as_admin()`: the key scope for writing rows directly in tests.

   Existing tests switch from `TestClient(app)` and raw `SessionLocal` writes to these. `tools/demo_data.py` and `tools/readme_screenshots.py` (Playwright login) are updated the same way.

---

## 6. Flows
WCAG notes that apply to every auth page:
- Each form has `autocomplete` values: `username`/`email`, `current-password`, `new-password`, `one-time-code`, `name` (1.3.5).
- A "Show password" button with `aria-pressed`; no "confirm password" or "confirm email" fields (3.3.7).
- Paste is never blocked, and password managers work: the MFA step includes a hidden `autocomplete="username"` field.
- Codes are accepted with any case, spaces or hyphens, and with O/0 and I/L/1 mapped (3.3.8).
- Errors appear as an error summary at the top that receives focus and links to each field, plus inline messages linked with `aria-describedby` (3.3.1, 3.3.3).
- Inputs use 16px text, layouts are one column and work at 320 px wide (1.4.10), targets are at least 24 px (2.5.8), and sticky elements never cover the focused field (2.4.11).
- "Trouble signing in?" sits in the same place on every page (3.2.6).
- Status messages use `aria-live`.

**6.1 Sign-up, then the wizard, then recovery codes**
1. `signup.html?invite=…`: `GET /api/auth/state` returns the sign-up mode and whether the invite is valid. The fields are email, password, an optional name and "Keep me signed in".
2. `POST /api/auth/signup`:
   - Applies rate limits and checks the mode or invite. An existing address gets "An account with this email already exists. Sign in or reset your password."
   - Creates the user, DEK and wraps, key pair, 8 recovery codes in `pending_codes_enc`, membership (3 free searches when payments are on), email_prefs (off), the workspace and a session.
   - If the invite was emailed to this same address, the email counts as verified. Otherwise a verification email is sent when SMTP is set up; digests require a verified address.
3. `welcome.html`: each step has an `<h2 tabindex=-1>` that receives focus, "Step n of 6" as text, Back/Next and, where it makes sense, "Skip for now". Each step saves through the existing APIs (`/api/profile`, `/api/pins`, `/api/schedule`, `/api/documents`, `/api/documents/suggest-titles`), and `onboarding_step` records progress.
   1. **Theme.** A radio group in a fieldset. Each card previews its theme by carrying `data-theme` on the card itself, which works because themes.css uses attribute selectors. Choosing applies the theme to the page at once and saves it with `PUT /api/account/prefs`.
   2. **Job titles,** with an optional resume upload and "Suggest titles".
   3. **Where:** SEEK locations, a home suburb (a text field geocoded through a new `GET /api/geo/lookup?q=` that creates the home pin; no map needed, which keeps it keyboard-friendly), radius, work modes and maximum office days.
   4. **Optional extras:** salary floor and exclude keywords.
   5. **Scheduled searches and emails:** the background-access switch with its explanation, and the digest switch with "every n days". When an option isn't available it explains why (needs a membership, or email isn't set up).
   6. **Recovery codes,** shown once:
      - Copy all, Download .txt and Print.
      - "I've saved my recovery codes" (a checkbox), then `POST /api/auth/recovery-codes/ack` wipes `pending_codes_enc`.
      - Then "Run my first search (uses 1 of your 3 free searches)".

   If someone leaves before step 6, a banner stays up until the codes are acknowledged.

**6.2 Login**
1. `POST /api/auth/login {email, password, remember}` derives the master value and checks the verifier.
   - On failure: one message for every case and the failure counters.
   - With MFA off: an active session.
   - With MFA on: a `pending_mfa` session (the cookie is set) and `{"mfa_required":true}`.
2. `POST /api/auth/login/mfa {code}` checks TOTP with ±1 time step and rejects reused steps. Alternatively `{recovery_code}` uses up one code and then shows "7 codes left; make new ones in Settings".
3. A `next` parameter (relative paths only: it must start with "/" and not "//") sends the user back, for example to the job page a digest linked to.

**6.3 MFA enrol and turn off**
- **Enrol.** Settings → Account → "Two-step sign-in" → re-authenticate.
  - `POST /api/auth/mfa/setup` returns the otpauth URI, a QR code and the secret.
    - The QR code is made on the server with segno as inline SVG with `<title>`, so the secret is never sent to a CDN script.
    - The secret is also shown as text in groups with a Copy button, alongside an `otpauth://` link for setting up on the same device.
  - `POST /api/auth/mfa/enable {code}` finishes it, followed by a reminder that recovery codes also work instead of the app.
- **Turn off.** Re-authenticate with password plus a code or recovery code, then `POST /api/auth/mfa/disable`.
- **Admin override.** An admin can turn off a user's MFA (it is audited, and the user is emailed).

**6.4 Recovery codes**
- **At the MFA step:** see 6.2.
- **Forgot password, with a code (keeps your data):**
  - `forgot.html` offers "I have a recovery code" or "Email me a link". The email typed on the login page is carried over (3.3.7).
  - `POST /api/auth/recover {email, recovery_code, new_password, code | second_recovery_code}`. The code unwraps the DEK. With MFA on, the person also needs the TOTP code or a second recovery code.
  - The DEK is re-wrapped under the new password, the code is used up, other sessions are signed out and the person is logged in.
  - This path needs no SMTP, so it works on local installs.
- **Regenerate:** re-authenticate, then `POST /api/auth/recovery-codes` replaces all eight wraps and shows the new codes once.

**6.5 Forgot password by email, and a reset sent by an admin**
1. `POST /api/auth/forgot {email}` always answers 202 "If an account exists, we've sent a link".
   - In the background: look up `email_hash` and create a token (32 bytes, 1 hour, single use, `payload_enc` holding the email).
   - Send `${PUBLIC_URL}/reset.html#token=…`, with the token in the fragment.
2. `reset.html` calls `POST /api/auth/reset/info {token}`, which returns `{keeps_data, mfa_required}`.
   - **With background access (`keeps_data`):** "Your data will be kept."
   - **Locked:** the page shows the warning text from "Copy for users" below, a link to the recovery-code path, a required checkbox "I understand my saved data will be deleted" and the new-password field.
3. `POST /api/auth/reset {token, new_password, code?, accept_wipe}`. If MFA is on, the TOTP code is required, because the secret is under the KEK.
   - **With background access:** unwrap the DEK with the KEK and re-wrap it under the new password. The data is kept.
   - **Locked:**
     - Delete the user's workspaces (cascade), sealed files, recovery codes and `billing_self`.
     - Create a new DEK, a new key pair and new recovery codes (shown on the next page), plus a fresh workspace.
     - Rebuild `pii_self` from the token's email; `pii_admin` is unchanged.
     - Memberships stay.
     - An admin who loses their grant this way gets a warning, and other admins see "Share the admin key again".
   - Either way: all sessions are deleted and a "Your password was changed" notice is sent.
4. **Sent by an admin:** in the admin panel, "Send password reset email" decrypts `pii_admin.email` and goes through the same token flow. Admins never set passwords.

**6.6 Change password.** `POST /api/auth/password {current_password, new_password, sign_out_others=true}`. The DEK is re-wrapped and nothing is re-encrypted.

**6.7 Background access on and off.** Settings → Account → "Background access" (shown as a switch).
- **On:** re-authenticate, then wrap the DEK with the current KEK.
- **Off:** set `server_wrapped_dek=NULL`. Scheduled searches and digests stop, and the explanation says so.

**6.8 Delete account.** `POST /api/account/delete {password}`. Confirmation is the password, which a password manager can fill; there's no "type DELETE" test.
1. Refuse while one of the user's runs is active.
2. Cancel the Stripe subscription immediately.
3. Delete sealed files, workspaces (cascade), keys, sessions and grants.
4. Set `llm_usage.user_id` to NULL.
5. If the user was an admin, rotate the APK. The last admin can't delete their account.

**6.9 Change of email address.** A verification link goes to the new address. Confirming updates `email_hash`, `pii_self` and `pii_admin` (sealed to the APK public key).

**Copy for users (keep it short):**
- **Sign-up and first run:** "Your searches, documents and jobs are encrypted with a key that only your password or a recovery code can unlock. Not even the person who runs this server can read them from the database. Save your recovery codes: if you forget your password, a recovery code is the only way to keep your data. Resetting your password by email without one gives you a fresh, empty account."
- **Background access, off:** "Only you can unlock your data. JobHuntPA can't run searches while you're away or email you new jobs."
- **Background access, on:** "JobHuntPA keeps a key on the server so it can run scheduled searches and email you new jobs. The server's operator could technically read your data, and a password reset by email keeps your data."
- **Account page link, "How your data is protected":** the limits from 2.5 in plain words. "Admins can see your email, name and membership, never your searches, documents or jobs. Your AI provider sees what it scores. Someone who changes the app's code on the server could capture your data."
- **Reset page, locked account:** "Your saved searches, documents and jobs are locked with your old password. Setting a new password without a recovery code deletes them, and they can't be brought back."

---

## 7. Admin panel (`backend/admin.py`, `frontend/admin.html` + `admin.js`, linked from the header's account menu for admins)
Admin routes never use `current_workspace` and never read sealed DEK columns. Their key scope is the admin's own DEK, so any mistake fails closed. A test checks that admin endpoints with another user's ids return no content fields.

**Endpoints** (every one requires an admin; the ones marked † also require re-authentication within 15 minutes):
- **Stats and users**
  - `GET /api/admin/stats?days=30`
  - `GET /api/admin/users`: id, email, name, role, status, created_at, last_active_on, mfa, background, membership summary, free searches used, number of workspaces.
- **User actions**
  - `POST /api/admin/users/{id}/reset-email`
  - `POST …/{id}/mfa/disable`
  - `POST …/{id}/disable|enable`
  - `DELETE …/{id}`†
- **Roles**
  - `POST …/{id}/role {role}`†: makes a grant, or rotates the APK.
  - `POST …/{id}/regrant`: shares the admin key again with an admin who lost it.
- **Membership**
  - `PUT …/{id}/membership {exempt?, free_searches_used?}`
  - `POST …/{id}/grants {kind, starts_at, ends_at | days, note}`, `PATCH|DELETE …/grants/{gid}`
  - `POST /api/admin/billing/sync`
- **Sign-up and invites**
  - `GET|PUT /api/admin/signup {mode, free_searches}`
  - `POST /api/admin/invites {email?, send_email}` returns `{link}`; `GET|DELETE /api/admin/invites`
- **Server settings**
  - `GET|PUT /api/admin/settings/email` (SMTP and public URL) and `POST …/email/test`
  - `GET|PUT /api/admin/settings/billing`
  - `POST /api/admin/keys/rotate`†
- **Plaintext clean-up**
  - `GET /api/admin/plaintext-audit`: any non-blob values in sealed columns, plus leftover files.
  - `POST /api/admin/plaintext-files/delete`†
- **Audit log:** `GET /api/admin/audit`

The existing LLM and commute-key cards stay on the Settings tab but are shown to admins only.

**Stats queries:**
```sql
-- users
SELECT COUNT(*) total, SUM(role='admin') admins,
       SUM(last_active_on >= date('now','-6 days'))  active_7d,
       SUM(last_active_on >= date('now','-29 days')) active_30d,
       SUM(created_at >= datetime('now','-30 days')) new_30d
FROM users WHERE status != 'deleted';
-- LLM usage by day / provider / model / purpose
SELECT date(created_at) day, provider, model, purpose, COUNT(*) requests,
       SUM(COALESCE(input_tokens,0)) input, SUM(COALESCE(output_tokens,0)) output,
       SUM(COALESCE(cached_tokens,0)) cached, SUM(COALESCE(cache_write_tokens,0)) cache_write
FROM llm_usage WHERE created_at >= :since GROUP BY 1,2,3,4 ORDER BY 1;
-- per user (llm_usage.user_id, falling back to the workspace owner for older rows)
SELECT COALESCE(u.user_id, w.owner_id) uid, u.model, COUNT(*), SUM(u.input_tokens), SUM(u.output_tokens), SUM(u.cached_tokens)
FROM llm_usage u LEFT JOIN workspaces w ON w.id = u.workspace_id WHERE u.created_at >= :since GROUP BY 1,2;
```
The panel also shows the cache hit rate (cached tokens ÷ input tokens). Users are labelled with their decrypted email.

**UI:**
- Summary tiles (users, admins, active in 7 and 30 days, new in 30 days).
- LLM usage as tables with captions and `<th scope>`, which are accessible. A chart is optional; if added, follow the dataviz skill.
- A users table with sortable columns (`aria-sort`). Each action button names its target, for example "Send reset email to alice@…".
- Edit dialogs use native `<dialog>` with focus management.
- Below 640 px, table rows become cards.
- Sections: Sign-up and invites; Server (public URL with a "Use this address" button pre-filled from the current origin, SMTP plus a test email, Stripe status); Security (rotate key, the plaintext-files warning, the one-admin warning).

---

## 8. Stripe (`backend/billing.py`, `frontend/billing.html` + `billing.js`)
- **Configuration:**
  - `.env` holds `STRIPE_SECRET_KEY` (sk_ or rk_, test or live) and later `STRIPE_WEBHOOK_SECRET`. Both are written through `app_settings` (admin only).
  - Prices are found by Stripe lookup keys `jobhunt_monthly` and `jobhunt_yearly`, so `.env` holds no price ids and switching from test to live keeps working. The operator creates one product with one or two prices carrying those lookup keys.
  - Lookups use `prices.list(lookup_keys=…, active=True)` and are cached for 10 minutes. The admin page shows the prices found, or setup instructions.
  - Use `stripe.StripeClient(api_key)` with no global state, isolated in billing.py the way the Anthropic SDK is.
  - **When billing is off** (no key or no prices), gating is off and everyone has access, which is how local installs stay unchanged. Grants and exemptions are still recorded.
- **Checkout:** `POST /api/billing/checkout {plan}` creates a session with:
  - `mode="subscription"` and the price.
  - `client_reference_id=stripe_ref`, with `metadata` and `subscription_data.metadata` both `{jh_ref}`.
  - `customer` from `billing_self` if known, otherwise `customer_email` from `pii_self`.
  - `success_url={PUBLIC}/billing.html?session_id={CHECKOUT_SESSION_ID}`, `cancel_url=…?cancelled=1`, `allow_promotion_codes=True`.

  The browser is then redirected. Card numbers never touch the app; only brand and last 4 are read, for admins.
- **Return check:**
  - `POST /api/billing/verify {session_id}` retrieves the session (expanding the subscription and its default payment method) and checks that `client_reference_id` matches. It then calls `apply_subscription(sub)`, stores `billing_self` and sets the customer's `metadata.jh_ref`.
  - `apply_subscription` finds the membership by `metadata.jh_ref` and updates `stripe_status`, `plan` (from the price's lookup key), `current_period_end` (taken from the subscription item, where recent API versions keep it), `cancel_at_period_end` and `billing_admin` (sealed to the APK public key, so no admin is needed).
- **Portal:** `POST /api/billing/portal` creates a portal session for `billing_self.customer`, with return URL `billing.html`. Plan changes, card updates and cancellation are configured in the Stripe dashboard. If `billing_self` was lost in a wipe, it is found again by searching Stripe customers by `jh_ref`.
- **Keeping status current without webhooks:**
  - The scheduler loop runs `billing.sync_tick()` every 6 hours: `subscriptions.list(status="all")`, auto-paginated, then `apply_subscription` for each one that has a `jh_ref`.
  - Each user is also synced on demand: when they open the billing page, or when the gate finds `current_period_end` has passed while the status is active or past_due. That happens at most once an hour, using `subscriptions.list(customer=…)` in their session.
- **Webhooks later:**
  - `POST /api/billing/webhook` returns 404 unless `STRIPE_WEBHOOK_SECRET` is set.
  - It verifies `Stripe-Signature` and handles `checkout.session.completed`, `customer.subscription.created|updated|deleted` and `invoice.paid|payment_failed`.
  - Every event fetches the subscription and calls the same `apply_subscription`, which is idempotent.
  - The 6-hour polling stays as a safety net.
  - The Stripe CLI (`stripe listen --forward-to http://127.0.0.1:8000/api/billing/webhook`) can test webhooks today, before the server is public.
- **Gate (`backend/access.py`):** `member = billing off OR admin OR exempt OR an active grant (starts_at ≤ now < ends_at) OR stripe_status in (active, trialing) OR (past_due and now < period_end + 3 days)`. `trial = not member and free_searches_used < free_searches`.

  | Feature | Allowed when |
  |---|---|
  | Viewing data, statuses, notes, uploads, pins, settings, account, billing, deleting the account | Always |
  | Run search (manual) | Member, or trial. A trial search uses one free search. |
  | Scheduled searches (and so digests) | Member and background access on. Free searches are for Run search only, so the default schedule doesn't use them up on day one. |
  | LLM features (score or rescore, chat, cover letters, title suggestions, research or Fill missing info, concise versions, summary) | Member, or trial. The run that uses the last free search still finishes its own scoring. |

  - **What counts as a "search":** one search run (Run search or a scheduled search), counted when it is queued (`search_runs.free_search=1`). A run that ends `failed` or is interrupted gives it back. Rescoring, research, chat, cover letters, title suggestions and importing saved pages don't count.
  - **Free periods and free searches:** free searches are not used up during a free period.
  - **Free period for someone already paying:** an admin grant also pushes back their next payment, with `subscriptions.update(sub, trial_end=grant_end, proration_behavior="none")`.
  - **Exempting someone who pays:** the admin is offered "Cancel their subscription at period end".
  - **Refusals:** blocked endpoints answer 402 `{"code":"membership_required"}`, and the frontend shows a banner linking to `billing.html`.
- **Test mode:** an `sk_test_` key shows a "Test mode" badge in the admin panel and on the billing page. Stripe test clocks can simulate renewals. Unit tests mock `StripeClient` and never use the network.

---

## 9. Email
- **Settings,** through `app_settings.ENV_RULES` and edited only in Admin → Server:

  | Variable | Rule |
  |---|---|
  | `JOBHUNT_PUBLIC_URL` | `https://host[:port][/path]`, or `http` only for localhost; no credentials, query or fragment |
  | `SMTP_HOST` | Must match `HOST_RE` |
  | `SMTP_PORT` | 1–65535 |
  | `SMTP_SECURITY` | `starttls`, `ssl`, or `none` (localhost only) |
  | `SMTP_USERNAME` | `[A-Za-z0-9._%+@-]{1,254}` |
  | `SMTP_PASSWORD_B64` | base64url; the app encodes any printable password, so the `.env` character rules stay strict; only a hint is ever shown |
  | `SMTP_FROM` | A plain email address; the display name is fixed as "JobHuntPA" |
- **Sending (`backend/mailer.py`):** stdlib `smtplib` and `email.message.EmailMessage`, a 20 s timeout and `ssl.create_default_context()`. Sends run on a separate single-thread executor, so a slow SMTP server can't hold up a request. Logs record user ids only, never addresses.
- **Templates (`backend/email_templates.py`):**
  - Small functions build HTML with `html.escape`, plus a plain-text part with the same content.
  - The base layout has `<html lang="en">`, a `<title>`, `<meta name="color-scheme" content="light dark">`, h1/h2/h3 structure, a single 600 px column, 16 px text with line height 1.5, and underlined links.
  - Colours are the Light theme's: text #12161d on #ffffff and links #3a2be0, all above 4.5:1. Meaning is never carried by colour alone. Layout tables have `role="presentation"`, and there are no images.
  - Templates: reset, invite, verify, security notice and digest.
- **Digest scheduler (`backend/digest.py`):**
  - `schedule.start_scheduler` gains extra tick functions: `digest.tick` (every 10 minutes), `billing.sync_tick` and `auth.cleanup_tick` (expired sessions and tokens).
  - `JOBHUNT_SCHEDULER=off` turns them off too, so a second copy sharing the same data doesn't send duplicates.
  - The tick does nothing unless SMTP and the public URL are set.
  - **Who gets one:** `digest_enabled`, a verified email, background access (`server_wrapped_dek`), and status active.
  - **Time zone:** taken from the user's first workspace's schedule state.
  - **When it's due:** at or after 07:00 local time, when `local_date(now) - local_date(last_sent_at or opted_in_at) >= digest_every_days`, and not already checked today (`last_checked_at`).
  - **Selection,** inside `crypto.as_user(owner, unwrap_with_KEK(...))`:
    ```sql
    SELECT j.* FROM jobs j JOIN search_runs r ON r.id = j.first_run_id
    WHERE j.workspace_id IN (:user_workspaces) AND r.scheduled = 1 AND r.started_at >= :cutoff
      AND j.duplicate_of IS NULL AND j.excluded_reason IS NULL AND j.hidden = 0 AND j.closed_at IS NULL
    ```
    `:cutoff = last_sent_at or opted_in_at`.
    - Sorted by fit score in Python, grouped by workspace, and capped at 60 per email ("and 23 more in JobHuntPA").
    - `first_run_id` is set in `pipeline.upsert_posting` from a `tasks.current_run_id` context variable.
  - **No new jobs:** nothing is sent; only `last_checked_at` is set.
  - **Sending:** `last_sent_at` is set only after a successful send. A failure is retried at the next due check, with no outbox table.
- **What each card shows,** from existing fields (the same logic as `job_to_dict` and `_fit_dict`):

  | Card element | Source |
  |---|---|
  | Heading | `title`; an assumption, see Q2 |
  | Company | `company` |
  | Salary range | `salary_min`–`salary_max` formatted as "$150k–$170k", else `salary_text` |
  | Office location | `office_text` if set, else `location_text` |
  | Office days | "N days in the office" from `office_days`, left out when unknown |
  | Work type | `work_mode` (remote, hybrid, onsite or unknown); see Q2 |
  | Fit summary | `evidence_json.summary` only, with no score, requirement points or evidence |
  | Gaps | "N gaps", using the frontend's own rule: `len(gaps)` or, if empty, the number of missing requirements |
  | "Open in JobHuntPA" | `{PUBLIC}/job.html?id={id}&ws={ws}` |
  | "View the original ad on SEEK" | `url`, with the source label from `sourceLabel` |

  The link text is clear in context, because each card is a section with its own heading (WCAG 2.4.4). The plain-text part lists the same fields.
- **Unsubscribe:**
  - **Token:** `t = unsubscribe_id + "." + b64url(HMAC(signing, "unsub|"+id))[:22]`, checked in constant time against the current and previous signing keys. No decryption is needed: `email_prefs` is plaintext.
  - **Headers:**
    - `List-Unsubscribe: <{PUBLIC}/api/email/unsubscribe?t=…>`
    - `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058)
    - `Auto-Submitted: auto-generated`

    The SMTP provider signs these with DKIM; the README explains the SPF, DKIM and DMARC setup.
  - **`POST /api/email/unsubscribe?t=`:** unauthenticated and exempt from CSRF. It accepts the RFC 8058 form body or JSON, sets `digest_enabled=0` and returns 200.
  - **The link at the bottom of each email** goes to `unsubscribe.html?t=…`. A GET never unsubscribes, because link scanners prefetch GETs; the page offers one button that POSTs, then "You're unsubscribed. [Subscribe again]" through `POST /api/email/resubscribe?t=`. It works without signing in. Access logs strip the query string.
- **Public URL:** used for every email link, the Stripe return URLs, the Origin allowlist and the Secure-cookie decision.
  - If it isn't set: digests and email resets are turned off. The admin panel says "Set the public address first", and invites show a link to copy instead.
  - Today it is the Tailscale serve HTTPS address. Later it becomes the Funnel or public domain.

---

## 10. Everything CLAUDE.md requires
- **`requirements.txt`.** These are the current PyPI releases as of today; implementers should test against them.
  ```
  # Encryption at rest (per-user keys, sealed columns and files, admin key pair)
  cryptography>=46,<52
  # Password-based key derivation (argon2id)
  argon2-cffi>=23.1,<26
  # Two-step sign-in: TOTP codes, and the QR code as SVG (pure Python, no Pillow)
  pyotp>=2.9,<3
  segno>=1.6,<2
  # Memberships (Stripe Checkout, customer portal, subscription sync)
  stripe>=16,<17
  ```
  Email uses the standard library. All of these have wheels for Windows, macOS and Linux x86_64/aarch64; on 32-bit Raspberry Pi OS they come from piwheels. The README recommends 64-bit Pi OS.
- **`.env.example`:** add a commented "Accounts and server (optional; admins set these in the app: Admin > Server)" block with:
  - `JOBHUNT_PUBLIC_URL` and the SMTP variables from section 9.
  - `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, plus a note on the lookup keys `jobhunt_monthly` and `jobhunt_yearly`.
  - `JOBHUNT_KDF_PROFILE=default|low`.
  - `JOBHUNT_KEY_FILE`.
  - `JOBHUNT_ADMIN_EMAIL` / `JOBHUNT_ADMIN_PASSWORD`, described as "process environment only, for CI or headless servers; don't store the password in .env".

  `ENV_RULES` gains only the editable variables (public URL, SMTP and Stripe).
- **Installer:**
  - No new prompts. Every new setting is optional, set in the app, and needed only for shared servers. `install_requirements` picks up the new packages.
  - `--non-interactive` doesn't write the admin variables.
  - The final message adds: "The first time JobHuntPA starts, your browser asks you to create your account."
- **`launch.py`:** opens `first-run.html#token=…` when `data/first-run.txt` exists; with `--no-browser` it prints that address. It passes `--proxy-headers --forwarded-allow-ips 127.0.0.1` explicitly and adds the access-log query filter. The shell scripts and Windows launchers need no changes beyond that.
- **Key file and backups:**
  - `data/server-keys.json` lives in `data/`, so the uninstaller backs it up and the Windows setup's `KEEP` set preserves it.
  - The uninstaller's backup message becomes: "It contains your encryption key file and API keys; keep it private. Restore by unzipping into a fresh install: the database and data/server-keys.json must stay together, and you still need your password."
  - Restoring keeps everyone's data: locked users with their passwords, background-access users through the KEK in the same backup.
  - `.gitignore` adds `data/server-keys.json` and `data/first-run.txt`.
- **README:**
  - The install sections get the first-run account step.
  - "Using it" covers accounts and that workspaces belong to each account.
  - New sections:
    - "Your account and how your data is protected": recovery codes, background access, what admins see, and the limits from 2.5.
    - "Sharing JobHuntPA (admins)": invites and sign-up mode, the admin panel, the public address, setting up SMTP and SPF/DKIM, Stripe products and lookup keys, test mode, and hosting on a Raspberry Pi behind `tailscale serve`.
  - "Updating": the one-time encryption step, backing up first, and that older versions can't be restored over it.
  - "Uninstalling": the backup includes the key file.
  - "Troubleshooting": forgot password, lost key file (`recover-lost`), last admin locked out (`new-admin-key`), bench-kdf on slow Pis.
  - "For developers": the bootstrap environment variables, the test KDF profile, the sealed-column rules and `tests/helpers.py`.
  - Each phase updates the sections it changes; Phase L does a final pass.
- **CI (`.github/workflows/ci.yml`):**
  - The `setup-unix` and `windows` jobs add `JOBHUNT_ADMIN_EMAIL: ci@example.invalid` and `JOBHUNT_ADMIN_PASSWORD: ci-test-password-123` to the job `env`.
  - **Unix launch check:** `curl -s -o /dev/null -w '%{http_code}' …/api/workspaces` must be 401. Then log in with `curl -sf -c jar -H 'Content-Type: application/json' -d '{"email":…,"password":…}' …/api/auth/login` and run `curl -sf -b jar …/api/workspaces`.
  - **Windows:** `Invoke-RestMethod -Method Post … -SessionVariable s`, then `-WebSession $s`.
  - **Restart check (Unix):** stop, start again and log in again. This exercises the key file, the fingerprint check and database sessions.
  - **Uninstall check:** `unzip -l ~/Documents/JobHuntPA-backup-*.zip | grep -q data/server-keys.json`.
  - The `tests` job is unchanged, apart from the tests themselves using `tests/helpers.py`.
- **Workspace scoping and merged duplicates:** all new job queries, for digests, admin counts and the free-search gate, use `current_workspace` or the owner's workspaces and skip `duplicate_of`.
- **LLM calls:** all still go only through `llm_providers`.

---

## 11. Phasing
Every phase must leave `python3 -m unittest discover -s tests -t .` passing, keep setup and launch working (CI green), update the README sections it touches and bump `VERSION`. All phases are rebased on d8eea88.

| # | Phase | Contents | Depends on | Can run in parallel with | Model and effort | Main tests |
|---|---|---|---|---|---|---|
| A | Prepare for multiple users and sealed columns (nothing visible changes) | Ownership-safe `owned()`; `/api/runs/{id}` scoping; `/api/companies` filtered to the workspace; `tasks` first-in-first-out queue per workspace plus `error_code`; context propagation helper; replace SQL on soon-to-be-sealed columns (source, `excluded_reason ""`/LIKE, DISTINCT); `list_jobs` omits rows in SQL; log hygiene (httpx level, access-log query filter, no labels or queries in logs); `Cache-Control: no-store` | — | B | sonnet, medium | Existing suite; runs not visible across workspaces; `list_jobs` output equal before and after; a log capture during a mocked search contains no marker company or URL |
| B | Crypto core | `backend/crypto.py` (envelopes, sealed types and their comparator guard, scope, blind index, sealed boxes, files, `submit`); `backend/keys.py` (key file, fingerprint, KEK wrap and rotation, pepper, signing, bench-kdf); argon2 profiles; requirements; `.gitignore` | — | A | **opus, high** | Round trips; wrong key gives KeyMismatch; AAD binding; legacy plaintext passthrough; comparator raises; key file 0600 and atomic; rotation re-wraps and survives a crash midway; copied context reaches the thread pool |
| C | Accounts and sessions | The account tables in §4; first run (token file and environment bootstrap) and workspace claiming (no data encryption yet); login and logout; database sessions with cookie-wrapped DEK; CSRF; rate limiting; `current_user`; ownership check in `current_workspace`; global settings admin-only; `login.html`, `first-run.html`; recovery codes shown at first run; `common.js` 401/402/423 handling and CSRF; account menu; `tests/helpers.py` with every test migrated; `launch.py`; CI login smoke and restart check | A, B | — | **opus, high** | Signup and login; wrong password and lockout; CSRF refusal; Origin check; another user's workspace gives 404; restart keeps the session; first-run token needed; environment bootstrap; global settings refused for non-admins |
| D | Encrypt user data and migrate | Sealed types on every table in §2.2; `url_index`/`careers_url_index`; sealed uploads; per-user commute cache; process-key http cache; `migrate_encryption.py` (backup, batches, check, VACUUM, report); 423 progress view; `secure_delete`; KeyMismatch becomes 404; tools/demo_data and readme_screenshots; benchmark tool | C | — | **opus, high** | Grep of raw DB, WAL and uploads bytes finds none of the planted markers; migration interrupted midway resumes and finishes; plaintext audit is zero; cross-user isolation; scorer thread pool works under scope; benchmark |
| E | Rest of the auth flows | MFA (pyotp and segno), recovery-code login and keep-data recovery, change password, regenerate recovery codes, sign out everywhere, delete account, theme in `prefs_enc`, Account cards on the Settings tab | D | F, G (G's UI waits for E) | opus for the backend, sonnet for the UI | TOTP replay refused; the recovery code is used up; recovery keeps the data; a locked wipe deletes everything; deletion cleans up files and sessions |
| F | Admin panel core | `admin.py` and `admin.html`: stats (adding `llm_usage.user_id`), users with their details, sign-up mode and invites plus `signup.html`, promote and demote with APK grants and rotation, re-grant, disable, MFA off, delete, audit log, plaintext-files report, KEK rotation button | D | E, G | sonnet, medium; **opus for the APK grant and rotation code** | The stats SQL; admin responses contain no content fields; promote then demote rotates the APK and the remaining admins can still read details; `new-admin-key` self-heals details at the next login |
| G | Background access | Turning it on and off, the scheduler using owners' keys (no key or locked means the slot is passed over with a reason), the "keep data" hook for email resets, the Account card and its explanation | D | E, F | opus, medium | A locked user's scheduled slot is passed over; an opted-in user's runs; turning it off stops runs |
| H | Email | SMTP and public-URL settings, mailer, templates, email verification, forgot password by email (wipe or keep), admin reset button, invite emails, security notices | E, F, G | I | sonnet, medium | `ENV_RULES` validation (injection cases); the reset token is single-use and expires; MFA required during reset; the template's HTML and text parts |
| I | Memberships and Stripe | `access.py` gate and 402 responses; free searches with refunds; grants and exemption; admin membership UI; `billing.py`; `billing.html`; sync tick; webhook route (off until a secret is set); test mode | F | H | sonnet, medium; opus reviews `access.py` | The gate's truth table; a free search is refunded when a run fails; scheduled searches need membership; `apply_subscription` from a mocked Stripe; a free period for a subscriber sets trial_end |
| J | Digests | `jobs.first_run_id`, `digest.py`, unsubscribe endpoints and page, digest preferences UI, scheduler integration | G, H (I optional) | K | sonnet, medium | Selection skips duplicates, excluded, hidden and closed jobs; due-date logic; a mocked SMTP capture has both headers; one-click POST works with no session; a GET doesn't unsubscribe |
| K | Sign-up wizard | `welcome.html` and `welcome.js`; `onboarding_step`; `/api/geo/lookup`; the schedule and digest toggles appear only when the feature is available | E (recovery codes, theme) | F through J | sonnet with the `frontend-design` skill, medium | Each step saves through the API; wizard resumes where left off; Playwright and axe run once by hand |
| L | Hardening and documentation | CSP with a hash for the inline theme script, frame-ancestors none, nosniff, Referrer-Policy; WCAG pass (axe on every new page, keyboard, 320 px, 200% zoom); `/security-review`; final README; proposed CLAUDE.md rules | all | — | opus for the security review; sonnet (or haiku) for the README | Every header present; a log-leak test across a full run |

**Worktrees that won't conflict:**
- A and B can run side by side.
- E, F and G can run side by side once D is merged. E and G both touch the Account section of `index.html` and `app.js`, so G's UI should merge after E.
- H and I can run side by side. Both add `ENV_RULES` entries and admin-panel sections; the merges are trivial.
- K can run alongside F through J.

---

## 12. Open questions (only these three) and the defaults I chose

**Questions:**
1. **Email resets for users with background access keep their data,** because the server holds their key. With MFA on, the TOTP code is still required. A strict reading of "a reset without the recovery code wipes" would wipe these users too. Default: keep the data.
2. **What the digest cards show:**
   - **Work type:** default is remote, hybrid or onsite. Employment type (full-time or contract) isn't stored today; it would need changes to the adapters.
   - **Job title:** default is to show it as the card heading, although your list didn't name it.
   - **Score:** default is to leave it out.
3. **LLM features after the free searches run out:** default is that rescoring, chat and cover letters are blocked until the person becomes a member. The jobs found stay visible.

**Defaults I chose without asking:**
- Sign-up is invite-only, as an admin setting (invite, open or closed).
- 3 free searches. Scheduled searches need membership and background access.
- Background access is off for invited users and on, suggested, for the first admin.
- 8 recovery codes of 80 bits each. They work both instead of the authenticator app and to reset a password without losing data.
- Sessions: 24 h idle (30 days with "Keep me signed in"). Re-authentication within 15 minutes for sensitive actions.
- argon2id at 64 MiB, t=3, p=4, with a `low` profile.
- AES-256-GCM, one data key per user, per-column sealing.
- Schedules, company profiles, the geocode cache and llm_usage stay plaintext.
- Digests are sent around 07:00 local time and skipped when there are no new jobs.
- Stripe prices are found by lookup key, with a 3-day grace period when a payment fails.
- The admin panel is a separate page.

### Critical Files for Implementation
- /home/florian/Projects/JobHuntPA/backend/db.py
- /home/florian/Projects/JobHuntPA/backend/workspaces.py
- /home/florian/Projects/JobHuntPA/backend/app.py
- /home/florian/Projects/JobHuntPA/backend/tasks.py
- /home/florian/Projects/JobHuntPA/backend/schedule.py
- /home/florian/Projects/JobHuntPA/backend/app_settings.py
- /home/florian/Projects/JobHuntPA/frontend/common.js
- /home/florian/Projects/JobHuntPA/installer/launch.py
- /home/florian/Projects/JobHuntPA/.github/workflows/ci.yml

New modules: `backend/crypto.py`, `backend/keys.py`, `backend/sessions.py`, `backend/auth.py`, `backend/account.py`, `backend/admin.py`, `backend/access.py`, `backend/billing.py`, `backend/mailer.py`, `backend/email_templates.py`, `backend/digest.py`, `backend/migrate_encryption.py`, `backend/ratelimit.py` and `tests/helpers.py`. New pages under `frontend/`: `login.html`, `signup.html`, `first-run.html`, `forgot.html`, `reset.html`, `welcome.html`, `admin.html`, `billing.html` and `unsubscribe.html`.
