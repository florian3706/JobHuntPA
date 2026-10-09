# JobHuntPA design: user accounts, per-user encryption, admin panel, Stripe memberships and email digests

**Base.** `main` at 48fb435, plus Phase 1 of the documents work (d8eea88 on branch `worktree-agent-a3d7c32a94762a7dd`). Phase 1 adds notes and summaries as `Document` rows (`filetype` "note"/"summary"), `concise_text`, `use_concise`, `summary_of`, `updated_at`, the "preferences" kind and `llm_usage`. Rebase every phase below on it.

**Threat model.** The goal is that someone holding only the database, its backups or the `data/` folder cannot read a user's searches, documents, jobs or personal details. This holds even if that person is the operator browsing with `sqlite3`, as long as the user has not opted in to background access. Personal and billing details of other people (the admin details key, §1.4) stay closed even to someone who holds the database **and** the key file, because the key that opens them is never reachable through the server key (the identity key, §1.2).

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
| Associated data (AAD) | Columns use `b"jh/" + table.column`, set automatically by an `after_parent_attach` event so values can't be swapped between columns. Key wraps use their own labels, for example `pw\|{uid}`, `rc\|{uid}\|{code_id}`, `sess\|{sid}`, `srv\|{uid}\|{kek_id}`, `grant\|{uid}\|{apk_ver}`, `totp\|{uid}` and `file\|{stored_name}`; the identity key's (§1.2): `pw-ik\|{uid}`, `rc-ik\|{uid}\|{code_id}`, `sess-ik\|{sid}`, `pend-ik\|{sid}`, `ik-priv\|{uid}` and `pending-codes\|{uid}`. |

### 1.2 Hierarchy
```
password ─argon2id(salt, params)─► master ─HKDF "auth"─► pw_verifier (stored, compared in constant time)
                                         └HKDF "kek"──► KEK_pw ──wraps──► DEK
recovery code i (80-bit, 8 codes) ─HKDF(user salt)─► KEK_rc_i ──wraps──► DEK
session cookie secret (256-bit) ─HKDF─► KEK_sess ──wraps──► DEK            (one wrap per session row)
server KEK (key file) ──wraps──► DEK                                       (background opt-in users only)
DEK ──► AES-GCM for columns and uploaded files
    └HKDF "index"──► blind-index key (jobs.url_index, company_sources.careers_url_index, commute cache keys)

identity key IK (32 random bytes per user; protects ONLY the X25519 private key and the unconfirmed recovery codes):
    KEK_pw ──wraps──► IK                       (pw-ik|{uid})
    KEK_rc_i ──wraps──► IK                     (rc-ik|{uid}|{code_id}, one per code made after the IK existed)
    KEK_sess ──wraps──► IK                     (sess-ik|{sid}; pend-ik|{sid} while pending_mfa)
    server KEK ──✗──► IK                       (never: background access does not open the IK)
IK ──wraps──► the user's X25519 private key    (ik-priv|{uid})
Admin PII key pair (APK): public key in app_settings; private key sealed to each admin's X25519 public key
key file: pepper ─► email_hash = HMAC-SHA256(pepper, normalised email)
          signing ─► unsubscribe tokens
process-only random key (never on disk) ─► http_cache
```

**The identity key (IK).** A second random key per user, made with the account, whose only job is to protect the X25519 private key (the one that opens an admin's grant of the APK, §1.4) and the recovery codes shown once and not yet confirmed (`pending_codes_enc`, because a code opens the IK).
- **Why.** The server KEK opens the DEK of every background-access account. If the private key were sealed under the DEK, anyone with `data/` (the database and `server-keys.json`: exactly the uninstaller's backup) could go server KEK → DEK → private key → grant → APK and read every user's personal and billing details. The IK has no wrap the server can open, so that chain stops at the private key. Everything the server does while nobody is signed in (scheduled searches, digests, Stripe sync, sign-ups) needs the DEK or only public keys, never the IK.
- **Where it is wrapped:** under the password key (`user_keys.pw_wrapped_ik`), under each recovery code's key (`recovery_codes.wrapped_ik`) and under each session's cookie-derived key (`sessions.wrapped_ik`, also while `pending_mfa`). **Never under the server KEK.** A password change re-wraps it with the DEK; a recovery-code reset opens it with the code.
- **Use.** A signed-in request's key scope carries a way to open its session's wrap (`crypto.as_user(..., ik_opener)`); `crypto.open_ik()` opens it afresh each time and nothing keeps the result. Only `admin_key.open_apk` asks for it. A background run's scope has none (`NoIdentityKey`): `tasks.start` builds the worker's scope again without the request's opener, so a run queued by a signed-in admin can't open the IK after the request ends.
- **A reset that can't open it makes a new one.** A reset by email that keeps the data (background access) can open the DEK but not the IK, so the person gets a new key pair and a new IK; an admin loses their grant (audited as `admin_key_lost`, and the page says so) and another admin shares the key again with the usual "Share the admin key again". A reset that wipes the data always makes new keys, as before.
- **Accounts from before the IK** have the private key sealed under the DEK (`pw_wrapped_ik` is NULL). There is no start-up step, because the IK is wrapped under the password: at the account's next password sign-in (or recovery-code reset, or any check of the password: changing it, making new codes) `users.unlock` makes an IK, re-wraps the private key and the unconfirmed codes under it, wraps it under the password key, overwrites the DEK-sealed copies and scrubs the write-ahead log. Until then an admin's arrangement is the old one, and nothing new is sealed to it: promoting such an account, sharing the key with it again, or a new key skips it (its private key is under the DEK, which an old copy of `data/` may open with the server key, and nothing records whether background access was ever on): `admin_key.NeedsSignIn`, a 409 "Ask them to sign in once first, then share the key.", and `admin_key_not_shared` in the audit log. **The migration makes a new key pair** (`users.migrate_identity`; it does not reuse the private key, because a copy of the DEK-sealed form in a backup, with the DEK from the server key, would still open it): the new private key is wrapped under the IK, an admin's grant is opened with the old private key and the key is **replaced** (`admin_key.new_key`, audited as `admin_key_replaced`: everything sealed to it is moved, every admin who can be given it is, the rest wait for a share), so a grant in an old copy opens a key that protects nothing written from then on. A grant that doesn't open is deleted (`admin_key_lost`). All of it is one transaction with the row changes. The recovery codes the account has not yet confirmed saving are in hand at that point (`pending_codes_enc`), so they get the IK too (`recovery_codes.wrapped_ik`), unless the account has background access: the server key opens the DEK that holds their text, so a code that opened the new IK would let an old copy of `data/` reach it, and those codes stay without (like the confirmed ones). Turning background access on is refused (409 `needs_sign_in`) until the account has an IK. The codes already confirmed can't (the plaintext is gone): a reset with such a code keeps the data but gives the account a new key pair (an admin needs the key shared again; `replace_identity` also drops unconfirmed codes still under the old IK, which nothing could open), and Settings suggests making new codes to admins whose unused codes predate the IK (`recovery_codes_old` in `GET /api/auth/security`). A reset with one of those codes by an account that has no IK yet loses nothing: the code opens the DEK, which still opens the private key. Sessions made before the IK have none; the admin panel then says "Sign in again to open people's details" (`admin_key_needs_sign_in`), until a password check in that session (`security.confirm_password`: confirming it, changing it) hands the IK it just opened to the session (`sessions.give_ik`).

**DEK lifetime.** The DEK is never rotated in place, because that would mean re-encrypting every row. It is replaced only when a password reset wipes the data. Random-nonce limits are not a concern: one user would need billions of encryptions to approach them. The `kid` and version byte would allow lazy re-encryption if it were ever needed.

### 1.3 The server key file, `data/server-keys.json`
- **Format.** Mode 0600, written atomically (temp file in the same folder, then `os.replace`, the same approach as `app_settings._replace_file`). `JOBHUNT_KEY_FILE` can point elsewhere, mainly for tests. Contents:
  `{"version":1,"kek":{"current":"k1","keys":{"k1":b64}},"pepper":{"current":"p1","keys":{"p1":b64}},"signing":{"current":"s1","keys":{"s1":b64}},"created_at":…}`, plus `"encryption_done_at"` once the one-time encryption of the data from before accounts has finished (written once, never removed).
- **`encryption_done_at` and strict mode.** Strict mode (§2.1, refusing plaintext in a sealed column) turns on at every start when the key file says the encryption is done, or the database does (`backend/migrate_encryption.is_done`; `db.encryption_done` reads both). The file is the one that counts: the database's `app_settings["encryption_migration"]` could be set back to `pending` by someone who can only write the database, and the next run would then seal whatever plaintext they planted as if it were real. With the marker set, a run is refused (`MigrationError`), start-up puts the database's state right, and the planted value stays plaintext, which strict mode refuses to read. An install from before the marker gets it at its next start (the database says done). `recover-lost` carries it into the new file. Restore the key file and the database as a pair: an older database with a newer key file is read as encrypted and its plaintext is refused.
- **One process at a time.** The app takes an exclusive lock on `data/server-keys.json.lock` at start-up (`keys.hold_key_file`: `fcntl.lockf`, `msvcrt.locking` on Windows) and keeps it until it stops; `rotate-kek`, `new-admin-key` and `recover-lost` take it before they change anything and refuse while someone else holds it, and the app refuses to start under a running command. This holds however the app was started; `data/running.json`, which only the launcher writes, stays as a second check. A command that replaced the file under a running app would leave it wrapping with a retired key (every two-step sign-in and background search failing until a restart). `ServerKeys.kek_wrap` also compares the file on disk with what the process loaded and refuses when they differ. The lock file is left out of the uninstaller's backup.
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
- **Reading.** In an admin request: the session's identity key (§1.2) → their X25519 private key → open their grant → APK private key. This is repeated for each request; nothing stays in memory. The admin's DEK does not open the private key, and the server KEK opens only the DEK, so **the key that opens the APK is usable only while that admin is signed in**, however their background access is set. `recover-lost` runs the same chain with the password (password → IK → private key → grant), and reads the older DEK-sealed form for an admin who hasn't signed in since the IK was introduced.
- **Making someone an admin.** The promoting admin (logged in, so they have the APK private key) seals it to the new admin's public key. Every user gets a key pair at sign-up, so this works without the new admin being present.
- **Demoting, deleting or turning off an admin.** The APK is rotated automatically: a new key pair is made, every admin-sealed value is decrypted and re-sealed to the new public key, and the remaining *active* admins get new grants (`admin_key.active_admin_ids`). An admin who is turned off is no longer trusted with what is written from now on, so turning one off rotates the key like demoting does, and they are given no grant of any later key. Only the rotation needs the acting admin's own copy: an admin without one can still turn the other off (the emergency brake: they are signed out and their grant deleted), and the answer says the key was not replaced (`apk_rotated` false, audited as `admin_key_not_replaced`); an admin who has the key then replaces it, or `new-admin-key` does. Turning them on again shares nothing: another admin uses "Share the admin key again" (`POST …/regrant`, refused for an account that is off). A database from before this rule may still hold a disabled admin's grant; the next rotation drops it, as does turning them off again.
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
- **`SealedAlways` (added by §14.6)** is the variant for columns whose null-ness is itself personal: `jobs.hidden`, `jobs.excluded_reason`, `jobs.location_verdict`, the office override and the own-content columns. It writes a sealed marker for NULL and opens it back to `None`. Its comparator refuses `IS`/`IS NOT` too, so the code decides these in Python.
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
| postings (§14) | — | Every column. Public ads as scraped, shared by all users, plus pool state (first and last listed, closed and why). |
| jobs, now the per-workspace link (§14) | status, hidden\*, excluded_reason\*, distance_km, lat, lng, location_verdict\*, office_text\*, office_lat\*, office_lng\*, office_source\*, office_checked_at\*, last_seen, and the own-content columns\* (source, external_id, company, title, location_text, country, work_mode, employment_type, office_days, salary_min, salary_max, salary_text, description, description_hash, detail_status, detail_fetched_at, posted_at, industry, closed_at: a private job's content, or this workspace's overrides such as a privately uploaded full ad). A private job's `url` is sealed too, found through `url_index`. \* = `SealedAlways` (§2.1): null-ness doesn't show. | id, workspace_id, posting_id and a pool link's url (the user decided the operator may see which postings a user has, §14.6), origin, source_id, duplicate_of (filtered in SQL by the CLAUDE.md rule), first_seen (link ids and run times reveal it anyway), posting_rev. New: `url_index` (blind index, unique with workspace_id) and `first_run_id` (for digests). |
| fit_results | score, evidence_json, status, error | job_id, profile_hash (a hash of high-entropy text, not reversible), model, created_at |
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
- **`pipeline.job_by_url`** (the only SQL comparison on `jobs.url`, used to find a workspace's link): `Job.url == url` becomes `Job.url_index == blind_index("url", url)`.
- **Closing jobs no longer listed:** since §14 this works on the plaintext `postings` table (`pool.close_unlisted`, `pool.sweep`), so sealing changes nothing there.
- **Personal link fields stay out of SQL (§14.6):** `hidden`, `excluded_reason`, `status`, `fit_results.status`/`score` and the own-content columns are decided in Python since Phase A2. Nothing to change in Phase D.
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
- **Cost (superseded by §14.6).** Before the job pool, `list_jobs` decrypted about 25 sealed columns per job. On a Pi 4 (no AES instructions) a small value costs roughly 4–8 µs including Python overhead, so 5,000 jobs would have added about 0.5–1 s.
- **Since §14:**
  - Postings are plaintext. A link carries about 15 sealed values, mostly NULL markers.
  - The old SQL shortcut (`excluded_reason IS NULL AND hidden = 0`) is gone: those two columns are `SealedAlways`.
  - `list_jobs` first loads `(id, hidden, excluded_reason, duplicate_of)` for the workspace's top-level links (about 50 ms for 5,000 links), decides in Python, then loads only the listed links in full (about 75 ms for 1,000) and their merged copies with `duplicate_of IN (...)`.
- **Benchmark.** Add `tools/bench_jobs_list.py`. Target: under 1.5 s for 5,000 jobs on a Pi 4.
- **Fallback if it fails:** seal the card fields as one JSON blob per row (`jobs.card_enc`). Simplicity: per-column first.

### 2.5 What still leaks, and the real limits
**Metadata the operator can see:**
- Row counts per workspace and per user.
- Ciphertext lengths, which equal plaintext length + 33 (for example the length of a CV).
- All timestamps: when jobs were seen or closed, when runs, chats and sign-ins happened. `last_active_on` is stored by day only.
- Whether each sealed column is null, except the `SealedAlways` ones (§2.1). Whether a job is hidden or excluded, has a verdict, an office override or a privately uploaded full ad doesn't show; the ciphertext length of the own description still hints at an upload.
- The plaintext flags: document kind and filetype, pin kind, use_for_scoring, run kind and state, and model names.
- A workspace's own merges and splits of duplicate ads (`jobs.duplicate_of`, `job_duplicates.status` and `decided_by`).
- Schedules, including the Australian state.
- Membership status, plan, period and free searches used; role, MFA on or off, background access on or off; failed-login counters.
- Shared caches: which companies were researched (not by whom) and which places were geocoded.
- llm_usage token counts per user and purpose.
- Uploaded file sizes.

**Job postings (§14):**
- **What the operator can see:** the postings are public ads in a shared plaintext pool. The user decided the operator may see which postings each user has: every link names its posting in plaintext. That also shows roughly what a user is searching for, and when each job was first linked.
- **What the operator can't see** is what the user thinks of a posting or did with it:
  - status, hidden, whether and why it's excluded, fit score and evidence;
  - distance, office and location verdict;
  - a privately uploaded full ad, cover letters and chats.
- **Private jobs**, imported from a saved page and not in the pool, are fully sealed, URL included.
- **Searches and pool matching:** a posting's `first_seen` and `last_listed_at` show when some search listed it. Pool matching shows each user postings that other users' searches found, but only those matching the user's own titles or keywords.

**The LLM provider** sees resumes, jobs, chats and documents in plaintext during scoring, chat, cover letters and research, under the operator's API account and the provider's retention policy. Prompt-cache keys sent to providers are hashes of the user's documents.

**The running server** holds plaintext in memory while it serves a request or runs a background job, and memory can reach swap or core dumps.

**An operator who changes the code**, proxies traffic before TLS terminates, or reads process memory can capture passwords and data at the next login. Encryption at rest cannot prevent this.

**Users with background access:** the operator can decrypt their data (searches, documents, jobs, `pii_self`) using the key file. What this does **not** reach is the admin details key: the private key that opens an admin's grant is under their identity key, which has no wrap the key file opens (§1.2). So the personal and billing details of all users (`pii_admin`, `billing_admin`, grant notes, audit details) stay closed to someone with `data/`, even when an admin has background access, until an admin signs in. What remains: an admin who has not yet signed in since the identity key was introduced still has the private key under their DEK (migration, §1.2; nothing new is sealed to such an account, and the migration replaces the key pair and the admin key); **old copies and leftover files**: a backup from before the migration (`*.bak-before-encryption`, `*.bak-before-job-pool`, the uninstaller's zips) holds the old DEK-sealed private key and grant, which with the DEK from the key file open the admin key *as it was then* (and everything written before the key was replaced), never what was written after; the README and the Server section tell admins to delete them; and an operator who changes the code or reads process memory can take the key from a signed-in admin's requests; the recovery codes an admin has not yet confirmed saving are under the IK, not the DEK.

**Third parties:**
- The email provider sees digest contents (job titles, companies and fit summaries).
- Stripe sees the email address and payment details.

**Sign-in timing and the mail thread.** Refused sign-ins are held to a floor (§3.6), so an unknown address and a wrong password take the same time to within a few milliseconds. "Forgot password" answers 202 either way and queues the same kind of job on the mail thread, a decoy as long as a typical send for an address with no account, so the delay of the next email shows nothing either. What remains: the delay is an average, so a very long or very short real send can still show, and sign-up answers "already exists" for an existing address anyway, so this is hardening of an already-open door.

**Wrong tries are forgotten on restart and spread over many addresses.** They are counted in memory (§3.6). Restarting the server (or a power cut) gives every source its five tries back. A guesser with many network addresses gets 5 tries per address up to the account-wide ceiling of 100 an hour; IPv6 addresses count one by one, so a whole /64 can rotate through the ceiling. The owner's usual browsers are exempt from the ceiling, a brand-new device is not. Wrong two-step codes have their own, lower ceiling (10 an hour from sources that aren't a known browser) and, on a reset link, 5 per link (§6.5).

**What someone who can WRITE the database can do (not only read a copy).** These columns are not authenticated by any key the writer lacks:
- The plaintext lookup and state columns: `email_hash`, `mfa_enabled`, `role`, `status`.
- The public keys: `user_keys.x25519_pub` and `app_settings.apk_public`/`apk_version`.

A writer can therefore:
- **Redirect a password link** by rewriting a victim's `email_hash` to their own address and asking for a link. For an account **with background access** this does not work: the reset keeps the data, so it checks that the link's address is the one in the account's own sealed details (`pii_self`, which a writer can't forge without the account's key) and refuses with 410 `link_invalid`. For an account **without** background access the reset deletes the data (the owner accepted that) and the writer ends up with an emptied account: their data is lost, never disclosed.
- **Not mint a link out of nothing.** A reset token carries the server key's signature of itself and the account id (`password_reset.link_token`), so a row added by hand, or a real link moved to another account, has no token that verifies (tests/test_password_reset.py `WriterTest`). Other single-use links (email checks, address changes, invitations) are not signed and are covered only by this section: a writer can mint them.
- **Receive the admin details key** by replacing an admin's `x25519_pub` (or `apk_public`) and then waiting for a promotion, regrant, rotation or sign-up, which seals the key (or new details) to the swapped public key. That exposes only admin-visible personal details (names, emails, notes, billing details), never searches, documents or jobs. Hardening for later: a `pub_mac` under HKDF(signing key, "jh pubkey v1") checked in `grant_to`, `admin_public_key` and `heal_details`.
- Set an account's `role`, `status` or `mfa_enabled`, or give it a membership (`memberships.stripe_status`, `stripe_mode`, `current_period_end`, `exempt`: the gate reads them in plaintext and nothing authenticates them); turning two-step off for an account whose secret is in the file lets them past that step but not past the password.

This is the same class as the plaintext-swap limit in §15 and needs write access to the database file or SQL, which none of a signed-in user, a stolen session, a read-only backup or an outsider has. Someone with both the database and the key file can do anything the server can.

**Who controls the mail route.** The mail server and the public address decide where every reset link goes, so whoever sets them receives the links. An admin who points them at their own server could therefore reset any account that has no two-step sign-in (and, with background access, keep its data) without the person seeing an email, and could first turn two-step off for one that has it. Guarded, not removed:
- Saving a change to `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` or `PUBLIC_BASE_URL` (once mail was set; the first set-up is not a change) sends a priority notice to every other active admin **through the old server** (`mail_route.notify_admins`, with the old `SmtpConfig`, before the new settings are saved), and records the time (`app_settings.mail_route_changed_at`). Their addresses are sealed to the admin key, so an admin without a copy of it is refused (409 `admin_key_missing`) while other active admins exist, and nothing is saved: the one detection step can't be skipped. An admin whose own details are stale (sealed to an older key) can't be read and gets no notice; that is recorded in the audit log (`mail_route_notice_partial`), not refused, so settings stay editable until they sign in again.
- For 24 hours after that, `POST /api/admin/users/{id}/mfa/disable` and `.../reset-email` answer 409 `mail_changed_recently`, for every admin. The person can still use a recovery code.
- Not covered: the anonymous "forgot password" form, which an admin can drive with any address. For an account with two-step sign-in the code is still needed (the hold stops the admin turning it off). For one without, the operator who controls the mail can take the account; that is the same trust as the operator of §2.5's "An operator who changes the code". Turn on two-step sign-in to close it.

**The write-ahead log.** `secure_delete` zeroes freed pages in the main file; the log (`-wal`) keeps old page images until a checkpoint truncates it. `scrub_wal()` runs after each commit that drops a key copy (a spent recovery code included) but a reader open at that moment blocks it. A blocked scrub is remembered and tried again at start-up and by the clean-up tick (`scrub_wal_again`, every 10 minutes), and `PRAGMA journal_size_limit=0` cuts the log file whenever SQLite resets it. Until one of those happens the old key copy can sit in the log, as it can on flash storage.

**Who can read the files.** The data folder is made 0700 and the database with its `-wal` and `-shm` 0600 (`db.owner_only`, at every start; an existing install is tightened), the plaintext backups (`*.bak-before-encryption`, `*.bak-before-job-pool`) are created 0600 before the first byte is written, and the key file and the uninstaller's zip were already 0600. Other accounts on the same computer can't copy the wrapped keys and password verifiers to guess passwords offline. On Windows this relies on the folder's permissions. Root and the owner can still read everything.

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
   - The row also holds `wrapped_ik` (label `sess-ik|{sid}`, `pend-ik|{sid}` while pending). The middleware does not open it: it gives the request's scope a function that does (`crypto.session_ik_opener`), and only the admin details chain calls it, once per use (§1.2). A session without one asks the admin to sign in again.
2. **Cookie:**
   - Name `__Host-jh_session` with `Secure` when the request is HTTPS, and with `_<port>` after it when the Host has a port (`__Host-jh_session_8443`). Browsers keep cookies per host name and ignore the port, so a test instance on `:8001` next to the live app on `:8040` would otherwise overwrite each other's cookie and sign each other out. A port in the name stops that; it does not stop another app on the same host name from receiving the cookie, so JobHuntPA should have a host name of its own. HTTPS means `request.url.scheme` after uvicorn's proxy headers (`--forwarded-allow-ips 127.0.0.1` is the default, and Tailscale serve connects from loopback), or a Host matching an `https` `PUBLIC_BASE_URL`.
   - On plain `http://127.0.0.1` (local installs; Safari rejects Secure cookies there) the name is `jh_session` (`jh_session_8040` with the port). Existing sessions end once when this changes, and people sign in again.
   - A second cookie, `jh_device`, marks a browser that has signed in to an account (item 6). Same naming rules; `SameSite=Strict`, one year.
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
6. **Rate limiting and lockout** (`backend/ratelimit.py`, `backend/lockout.py`; in memory, the app is a single uvicorn process):
   - **Per IP** (the client address from X-Forwarded-For on loopback): auth endpoints allow 10 requests per 5 minutes and 60 per hour. Unsubscribe allows 30 per minute. This is only as good as the proxy's header: `tailscale serve` and other proxies on this computer **append** the real client address, which uvicorn then reads; a proxy that passed a client's own `X-Forwarded-For` through unchanged would let anyone pick their address. Behind the Host allowlist (§18) a web page on another site can't reach the server to forge one, so only programs on this computer can.
   - **Wrong passwords and codes** are counted per account **and per source**, never for the account as a whole:
     - A source is the client address, or, for a browser that has signed in to the account before, that browser (the signed `jh_device` cookie, bound to the account and to its password salt, so a password change or reset makes every copy useless).
     - After 5 failures in a row from one source, that source waits 30 s, doubling with each further failure up to 15 minutes. There is no permanent lockout. Failures are forgotten after a day without a try or at the next right password from that source.
     - The try is **counted before the slow password check** (one step under a lock) and given back if the password was right, so tries sent at the same moment can't all slip past the 5: of 10 parallel wrong passwords, 5 get "wrong password" and 5 get "wait". A right password at the password step of an account with two-step sign-in gives its try back; wrong codes at the next step are counted on their own.
     - Why per source: a lock for the whole account lets anyone who knows an address keep its owner out (4 requests an hour was enough, and the way out, recovery and reset, was locked too). Now an outsider locks only their own address.
     - **Account-wide ceiling:** 100 wrong tries an hour from any source slows a guesser who rotates addresses. It holds up browsers new to the account, with a plain message; it never holds up a browser that has signed in to the account (so someone with a botnet can keep strangers' *first* sign-in on a new device waiting, but not the owner's usual browsers). It does not apply at the steps that need a proof only the owner has: the code after the password, a recovery code, the code during a reset by email (the link is the proof of the mailbox). Those steps do have the **code ceiling** (`ratelimit.code_guesses`, `lockout.begin_code`): 10 wrong codes an hour from sources that are not a browser the account signed in from, whatever the number of addresses, because a code has only a million values and a link or a recovery code is not used up by a wrong guess. It covers the code on a reset link, the second proof in `/recover`, and turning two-step off, changing the address and making new codes. A reset link is also used up (410) by its 5th wrong code, and a recovery code typed first in `/recover` is used up by its 5th wrong second proof, so the person asks for a new link. The app's code at sign-in has the code ceiling too (a recovery code typed there does not), and each pending session allows 5 tries (`sessions.MFA_ATTEMPTS`).
     - Wrong tries are no longer written to the database (`users.failed_logins` and `locked_until` are unused; a restart forgets them). That also removes the extra database write only real accounts made on a wrong password, which showed in the timing.
     - An admin's "unlock" forgets all of an account's counts; a password change or reset does too. The admin list shows "waiting after wrong passwords" while any source of an account waits.
   - Responses are 429 with `Retry-After` and a plain message.
   - Email addresses with no account are counted the same way (a separate store, so made-up addresses can't push real accounts out), so answers can't tell the two apart. Unknown emails still run a dummy argon2 derivation, and **every refused sign-in or recovery waits for a floor**: the slowest of three password derivations measured at start-up for the KDF settings in use, plus 50 ms. A real account's work (stored KDF settings that differ from the profile, a lookup that rewrites the email hash) can't then be told from an unknown address by its time.
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
- `x25519_pub`, `x25519_priv_enc` (the private key wrapped under the IK, `ik-priv|{uid}`; sealed under the DEK in accounts from before the IK), `pw_wrapped_ik NULL` (NULL: no IK yet).
- `recovery_salt`, `pending_codes_enc NULL` (IK: recovery codes still to be shown once; DEK in accounts from before the IK).

**recovery_codes:** `id`, `user_id` (CASCADE), `wrapped_dek`, `wrapped_ik NULL` (NULL for a code made before the IK), `created_at`, `used_at NULL`. Eight codes.

**sessions:** `id TEXT PK` (sid), `user_id` (CASCADE), `state` ('pending_mfa'|'active'), `wrapped_dek`, `wrapped_ik NULL` (NULL for a session made before the IK), `persistent`, `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `reauth_at`.

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
- jobs (the per-workspace link since §14, which added `posting_id`, `origin` and `posting_rev` and the shared `postings` table): `url_index`, `first_run_id`.
- company_profiles (§14.7, Phase A2): `sentiment_researched_at`, `news_researched_at`.
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
- App-wide settings become admin-only through a `require_admin` dependency: `/api/settings/*` (LLM and commute keys), `/api/llm/*` (reasoning levels) and `/api/score/test`. This covers reading as well as writing: ordinary users can't see them (§14.8), and `/api/score/status` and `/api/commute/status` tell them only whether the features are available.

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
5. **After the run:** the completion screen lists leftover plaintext files: the new backup, older `jobhunt.db.bak-*` files and `data/cache/*.html`. The dashboard shows admins a notice that links to the admin panel's Server section, where the list has "Delete these files now" (it asks for the password) and keeps showing a warning until they are gone.
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
   - Creates the user, DEK and IK and their wraps, key pair, 8 recovery codes (each wrapping the DEK and the IK) in `pending_codes_enc` (under the IK), membership (3 free searches when payments are on), email_prefs (off), the workspace and a session.
   - If the invite was **actually emailed** to this same address (the mail server took it; the mail job records `emailed` in the invitation's sealed payload), the email counts as verified. An invitation the admin copied, one made while email was off or the queue was full, and one the mail server refused, prove nothing about the mailbox, so those sign-ups get a verification email when SMTP is set up. Digests require a verified address.
3. `welcome.html`: each step has an `<h2 tabindex=-1>` that receives focus, "Step n of 6" as text, Back/Next and, where it makes sense, "Skip for now". Each step saves through the existing APIs (`/api/profile`, `/api/pins`, `/api/schedule`, `/api/documents`, `/api/documents/suggest-titles`), and `onboarding_step` records progress.
   1. **Theme.** A radio group in a fieldset. Each card previews its theme by carrying `data-theme` on the card itself, which works because themes.css uses attribute selectors. Choosing applies the theme to the page at once and saves it with `PUT /api/account/prefs`.
   2. **Job titles,** with an optional resume upload and "Suggest titles".
   3. **Where:** SEEK locations, a home suburb (a text field geocoded through a new `GET /api/geo/lookup?q=` that creates the home pin; no map needed, which keeps it keyboard-friendly), radius, work modes and maximum office days.
   4. **Optional extras:** salary floor and exclude keywords.
   5. **Scheduled searches and emails:** the background-access switch with its explanation, and the digest switch with "every n days". When an option isn't available it explains why (needs a membership, or email isn't set up).
   6. **Recovery codes,** shown once:
      - Copy all, Download .txt and Print.
      - "I've saved my recovery codes" (a checkbox), then `POST /api/auth/recovery-codes/ack` wipes `pending_codes_enc`. The page keeps the codes in `sessionStorage` (this tab only) until then; a later visit asks for the password (`POST /api/auth/recovery-codes/show`, below).
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
  - `POST /api/auth/recover {email, recovery_code, new_password, code | second_recovery_code}`. The code unwraps the DEK and the IK. With MFA on, the person also needs the TOTP code or a second recovery code.
  - The DEK and the IK are re-wrapped under the new password, the code is used up, other sessions are signed out and the person is logged in. A code made before the IK can't open it: an account that has no IK yet gets one now (nothing lost); one that has an IK gets a new key pair and IK, an admin loses their grant (`admin_key_lost: true` in the answer, audited) and keeps their data.
  - This path needs no SMTP, so it works on local installs.
- **Regenerate:** `POST /api/auth/recovery-codes {password, code?}` replaces all eight wraps and shows the new codes once. The request carries the password itself (and a code from the app or a recovery code with MFA on), not just a recent sign-in: every code opens the data key and can set a new password, so a cookie taken from a browser a few minutes after sign-in must not be able to mint them. The account's address gets a "new recovery codes were made" notice. The codes are in the answer of the request that made them (and of sign-up, first run and a reset). Reading codes not yet confirmed as saved again, after a reload or on a later visit, is `POST /api/auth/recovery-codes/show {password, code?}`: the password (and with two-step on a code or recovery code) is checked like the regenerate request, because a new session counts as recent for 15 minutes and a cookie taken in that window must not read eight working codes. There is no `GET`.

**6.5 Forgot password by email, and a reset sent by an admin**
1. `POST /api/auth/forgot {email}` always answers 202 "If an account exists, we've sent a link".
   - In the background: look up `email_hash` and create a token (32 bytes, 1 hour, single use, `payload_enc` holding the email).
   - Limits: 3 an hour and 6 a day per address and per account, for anonymous requests only (the admin's button has its own, so strangers can't use up an admin's). A request over the limits still goes out when the account has no link that can be used, so whoever keeps the limits used up can't leave the owner without one (at most one extra email an hour: it is counted when the request comes in, `ratelimit.FORGOT_NO_LINK`, so requests in flight together or queued behind other mail can't each send one). Held-back requests are logged by account id.
   - Every request puts one job on the single mail thread: the real send, or for an address with no account (or one the limits hold back) a pause as long as a typical send (average of the last 20), so timing the next email can't show which addresses have accounts.
   - Send `${PUBLIC_URL}/reset-password.html#token=…`, with the token in the fragment.
2. `reset-password.html` calls `POST /api/auth/reset/info {token}`, which returns `{keeps_data, mfa_required}`.
   - **With background access (`keeps_data`):** "Your data will be kept."
   - **Locked:** the page shows the warning text from "Copy for users" below, a link to the recovery-code path, a required checkbox "I understand my saved data will be deleted" and the new-password field.
3. `POST /api/auth/reset {token, new_password, code?, accept_wipe}`. If MFA is on, the TOTP code is required, because the secret is under the KEK.
   - **With background access:** unwrap the DEK with the KEK and re-wrap it under the new password. The data is kept. **The recovery codes are replaced** (each opens the same DEK, and whoever held the account before, for example someone who signed up with this address first and kept the codes, could set a password of their own with one) and shown on the next page like after a wipe; a never-finished authenticator setup is dropped. The server key can't open the IK, so the account also gets **a new key pair and IK** (`users.replace_identity`): an admin loses their grant (`admin_key_lost: true`, audited as `admin_key_lost`) and another admin shares the key again. `POST /api/auth/reset/info` (read-only) says beforehand whether this happens (`admin_key_will_be_lost`) and whether nobody else holds the key (`last_key_holder`); the page then says that a recovery code keeps the key and, for the last holder, that the server's operator must run `python -m backend.keys new-admin-key --email ...`, and the answer to the reset says the same (`admin_key_last_holder`). A reset by email leaves nothing the previous holder could use: no recovery codes, no sessions, no pending address-change or reset links, and the device cookies are bound to the old password salt.
   - **Locked:**
     - Delete the user's workspaces (cascade), sealed files, recovery codes and `billing_self`.
     - Create a new DEK, a new IK, a new key pair and new recovery codes (shown on the next page), plus a fresh workspace.
     - Rebuild `pii_self` from the token's email; `pii_admin` is unchanged.
     - Memberships stay.
     - An admin who loses their grant this way gets a warning, and other admins see "Share the admin key again".
   - Either way: all sessions are deleted and a "Your password was changed" notice is sent.
4. **Sent by an admin:** in the admin panel, "Send password reset email" decrypts `pii_admin.email` and goes through the same token flow. Admins never set passwords.

**6.6 Change password.** `POST /api/auth/password {current_password, new_password, sign_out_others=true}`. The DEK and the IK are re-wrapped and nothing is re-encrypted.

**6.7 Background access on and off.** Settings → Account → "Background access" (shown as a switch).
- **On:** re-authenticate, then wrap the DEK with the current KEK.
- **Off:** set `server_wrapped_dek=NULL`. Scheduled searches and digests stop, and the explanation says so.

**6.8 Delete account.** `POST /api/account/delete {password}`. Confirmation is the password, which a password manager can fill; there's no "type DELETE" test.
1. Refuse while one of the user's runs is active.
2. Cancel the Stripe subscription immediately.
3. Delete sealed files, workspaces (cascade), keys, sessions and grants.
4. Set `llm_usage.user_id` to NULL.
5. If the user was an admin, rotate the APK. The last admin can't delete their account.

**6.9 Change of email address.** Asking needs a recent sign-in **and the password in the request** (with MFA on, a code too): otherwise a session cookie taken in the 15 minutes after sign-in could move the account to another mailbox, and "forgot password" there would hand it over. The old address gets a notice when the change starts (a password change or reset ends a pending change). A verification link goes to the new address and says what it is for. Opening it only shows the address and a button (`POST /api/auth/verify-email/info`, which uses nothing up); the button sends the confirmation. A page that confirmed on load would let a mail scanner that runs scripts, or one stray click, put one account on another person's address. Confirming updates `email_hash`, `pii_self` and `pii_admin` (sealed to the APK public key).

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
  - `POST …/{id}/disable`†: also replaces the admin key when the account is an admin who holds it; only the replacement needs the acting admin's own copy, so without one they are still turned off and their grant deleted (§1.4). `POST …/{id}/enable` shares nothing.
  - `DELETE …/{id}`†
- **Roles**
  - `POST …/{id}/role {role}`†: makes a grant, or rotates the APK.
  - `POST …/{id}/regrant`†: shares the admin key again with an active admin who lost it, or was turned off and on.
- **Membership**
  - `PUT …/{id}/membership {exempt?, free_searches_used?}`†
  - `POST …/{id}/grants {kind, starts_at, ends_at | days, note}`†, `PATCH|DELETE …/grants/{gid}`† (free access outlives a stolen session, so these ask for the password again like the Stripe settings; the dialog's `reauth()` asks for it and repeats the save)
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

The LLM card (provider, model, key, reasoning level per task, connection test) and the commute-key card (TomTom, Transport for NSW) move from the Settings tab into the admin panel's Server section, as "AI provider" and "Commute data" (§14.8). They are global for every user, and ordinary users can't see them; the Settings tab keeps only personal settings.

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
- **Portal:** `POST /api/billing/portal` needs the password entered in the last 15 minutes (`require_recent_auth`; the page calls it through `withRecentAuth`), because the portal shows the billing name, address and card and can cancel or change the plan. It creates a portal session for `billing_self.customer`, with return URL `billing.html`. Plan changes, card updates and cancellation are configured in the Stripe dashboard. If `billing_self` was lost in a wipe, it is found again by searching Stripe customers by `jh_ref`.
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
  | AI features (rescore and Score pending, chat, cover letters, title suggestions, research or Fill missing info, concise versions, summary) | Members only (reason `ai_members_only`), whatever is left of the free searches (§13 item 5). A free search run still scores the jobs it finds, and up to `access.FREE_SEARCH_EXTRA_SCORES` (5) other jobs waiting for a first score (§13 item 6): that scoring happens inside the run and doesn't ask the gate. |

  - **What counts as a "search":** one search run (Run search or a scheduled search), counted when it is queued (`search_runs.free_search=1`). A run that ends `failed` or is interrupted **while it is still searching** gives it back. When `run_search` returns, `app.start_search_run` clears the mark (the search is paid for: the sources were fetched), so a failure in the duplicate check or the scoring, or a restart during them, keeps it used. Otherwise an answer built to break the scoring would make every search free. Rescoring, research, chat, cover letters, title suggestions and importing saved pages don't count (the AI features are refused to trial accounts, so they never reach a count).
  - **Free periods and free searches:** free searches are not used up during a free period.
  - **Free period for someone already paying:** an admin grant also pushes back their next payment, with `subscriptions.update(sub, trial_end=grant_end, proration_behavior="none")`.
  - **Exempting someone who pays:** the admin is offered "Cancel their subscription at period end".
  - **Refusals:** blocked endpoints answer 402 `{"code":"membership_required"}`, and the frontend shows a banner linking to `billing.html`.
- **Test mode and live mode:** `memberships.stripe_mode` (`test` or `live`, from the key in use when `apply_update` stored the state) says which kind of key the stored subscription came from. When it differs from the current key (a test-card payment kept after the operator saved a live key, or a row stored before the column existed), `membership._foreign` is true: `load_state` leaves the subscription out, so it gives no access, and the gate syncs with Stripe (at most every 5 minutes) and either stores the live account's answer or forgets the row (`_forget_stripe`). Without this nothing asked Stripe until the stored period ran out, up to a year. The same account switched to another live Stripe account is not detected until the period ends (the key's prefix is all the row records).
- **Test mode:** an `sk_test_` key shows a "Test mode" badge in the admin panel and on the billing page. Stripe test clocks can simulate renewals. Unit tests mock `StripeClient` and never use the network.

---

## 9. Email
- **Settings,** through `app_settings.ENV_RULES` and edited only in Admin → Server:

  | Variable | Rule |
  |---|---|
  | `PUBLIC_BASE_URL` | `https://host[:port][/path]`, or `http` only for localhost; no credentials, query or fragment |
  | `SMTP_HOST` | Must match `HOST_RE` |
  | `SMTP_PORT` | 1–65535 |
  | `SMTP_SECURITY` | `starttls`, `ssl`, or `none` (localhost only) |
  | `SMTP_USER` | `[A-Za-z0-9._%+@-]{1,254}` |
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
      AND j.duplicate_of IS NULL
    ```
    `:cutoff = last_sent_at or opted_in_at`. Excluded, hidden and closed jobs are dropped in Python: `excluded_reason` and `hidden` are `SealedAlways`, and closed is the posting's (§14).
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
  - `PUBLIC_BASE_URL` and the SMTP variables from section 9.
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
| A2 | Shared job pool and company-profile cache (§14) | `postings` table and `jobs` as the per-workspace link (same ids); `backend/pool.py` (upsert, link, detail freshness, global closing rule, pool matching, link refresh); private uploads and imports; personal link fields and fit status decided in Python, not SQL; `backend/pool_migration.py` (backup, batches, idempotent, resumable); company profiles: 30-day base, per-section sentiment and news timestamps, narrower refresh on request | A | B | opus, high | Two workspaces share one posting and one detail fetch; pool matching; private full ad not visible to others; per-workspace fit; closing rule; migration with resume; `list_jobs` shape unchanged; profile freshness |
| B | Crypto core | `backend/crypto.py` (envelopes, sealed types and their comparator guard, scope, blind index, sealed boxes, files, `submit`); `backend/keys.py` (key file, fingerprint, KEK wrap and rotation, pepper, signing, bench-kdf); argon2 profiles; requirements; `.gitignore` | — | A | **opus, high** | Round trips; wrong key gives KeyMismatch; AAD binding; legacy plaintext passthrough; comparator raises; key file 0600 and atomic; rotation re-wraps and survives a crash midway; copied context reaches the thread pool |
| C | Accounts and sessions | The account tables in §4; first run (token file and environment bootstrap) and workspace claiming (no data encryption yet); login and logout; database sessions with cookie-wrapped DEK; CSRF; rate limiting; `current_user`; ownership check in `current_workspace`; global settings admin-only for reading and writing (`/api/settings/*`, `/api/llm/*`, `/api/score/test`), `/api/score/status` and `/api/commute/status` trimmed to availability for non-admins, the LLM and commute-key cards hidden from them (§14.8); `login.html`, `first-run.html`; recovery codes shown at first run; `common.js` 401/402/423 handling and CSRF; account menu; `tests/helpers.py` with every test migrated; `launch.py`; CI login smoke and restart check | A, B | — | **opus, high** | Signup and login; wrong password and lockout; CSRF refusal; Origin check; another user's workspace gives 404; restart keeps the session; first-run token needed; environment bootstrap; global settings refused to non-admins for reading and writing; score status shows no model or URL to non-admins |
| D | Encrypt user data and migrate | Sealed types on every table in §2.2, including `SealedAlways` for the link's `hidden`, `excluded_reason`, verdict, office override and own-content columns; `fit_results.status` sealed; `postings` stays plaintext and `jobs.posting_id` stays plaintext (§14.6); `url_index`/`careers_url_index`; sealed uploads; per-user commute cache; process-key http cache; `migrate_encryption.py` (backup, batches, check, VACUUM, report); 423 progress view; `secure_delete`; KeyMismatch becomes 404; tools/demo_data and readme_screenshots; benchmark tool | C | — | **opus, high** | Grep of raw DB, WAL and uploads bytes finds none of the planted markers; migration interrupted midway resumes and finishes; plaintext audit is zero; no SQL `IS NULL` on `SealedAlways` columns (the comparator refuses it); the raw bytes show no difference between an excluded and a kept job; cross-user isolation; scorer thread pool works under scope; benchmark of `list_jobs` with the Python filter (§14.6) |
| E | Rest of the auth flows | MFA (pyotp and segno), recovery-code login and keep-data recovery, change password, regenerate recovery codes, sign out everywhere, delete account, theme in `prefs_enc`, Account cards on the Settings tab | D | F, G (G's UI waits for E) | opus for the backend, sonnet for the UI | TOTP replay refused; the recovery code is used up; recovery keeps the data; a locked wipe deletes everything; deletion cleans up files and sessions |
| F | Admin panel core | `admin.py` and `admin.html`: stats (adding `llm_usage.user_id`), users with their details, sign-up mode and invites plus `signup.html`, promote and demote with APK grants and rotation, re-grant, disable, MFA off, delete, audit log, plaintext-files report, KEK rotation button; Server section gains the "AI provider" (LLM provider, model, key, reasoning level per task, connection test) and "Commute data" (TomTom, Transport for NSW keys) cards moved from the Settings tab (§14.8) | D | E, G | sonnet, medium; **opus for the APK grant and rotation code** | The stats SQL; admin responses contain no content fields; promote then demote rotates the APK and the remaining admins can still read details; `new-admin-key` self-heals details at the next login |
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
1. **Email resets for users with background access keep their data,** because the server holds their key. With MFA on, the TOTP code is still required. A strict reading of "a reset without the recovery code wipes" would wipe these users too. Default: keep the data. (The server can't open the identity key, so an admin who resets this way loses their share of the admin key, §1.2.)
2. **What the digest cards show:**
   - **Work type:** default is remote, hybrid or onsite. Employment type (full-time or contract) isn't stored today; it would need changes to the adapters.
   - **Job title:** default is to show it as the card heading, although your list didn't name it.
   - **Score:** default is to leave it out.
3. **LLM features after the free searches run out:** default is that rescoring, chat and cover letters are blocked until the person becomes a member. The jobs found stay visible. (Item 5 in section 13 later made the AI features members-only from the start.)

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

## 13. The user's answers to section 12 (2026-10-05)
1. **Email resets for background-access users keep their data.** The server already holds their key. If MFA is on, a TOTP code is still required.
2. **Digest cards show everything:**
   - the job title as the heading
   - every field the user listed; "work type" is remote, hybrid or onsite
   - the fit score (0–100)
   - the **employment type** (full-time, part-time, contract, casual). Jobs don't record this yet, so it needs a new `jobs.employment_type` column, sealed in Phase D, filled by the adapters (SEEK's work type; ATS fields where they exist). The main job cards show it too, so the digest mirrors the app.
3. **After the 3 free searches:** jobs already found stay visible. New searches, rescoring, chat and cover letters are blocked until the person becomes a member. (Item 5 later made the AI features members-only from the start.)
4. **Release timing:** Phase 1 and the WCAG/mobile pass ship together in one release; no push before then.
5. **AI features are for members only (2026-10-08).** A security review pointed out that a trial account could use the AI features as much as it liked without spending a free search. The user's decision: trial accounts get their free searches, and each free search run still scores the jobs it finds (inside the run, not through a gated endpoint), but rescoring, chat, cover letters, company research and Fill missing info, title suggestions, and the concise versions and summary of documents wait until the person becomes a member. With billing off nothing is gated. Admins, exempt accounts, active free periods and paying members are members, as before. Built as: `access.decide` refuses every feature except `view_jobs` and `search` without a membership; the AI features get the reason `ai_members_only` and the message "AI features are for members. Your free searches still find and score jobs. ..." (a search keeps `free_searches_used`, a scheduled search `membership_required`). The pages show the usual 402 notice and a short note beside the AI actions for trial accounts. The sign-up walkthrough has no AI step: titles are typed, so a trial account can finish it.
6. **A free search also scores up to 5 waiting jobs (2026-10-09).** Under item 5 a free search scored only the jobs it found, so a job the person imported from a saved page (`POST /api/import/page`: no `first_run_id`) or completed with an uploaded full ad was never scored: rescoring and Score pending are for members. The user's decision: "allow 5". Each free search run scores the jobs it found plus up to 5 of the workspace's other jobs that are waiting for a first score (never scored, or the last scoring failed; and eligible: not excluded, hidden, closed or without a description), newest first (highest `jobs.id`), jobs never scored before jobs whose last scoring failed (so jobs that keep failing don't take the slots every time). Members, and anything that isn't a free search, are unchanged: every pending job. Built as: `access.FREE_SEARCH_EXTRA_SCORES = 5`; `app.start_search_run` passes it as `extra` to `scorer.select_jobs`, which picks the 5 in Python after the eligibility checks (so they are 5 jobs that really get scored, and none is a job the search found), from `pool.job_states` (this workspace's listed jobs only: merged duplicates are skipped), sorting by the plaintext id. The Jobs tab note, the membership page, the "full ad" message and the import result say so for trial accounts that still have a free search (`me.can_search`; with none left the pages say only that scoring by hand is for members) (`/api/billing/status` `me.free_search_extra_scores` carries the number).

## 14. Shared job pool and company-profile cache (2026-10-05)

**What the user asked for:**
1. Scrape each job listing once, and let every user draw on the stored jobs. Content a user adds by hand (a saved full SEEK ad, a saved search-results or job page) stays private to that user's workspace; scraped content is shared.
2. Company profiles are shared by all users and cached for a month. Employee sentiment and news/controversies are cached too, but when someone asks to research a company, those two parts are refreshed if they are more than a day old.
3. Admins set the LLM (provider, model, keys, reasoning levels per task) and the TomTom and Transport for NSW keys for everyone; ordinary users can't see or change them. Design only here; it is built in Phases C and F (14.8).
4. **The user's decision on encryption:** the operator may see which postings each user has. The link from a workspace to a posting stays plaintext, with no blind index. Everything the user thinks of a posting or did with it is sealed in Phase D, including `hidden` and `excluded_reason`, which the earlier design kept plaintext or filtered in SQL (14.6).

Phase "A2" below builds 1 and 2. It changes nothing the frontend sees: the `/api` job JSON keeps the same ids and fields.

### 14.1 Data model

**`postings` (new, shared, plaintext): one row per canonical posting.**
- **Identity:** the posting's URL as the adapter produces it, `UNIQUE`. Adapters already canonicalise it: SEEK builds `https://www.seek.com.au/job/{id}` from the id, the generic scraper drops fragments, and ATS feeds give the board's own URL. Today's per-workspace key is the same URL, so existing rows map to postings one to one. `(source, external_id)` is not used as a key: crawled careers pages have no stable id, and one id can come from two boards' feeds.
- **Scraped, public fields:** `source`, `external_id`, `company`, `title`, `location_text`, `country`, `work_mode`, `employment_type`, `office_days`, `salary_min`, `salary_max`, `salary_text`, `description` (as scraped), `description_hash`, `detail_status` (none | summary | full), `detail_fetched_at`, `posted_at`, `industry`.
- **Derived once, from the scraped text only:** the country guess, the work mode, office days and the description hash. They don't depend on any user, so they are computed when the posting changes (`pool.derive`), not per workspace.
- **The office named in the ad,** when that is a property of the public ad: `ad_office_text` (empty when the ad names none) and `ad_office_checked_at` (when an LLM read the public ad for it).
- **Pool state:** `not_a_job` (the detail page turned out not to be a posting), `listing_key` (the listing that last listed it, see 14.3), `first_seen` (first stored in the pool), `last_listed_at` (last time any listing showed it), `closed_at`, `close_reason` (gone | unlisted | stale), `rev` (bumped whenever its content changes).
- Coordinates are **not** stored on the posting: an ad open in several places ("Sydney NSW; Canberra ACT") is placed at the place nearest each user's home, so placement is per workspace. The geocode cache is shared, so placing it again costs no request.

**`jobs` becomes the per-workspace link, keeping its table and its ids.**
- **Plaintext:** `id`, `workspace_id`, `posting_id` (NULL for a private job), `url` (the link key: the posting's URL, or a private job's own), `origin` ('pool' | 'private'; NULL only on rows the migration hasn't reached), `source_id` (which of the workspace's sources found it), `duplicate_of`, `first_seen` (first linked to this workspace), `posting_rev` (the posting `rev` this link was last refreshed against).
- **Personal (sealed in Phase D):** `status`, `hidden`, `excluded_reason`, `distance_km`, `lat`, `lng`, `location_verdict`, the office override (`office_text`, `office_lat`, `office_lng`, `office_source`, `office_checked_at`) and `last_seen` (when this workspace's own search last listed it).
- **Own content:** the old content columns (`title` … `description`, `detail_status`, `closed_at`) stay on `jobs`, but they now hold only this workspace's own values. These are overrides of the posting (a privately uploaded full ad, or a detail filled in from a merged duplicate) or, for a private job, all of its content. NULL means "the posting's value".
- **In the code:** `job.title` and the other content attributes are Python properties: the workspace's own value when there is one, else the posting's. Assigning through a job always writes the workspace's own value; only `backend/pool.py` writes postings. A slip therefore can't leak a user's data into the pool. The properties have no SQL form, so any `db.query(Job.title)` fails loudly instead of reading only the override column.

**Why `jobs` stays and becomes the link:** every id the rest of the system holds is a `jobs.id`. That covers the frontend's job ids (localStorage and URLs such as `job.html?id=`), `fit_results.job_id`, `cover_letters.job_id`, `chat_threads.job_ids_json` and `job_duplicates`. They all stay valid, and nothing is re-keyed. Two workspaces' copies of one ad keep their two ids and now point at one posting. Within one workspace, two rows never shared a URL (`UNIQUE(workspace_id, url)`), so no row is merged away and no public id changes.

**Tables keyed by job id are per workspace by construction,** because a link id belongs to one workspace:
- `fit_results`: per workspace (it depends on that user's documents and on the description that workspace sees).
- `cover_letters`, `chat_threads`/`chat_messages`, `job_duplicates`: per workspace, as before.
- `company_profiles` and `geocode_cache` stay shared; `commute_cache` becomes per user in Phase D (unchanged).

### 14.2 Search flow
For each source (`pipeline._process_source`):
1. **List** with `adapter.list_postings()`, through the http cache (TTL as today).
2. **Upsert into the pool** (`pool.upsert_scraped`): the posting is created if new; listing data updates it without losing better data (a full description is never replaced by a summary); `last_listed_at` and `listing_key` are set; a closed posting reopens. Any content change bumps `rev` and re-derives.
3. **Link it to the workspace** (`pool.link`): get or create the workspace's `jobs` row by URL. A private job with the same URL (14.4) is attached to the posting; its own values that equal the posting's are dropped, and the rest stay as private overrides.
4. **Fetch the detail page only when the pool needs it** (`pool.needs_detail`), the workspace's listing-stage filter passes and the per-source budget (60 a run) allows. The freshness rule:
   - The posting has no full description yet: new to the pool, deferred by an earlier budget, or the fetch failed.
   - Or its full description is older than 30 days (`DETAIL_REFRESH`) and it is still listed. Ads rarely change, and a monthly re-fetch also confirms the page is still there (rule 1 in 14.3).

   So two workspaces listing the same posting cost one detail fetch, and a posting found by someone else needs no fetch at all. A 404 or 410 closes the posting (14.3), and a page that isn't a posting sets `not_a_job`.
5. **Refresh the link** (`pool.refresh_link`): place it (per-workspace pins), take the office from the ad when the posting has one and the user hasn't set another, then filter it.

After all sources:
- **Closing sweep:** `pool.sweep`, see 14.3.
- **Pool matching** (`pool.match_pool`): open postings already in the pool, found by any user's search, are matched against this workspace's profile with the same filters the app applies today (`filters.exclusion_reason`: titles, include and exclude keywords, dealbreakers, salary floor, work modes, office days, pins), and the matches are linked. A user therefore benefits from postings other users' searches found.
  - A match must hit one of the workspace's titles (in the posting's title) or an include keyword (in the title or the description), and must pass every filter.
  - A workspace with neither titles nor include keywords gets no matches. Otherwise it would be shown the whole pool, which would reveal what everyone else searches for. Postings that only match other users' filters stay invisible.
  - Skipped: closed postings, `not_a_job` ones, those the workspace already links (by posting or by URL), and private content, which never enters the pool.
  - At most 300 matches per run (`MAX_POOL_MATCHES`), newest-listed first; the rest follow at the next run. The run summary gains `totals.pool_matched`.
- **Refreshing links to changed postings:** links to postings that other workspaces' runs changed since this workspace last looked (`posting_rev != rev`) are refreshed (`pool.refresh_links`).
- **Then, as today:** refilter, duplicate scan and scoring.

**Duplicates (copies of one ad on several boards) stay per workspace:**
- Merging, splitting and "these are different jobs" are the user's decisions.
- Which copy is listed depends on that user's filters, scores and statuses.
- A scan covers one workspace's links, so it stays small.
- A global scan would let one user's wrong merge hide another user's job.

CLAUDE.md's `duplicate_of` rule keeps working unchanged, because `duplicate_of` is on the link. Gap-filling (salary, work mode, … taken from a copy) writes the workspace's own override values, never the posting.

### 14.3 Closing postings
A posting missing from one user's query results says nothing about whether it closed, because that user's titles or locations differ from the next user's. The rule is global and conservative, and lives on the posting:
1. **Gone:** its detail page or API answers 404 or 410. The posting is closed with reason `gone`.
2. **Missing from a complete listing of the same listing.** Complete-listing adapters (ATS feeds, an APSJobs query, a careers page crawled to the end) return every open job of that listing, whoever asks.
   - A posting whose `listing_key` is that listing and that the listing no longer contains is closed with reason `unlisted`.
   - The listing key is the adapter's `source` for ATS feeds, Atlassian and APSJobs (whose source already encodes the query). For a crawled careers page it is the source plus the careers URL, so two users crawling different pages of one site can't close each other's postings.
   - Capped or partial listings already turn `complete_listing` off, so they close nothing.
3. **Stale:** not seen in any listing for 45 days (`STALE_AFTER`), reason `stale`. This covers SEEK and the other partial listings, which never closed before. SEEK ads run for 30 days unless renewed, and a renewed ad is listed again and reopens.

**Other rules:**
- A closed posting that appears in a listing again reopens.
- Private jobs are never closed by these rules, since no listing knows them.
- Unlinked postings that have been closed for 180 days are deleted from the pool.

**What changes from the old per-source "unlisted" closing:**
- Before, each workspace closed its own rows: by `source_id` for a company source or by `source` for SEEK, with `url NOT IN (seen)`.
- Now one complete listing closes the posting for everyone, keyed by the listing and not by the workspace.
- The run report's "closed" column counts the postings this run closed (rules 1 and 2), plus the stale sweep.
- `jobs.closed_at` is no longer written for pool links. Reading `job.closed_at` gives the posting's.

### 14.4 Private uploads
- **"Upload full ad"** (`POST /api/jobs/{id}/import-page`):
  - The saved page's values go into the link's own columns: the description, `detail_status = "full"`, and any field the page states differently.
  - The posting is untouched, so other workspaces keep the summary.
  - That workspace's filters, scorer, chat and cover letters read the full ad; everyone else reads the summary.
  - Fields derived from the private ad (office days, work mode, description hash) are computed into the workspace's own columns.
- **"Import a saved page"** (`POST /api/import/page`: a SEEK search-results page, a SEEK job page or any page with JobPosting JSON-LD). For each job on the page:
  - If the workspace already has it, the page's differing values become own overrides.
  - Else, if the pool has the URL, a pool link is made and differing values become overrides.
  - Otherwise a **private job** is created (`origin = 'private'`, `posting_id` NULL), which never enters the pool. If a later search of this workspace lists the same URL, the private job is attached to the new posting, and its own differing values stay private.
- **A source label the user typed is theirs.** Adapters fall back to a company name derived from the source's URL (`sources.default_label`), and that name goes into the posting. When the user gave the source a different label, it becomes the link's own company for the jobs that named no employer, as the old pipeline showed it.
- **Facts an LLM reads from an ad are shared only when it read the public ad:**
  - The office named in the ad (`ad_office_text`, from scoring or Fill missing info).
  - The employment type taken from the scorer.

  Read from a private full ad, they stay on the link.

### 14.5 Migration (existing installs)
`backend/pool_migration.py`, called by `init_db()` at startup. Existing installs have per-workspace copies of the same jobs; the user's two workspaces overlap.
1. **State:** `app_settings["job_pool_migration"]`. When absent, it is `pending` if `jobs` has rows with `origin IS NULL`, otherwise `done` (a fresh database).
2. **Backup first:** `data/jobhunt.db.bak-before-job-pool`, made with SQLite's online backup API, once. Like the other `bak-*` files, it holds plaintext; Phase D's leftover-files report lists it.
3. **Batches** of 200 URLs, one transaction each. For every URL, all its unmigrated rows (any workspace) are classified:
   - `source = 'import'` (a JSON-LD page the user saved) or no URL: **private**. Origin `private`, everything stays on the row.
   - SEEK rows with `detail_status = 'full'`: SEEK's job pages are robots-blocked, so only "Upload full ad" or a saved job page produced these. They are **pool links with a private full ad.** The posting takes the public fields (title, company, location, salary, work mode, dates, ids). The description, detail status, fetch time, office days and description hash stay on the link, as does an office read from that ad.
   - Everything else is scraped: a **pool link**. Its content moves to the posting and the row's content columns are cleared. SEEK summary rows that came from a saved search-results page can't be told apart from scraped ones. They join the pool, which is harmless: they hold the same public summary the scraper would get.
4. **The posting's content** comes from the best public row (full description first, then the most recently seen).
   - The posting is closed only if every public row was closed.
   - Its `first_seen` is the earliest row's and its `last_listed_at` is the latest `last_seen`.
   - An office read from a public ad (`office_checked_at` with `office_source` 'ad', 'company' or none) becomes the posting's `ad_office_*`.
5. **Kept on every link:** status, hidden, excluded_reason, coordinates, distance, verdict, office override, `duplicate_of`, `source_id`, `first_seen` and `last_seen`.
   - Fit results, cover letters, chats and duplicate decisions point at link ids, which don't change. Nothing to re-key, and nothing to do for job ids held in localStorage or in chats.
   - Values an old duplicate merge copied onto a row (salary, work mode, …) can't be told apart from scraped ones. They came from another public ad of the same job, so they move to the posting with the rest.
   - A company name that is the label the user typed for the source (the old pipeline used the label when an ad named no employer) stays the link's own value. The posting gets the name derived from the source's URL, as new searches do (14.4).
6. **Company profiles** (in `_fix_legacy_data`, idempotent):
   - `news_researched_at = researched_at` for every researched profile: the single research always covered controversies.
   - `sentiment_researched_at = researched_at` where `sentiment_json` is set (research version 2 and later).
7. **`done`.**

**Idempotent and resumable:**
- A row is migrated exactly when its `origin` is set. Postings are found by URL, and each batch commits on its own.
- After a crash, `running` resumes at the next start with the rows still unmigrated. The backup is not made again.
- Running it again on a finished database does nothing.

### 14.6 Encryption (Phase D), as the user decided
- **Shared postings stay plaintext.** They are public ads.
- **The link's reference to the posting stays plaintext:** `workspace_id`, `posting_id`, and for pool links the URL, which equals the posting's. The earlier recommendation was a sealed `posting_id` with a blind index, so the operator couldn't tell which postings a user has. The user chose to give that up for simpler queries. §2.5 now says so plainly.
- **Sealed on the link, under the user's DEK:**
  - `status`, `hidden`, `excluded_reason`, `distance_km`, `lat`, `lng` and `location_verdict`.
  - The office override columns and `last_seen`.
  - Every own-content column, including a privately uploaded full ad.
  - A private job's `url`, through `url_index` (§2.3).
- **Sealed in `fit_results`:** score, evidence, status and error.
- **Null-ness must not leak.** `hidden`, `excluded_reason`, `location_verdict`, the office override and the own-content columns use a new `SealedAlways` type (§2.1). Phase D also drops the `server_default`s that `jobs.hidden` and `jobs.detail_status` still carry (§15). It writes a sealed marker for NULL, so `IS NULL` reveals nothing and is refused like any other comparison. Whether a user uploaded a full ad then shows only as ciphertext length, which §2.5 already lists.
- **Stays plaintext:**
  - `first_seen`. Link ids are allocated in order, and run start times are plaintext, so when a link was made is visible either way; sealing it would hide nothing.
  - `last_seen` is sealed, because it records which later searches still matched the job.
  - `duplicate_of`. Most merges are automatic, made from public data, and CLAUDE.md's rule filters on it in SQL. It does reveal a user's own merges and splits, which §2.5 lists.
  - `origin`, `source_id`, `posting_rev` and `job_duplicates.status`.
- **Code rule from A2 on:** never filter or sort in SQL on `status`, `hidden`, `excluded_reason`, `distance_km`, `location_verdict`, office fields, own-content columns or `fit_results.status`/`score`.
  - A2's `list_jobs`, scorer selection, research and office selection, `filters.softened_job_ids` and the duplicate ranking load the workspace's link rows and decide in Python.
  - SQL still filters on `workspace_id`, `posting_id`, `duplicate_of` and `id`, and on posting columns (`postings.closed_at`, …), which are plaintext.
- **Cost on a Pi 4, replacing the old SQL shortcut (§2.4):**
  - `list_jobs` first loads `(id, hidden, excluded_reason, duplicate_of)` for every top-level link of the workspace: two small sealed values per link. At roughly 5 µs per value, 5,000 links cost about 50 ms.
  - It then loads the listed links in full: about 15 sealed values each, mostly NULL markers or short strings, since postings are plaintext. With about 20% listed, 1,000 × 15 × 5 µs is about 75 ms. Hidden and excluded jobs are only decrypted when asked for.
  - That is well under the old estimate of 0.5–1 s, which decrypted about 25 values per job, and under §2.4's 1.5 s target. Phase D's benchmark tool measures it.

### 14.7 Company profiles
- **Shared** by all users, as before. `/api/companies` keeps Phase A's scoping: it lists the profiles of companies in the current workspace's jobs, so it doesn't reveal what others researched. Anyone whose jobs include a company gets the shared cached profile with no new research, including companies first seen through pool matches.
- **Freshness:**
  - The base profile (business model, ownership, headquarters, headcount, offices, sources) is refreshed after 30 days (`BASE_REFRESH`; it was 90).
  - Employee sentiment and news/controversies keep their own timestamps.
  - Bulk "Fill missing info" researches companies whose base is missing, failed, from an older research version or older than 30 days. It does not refresh sentiment or news on their own.
- **"Research this company"** (`POST /api/companies/research {name}`, from the company dialog and the job page) follows a plan (`research.plan`):
  - **Full research** (one web-search request, as today) when the base is missing, failed, outdated or older than 30 days. It sets all three timestamps.
  - Otherwise a **narrower research** when sentiment, news or both are older than one day (`SECTION_REFRESH`). One web-search request asks only for the stale parts: the sentiment with the Glassdoor page, and/or the controversies with the note. Links are verified as in full research. Only those fields and their timestamps change; the base is kept.
  - Otherwise **nothing:** the cached profile is shown and no LLM call is made.
  - Every request goes through `llm_providers.web_search`, as today.
- **What the run reports:** `summary.research` keeps `requested`, `researched`, `errors`, `skipped`, `first_error` and `aborted`, so the frontend's text still works. `researched` counts the profiles that are up to date afterwards, whether researched now or already fresh, so "Research again" on a recent profile reads "researched 1/1" and not "0/1". It adds:
  - `refreshed: {company name: [parts]}`, where the parts are `"base"`, `"sentiment"` and `"news"` (a full research lists all three).
  - `up_to_date: [company names]` for those that needed nothing.
- **Profile JSON (`CompanyProfile.to_dict`, `/api/companies`).** Alongside the existing fields:
  - `researched_at`: the base profile's date.
  - `sentiment_researched_at`: the employee-sentiment section's date.
  - `news_researched_at`: the controversies/news section's date.

  Migrated rows get the section dates from `researched_at`, where that research covered them (14.5 step 6). The frontend shows `researched_at` today; the two section dates are for the company dialog to show next to their sections.

### 14.8 Admin-owned global settings (design only; Phases C and F)
- **What becomes admin-only:**
  - The LLM: provider, base URL, model, API key, timeout and concurrency, and the reasoning level per task.
  - The commute keys: `TOMTOM_API_KEY` and `TFNSW_API_KEY`.
- **Global, with no per-user override.** They live in `.env`, written only through `app_settings` (Phase C makes those writes admin-only). Every user's scoring, research, chat, cover letters and commute use them.
- **Ordinary users can't see or change them:**
  - `/api/settings/*`, `/api/llm/*` and `/api/score/test` require an admin for reading too, not only for writing.
  - `/api/score/status` returns only `configured`, `capabilities` (for example whether web search is available) and the counts. `base_url`, `model`, `reasoning_effort` and `provider` are left out.
  - An unconfigured LLM reads "AI features aren't set up yet. Ask your admin."
  - `/api/commute/status` returns only whether public transport and driving times are available.
  - The model name recorded on a fit result, cover letter or chat answer stays visible: it describes that output, not the settings.
- **Where they're set:** the LLM and commute-key cards move from the Settings tab to Admin → Server ("AI provider" and "Commute data"), next to email and billing. The Settings tab keeps only personal settings: workspaces, search profile, sources, schedule, theme and account.
- **Phase C** gates the endpoints (read and write) and hides the two cards from non-admins. **Phase F** moves the cards into the admin panel.

## 15. Notes from the Phase B review (these bind Phases C and D)
- **The comparator guard has blind spots.** It doesn't see a bare `order_by(col)`, `group_by(col)`, `select().distinct()` or `func.*(col)`. Phase D adds a test that greps for these on sealed columns, or adds a statement hook that refuses them. The hook (`crypto.check_statement`, run when a statement is compiled) refuses ORDER BY, GROUP BY, DISTINCT, SQL functions, `OVER (PARTITION BY / ORDER BY)`, `cast`, `type_coerce` and a UNION's own ORDER BY / GROUP BY over a sealed column. It can't see a bare `literal_column("name")` in the ORDER BY of a union (that is only text to SQLAlchemy), so don't write one.
- **Legacy plaintext is a downgrade path.**
  - Someone with write access could swap a ciphertext for plaintext, and it would be accepted.
  - Once migration state is `done`, turn on a strict mode that refuses plaintext in sealed columns.
  - Don't put `server_default` on sealed columns.
- **Errors must not leak data.**
  - Use `create_engine(..., hide_parameters=True)` and never `echo`, so a `StatementError` can't put plaintext bind values in logs.
  - Re-saving a legacy row through the ORM with an unchanged value issues no UPDATE, so the migration writes with Core `update()`.
- **The uninstaller's backup zip** will hold the key file (KEK and pepper) as well as `.env`, so Phase C writes it as 0600. On Windows the key file relies on the folder's ACLs, because `chmod` does nothing there.
- **Choices fixed in Phase B:**
  - Passwords are NFKC-normalised before argon2.
  - KEK wrap labels always end in `|{kek_id}`, e.g. `totp|{uid}|{kek_id}`.
  - Each use has its own AAD prefix (`jh-wrap|`, `jh-file|`, `jh-value|`, `jh-box|`), and columns use `jh/<table>.<column>`.
  - The 5-byte envelope header is part of the AAD.
- **Identity key (added after the final security review).** Wrap labels of the IK: `pw-ik|{uid}`, `rc-ik|{uid}|{code_id}`, `sess-ik|{sid}`, `pend-ik|{sid}`, `ik-priv|{uid}` (the private key under the IK) and `pending-codes|{uid}`; each names its row and user. The IK is never wrapped under the server KEK and never cached; multi-row key changes (migration, password change, new codes, resets) are one transaction followed by `scrub_wal()`.
- **Dependencies.** `requirements.txt` pins older `cryptography` and `argon2-cffi-bindings` on Intel Macs through environment markers, because the newest versions have no x86_64 wheels there. Keep those markers when you bump versions.

## 16. Notes from the email-core review (they bind Phases H and J)
- **Names changed in Phase H.** The public address is `PUBLIC_BASE_URL` (§9 and §6.3 say `JOBHUNT_PUBLIC_URL`) and the SMTP login is `SMTP_USER` (§9 said `SMTP_USERNAME`); use the new names.
- **Link targets differ from §9.** The email templates link to `reset-password.html#token=`, `verify-email.html#token=`,
  `signup.html?invite=` and `/unsubscribe?token=`, where §9 had `reset.html`, `accept-invite.html` and
  `/api/email/unsubscribe?t=`.
  - **Phase H changed the delimiter for the two links that give access.** The token is in the URL *fragment*
    (`#token=`), not the query: a browser never sends a fragment to a server or in a `Referer` header, so a token
    can't reach an access log, a proxy or another site. The page reads `location.hash` and posts the token in a JSON body.
  - **There is no `accept-invite.html`.** An invitation links to the sign-up page itself, which Phase F built to read
    `?invite=` (and which fills in the invited address). That token stays in the query, as Phase F built it; the
    sign-up page should send `Referrer-Policy: no-referrer` (Phase L's headers) so it isn't passed on.
  - Build the pages and routes with these names.
  - A GET on `/unsubscribe` shows a confirmation page and changes nothing.
  - A POST unsubscribes. This is the RFC 8058 one-click path, and it needs no session.
- **The SMTP password (decided in Phase H).** The admin form removes every space and tab from what is pasted (Gmail
  shows app passwords with spaces, and they work without them), checks that the rest is printable ASCII (smtplib sends
  it as ASCII), and saves it as `SMTP_PASSWORD_B64`, the unpadded base64url of the password, which lets quotes and `#`
  through `.env`. A hand-written `SMTP_PASSWORD` keeps working until the form saves a new one; when both exist the
  B64 one wins. Only "a password is set" is ever shown. Moving to another mail server needs the password typed again.
- **Phase H's choices in the reset flow (§6.5).**
  - **The person is signed in after a reset.** They proved the mailbox (and the app's code, with two-step on) and have
    just chosen the password, the same standing as `/api/auth/recover`; and after a wipe the new recovery codes can only
    be shown to someone signed in. `onboarding_step` goes back to 1 after a wipe, because the search setup is gone.
  - **A recovery code typed during a reset only stands in for the app's code** (and is used up). It does not rescue the
    data: that is the `/api/auth/recover` path, which asks two proofs from an account with two-step sign-in.
  - **A wipe loses the name** in `pii_self` (only the admin's copy has it); the address is rebuilt from the token.
  - **An admin who is wiped loses their `admin_key_grants` row** (it was sealed to the old key pair), so the panel shows
    "Share the admin key again"; the audit log gets `admin_key_lost`.
  - **Only the newest link of a kind works.** A new reset or verification link deletes the account's older unused one.
  - **A refused "forgot" (a limit, a turned-off account) still answers 202**, so no answer shows whether an account exists.
- **Sending is synchronous** in `mailer.send`. Callers go through `backend/mail_queue.py`, whose single-thread
  executor does the sending (and, for a forgotten-password request, making the link), so a request never waits.
- **What the digest needs from its caller:**
  - `unsubscribe_id`, made of `[A-Za-z0-9_-]`
  - `every_days`
  - card dicts with `fit_summary` taken from `evidence_json.summary` and the gap count counted the way the frontend counts gaps
  - `email_templates.digest_card()` is public so the wiring can reuse it

## 17. Notes from Phase J (job digests)
- **Where the code is:** `backend/digest.py` (settings, due dates, selection, the tick, the unsubscribe routes); `jobs.first_run_id` is set by `pool.link` and `pool.match_pool` from `tasks.current_run_id`, which `tasks._execute` sets for the run and its threads.
- **Settings:** `GET/PUT /api/account/digest` (not under `/api/email`), answering also `can_send` and `problems`.
- **Unsubscribe routes are outside `/api`:** `GET/HEAD /unsubscribe?token=` (a page, changes nothing), `POST /unsubscribe?token=` (RFC 8058 one-click and the page's button) and `POST /unsubscribe/resubscribe?token=`. `sessions.AuthMiddleware` guards `/api` only, so these need no exemption there; everything under `/api` still needs a session. The server renders the page itself (no JavaScript, strict CSP). A bad token, an unknown account and a signing key retired for over 30 days all give the same 410 page. POSTs are limited to 30 a minute per address; a body over 4 KB is refused.
- **The cutoff is the later of `last_sent_at` and `opted_in_at`** (not `last_sent_at or opted_in_at`), so opting in again after a pause doesn't send what turned up during it. The due date counts from the same time.
- **A failed send records nothing**, so the next tick (10 minutes later) tries again; a tick stops after 3 failed sends (the mail server is probably down). A check also waits while a scheduled run of the user's is queued or running, so a late run's jobs aren't skipped by the next cutoff.
- **Cards:** a card shows the workspace's name when the email holds jobs of several workspaces, and the original-posting link names the source ("... on SEEK", `email_templates.source_label`, the same rule as `sourceLabel` in `common.js`).

## 18. Notes from Phase L (headers, Host allowlist, log hygiene)
- **Where the code is:** `backend/hosts.py` (the allowlist), `backend/security_headers.py` (the headers and the policy), both put around the whole app by `security_headers.install` (it wraps the finished middleware stack, so even an unexpected 500 and the 400 for a bad Host carry the headers). Tests: `tests/test_security_headers.py`, `tests/test_log_leaks.py`.
- **The policy has no `'unsafe-inline'`.** The pages had three `style="--c:var(--...)"` attributes in markup built by scripts; they are now `hue-*` classes (`styles.css`). Scripts still set sizes and positions with `element.style`, which a policy doesn't restrict. The hashes of the inline theme script are computed from `frontend/*.html` at start-up (CRLF folded to LF first, as the browser does). Anything new loaded from another site must be added to `security_headers.py`; the test fails until it is.
- **`Referrer-Policy: same-origin`, not `no-referrer`:** with `no-referrer` a browser sends `Origin: null` on same-site POSTs, and `sessions.same_site` would refuse every save. `same-origin` still keeps a page's address (an invitation token in `signup.html?invite=`, a job id) from every other site. The unsubscribe pages keep their own `no-referrer`, because they don't use the same-site check.
- **Host allowlist:** any IP address, `localhost`/`*.localhost`, the machine's own names, the host of `PUBLIC_BASE_URL`, `JOBHUNT_ALLOWED_HOSTS` (`.env` only: it is a security boundary, so the app's Settings don't edit it), and `testserver` while `JOBHUNT_TESTING=1`. Names only, ports ignored. A site that reaches the app through `tailscale serve` needs its name in the public address or in `JOBHUNT_ALLOWED_HOSTS` after this phase.
- **`/docs`, `/redoc` and `/openapi.json` are off:** they listed every route to anyone who could reach the server, and Swagger's page needs scripts the policy refuses.
- **Leaks the log test found and fixed:** (1) SQLAlchemy at DEBUG logs each row it reads *after* the sealed types have opened it (a search profile, in plaintext), plus the sessions and users rows; `SqlAtWarning` in `backend/app.py` drops everything below WARNING from its engine logger whatever the level. (2) pypdf warned with the first bytes of a file it couldn't read. (3) The Claude SDK logs whole request bodies at DEBUG. (2) and (3) join httpx in `QUIET_LOGGERS`. Everything the app itself logs was already free of personal data. Not covered: uvicorn's traceback for an unhandled exception (a bug, and the message could hold data), and the first-run token, which is printed on purpose.
- **Sign-in, sessions, resets and email (the Phase L review):** the lockout is per account and source with an account-wide ceiling and a `jh_device` cookie (§3.6); wrong tries are counted before the slow check; refusals wait for a floor; the session cookie carries the port; an email reset that keeps the data replaces the recovery codes; new recovery codes and an address change need the password in the request; the confirmation page asks first; anonymous "forgot" requests have their own limits and put a decoy on the mail thread; an invitation counts the address as verified only if it was emailed to it. A second review closed three more gaps: wrong two-step codes have their own account-wide ceiling and a reset link is used up by its 5th wrong code (§3.6), the one extra "forgot" email an hour is counted at the request, and unsaved recovery codes are read again only with the password (§6.4). `nextPage()` in `frontend/auth.js` refuses a path that starts with `//` or `/\` once resolved (`/.//evil.example` keeps the origin and still means another site). The Host allowlist closes the DNS-rebinding route to the sign-in endpoints, where forged `X-Forwarded-For` values had given every request its own rate-limit bucket; `tests/test_security_headers.py` replays that attack.
- **Keys, storage and admin actions (the Phase L review, second group):**
  - An admin who is turned off keeps no grant and is given none (§1.4); turning an admin off, changing a membership or a free period, and sharing the key ask for the password again.
  - Strict mode is switched on by the key file as well as the database (§1.3). A key file written by an earlier version gets the marker at its next start.
  - The key file has an exclusive lock for the app's lifetime, and the key commands take it (§1.3). `kek_wrap` refuses when the file changed under the process.
  - The data folder, the database, its log and the plaintext backups are owner-only (§2.5). `db.online_backup` is the one place a backup is made.
  - A blocked `scrub_wal()` is tried again (§2.5); a spent recovery code (at the second step of sign-in and when turning two-step off) and the password-key upgrade at sign-in now scrub too.
  - Reset links are signed with the server key and the account id, and with background access must name the address in `pii_self` (§2.5). The unauthenticated columns and public keys a database writer can change are listed there; the pubkey MAC is left as a later hardening.
  - Tests: `tests/test_storage_security.py`, `DisabledAdminKeyTest` and `RecentPasswordForMembershipTest` in `tests/test_admin.py`, `WriterTest` in `tests/test_password_reset.py`.
- **Payments, export and mail settings (the Phase L review, third group):**
  - The export keeps the account's one-at-a-time mark and one of the two app-wide slots until the download ends (the last byte, a disconnect, or the deadline: `MAX_STREAM_SECONDS`, 10 minutes, plus the zip's size at `MIN_RATE`, 100 KB/s, so a 200 MB zip over a slow link can finish), not just until the zip is built. A timer enforces the deadline, because a client that holds the connection open and reads nothing never lets the generator check it. The zip stays in memory until then, so releasing early let one account pile up buffers with repeated requests.
  - The Stripe portal needs the password entered recently (§8). Subscriptions are bound to the kind of Stripe key they came from (§8).
  - A change of mail route is announced to the other admins and holds admin MFA disables and admin reset links for 24 hours (§2.5). The Server card says so.
  - A free search is kept once the search stage has finished (§8), and `scorer.parse_result` turns any unusable answer (an infinite score, a number where a list belongs, a deeply nested answer) into a `ScorerError` for that job only; `score_one` also turns any other exception from the model call or the parse into one, so a bad answer counts as a scoring error and never ends the run.
  - A second pass: a keyless admin can still turn off a key-holding admin (§1.4); a mail-route change by an admin without the key is refused (§2.5); the export's deadline is a timer and grows with the zip's size; the SQL guard also refuses `OVER`, `cast`, `type_coerce` and a union's ORDER BY (§15).
  - Tests: `test_the_place_is_kept_until_the_download_has_ended` (`tests/test_export.py`), `test_the_portal_needs_the_password_entered_recently` and `StaleStripeState` (`tests/test_membership.py`), `RouteChangeTest` (`tests/test_mail_settings.py`), `ScorerParseTest` (`tests/test_core.py`).
- **Behind a proxy:** `launch.py` trusts `X-Forwarded-For` from 127.0.0.1. The proxy must append the real client address (`tailscale serve` and the usual proxies do), not pass a client's own header through.
