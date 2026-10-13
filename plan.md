# Plan: open-source JobHuntPA, built by Claude cloud sessions

Written 2026-10-13. The spec and every phase's brief are in [`docs/design/open-source.md`](docs/design/open-source.md);
this file is the order of work and where it stands. Boxes are ticked by whoever merges a phase, not by the cloud
session that built it.

## How a phase runs

1. **Start a cloud session** on this repository with the prompt below, changing the phase letter.
2. **The session pushes `cloud/os-<letter>`** and ends with a report. It never pushes `main` or `wip/open-source`.
3. **Review locally:** fetch the branch, run the full suite in a throwaway worktree, read the files the report names
   for a security reviewer. Ask a local Claude session: "review and merge cloud/os-<letter>".
4. **Merge into `wip/open-source`**, tick the box here, push.
5. **Finish a whole wave before starting the next.** Phases inside a wave can run at the same time.

Prompt for a cloud session:

```
Work on JobHuntPA. Check out a new branch cloud/os-<letter> from origin/wip/open-source.
Read CLAUDE.md, plan.md and docs/design/open-source.md. Follow the shared rules in Part 4 of that file and carry out
the brief "Phase <letter>" in Part 5, nothing more. Push cloud/os-<letter> only, and finish with the report Part 4
asks for.
```

## Before the first session

- [ ] Correct anything wrong in the spec's §1.17. Four readings are still mine: starting points, "GPLv3" meaning
      version 3 only, keeping the name JobHuntPA, and installs with several accounts keeping them.
- [ ] Commit `docs/design/open-source.md` and this file, and push `main`.
- [ ] Create `wip/open-source` from `main` and push it.

## The phases

| Wave | Phase | What it does | Needs | Branch | Merged |
|---|---|---|---|---|---|
| 1 | A | Memberships, digests, email and two-step sign-in out | | `cloud/os-a` | [ ] |
| 2 | B | Admin panel out, one account, settings moved, the reset tool | A | `cloud/os-b` | [ ] |
| 3 | D | Small changes: labels, research buttons, ghost section, defaults, applied date | B | `cloud/os-d` | [ ] |
| 3 | C | Commute as Google Maps links, starting points, all offices | B, rebased on D | `cloud/os-c` | [ ] |
| 3 | E | Export file types, the import file, import | B | `cloud/os-e` | [ ] |
| 3 | F | Careers pages in bulk (CSV in and out) | B | `cloud/os-f` | [ ] |
| 4 | G | Tasks tray | wave 3 | `cloud/os-g` | [ ] |
| 4 | H | First run, data folder, AppImage, Windows setup, service script | B, C | `cloud/os-h` | [ ] |
| 5 | I1 | AI mappings: the app's side, and the contract | F, G | `cloud/os-i1` | [ ] |
| 5 | I2 | AI mappings: the extensions | I1 | `cloud/os-i2` (extension repositories) | [ ] |
| 6 | J | Licence files, other people's licences, history scan, public README, final review | all | `cloud/os-j` | [ ] |

Notes on running them:

- **Waves 1 and 2 are the risky ones.** They take out about half the backend. Review their migrations with care:
  an existing install must start and keep every job, document and setting.
- **Wave 3:** start all four together, but merge D before C. Both change the job page, so C is rebased onto D.
- **I2 runs in `JobHuntPA-chrome-extension`,** from its branch `ccr-b6871b82-e9vz3j`, and cannot read this
  repository. Paste Part 4, the Phase I2 brief, §1.15 and Part 8 (the contract Phase I1 writes) into the session.
  The Firefox repository gets the same source afterwards.
- **J touches all three repositories.** Run it once per repository; the extension sessions need the brief pasted.

## Checkpoints

- [ ] **After wave 2:** rehearse the update on a copy of the Pi's data, in a second instance on the Pi itself, before
      anything reaches the live app. This is the first point where sign-in and stored data change.
- [ ] **After wave 4:** install from the AppImage and the Windows setup on clean machines, and walk through first run.
- [ ] **After wave 4:** follow the macOS steps on a real Mac; the README's wording for recent macOS is unverified.
- [ ] **After wave 6:** read Phase J's history report before making the app's repository public. The two extension
      repositories are public already, so act on anything it finds there at once.

## After the last wave

- [ ] Merge `wip/open-source` into `main`.
- [ ] Back up the Pi first: a Download-your-data export and a copy of its data folder. Then update it and check
      sign-in with the existing account, the jobs list, and the AI settings and public address in the Settings tab.
- [ ] Raise `VERSION` (2.0.0: features are removed) and push the tag; the release workflow builds the Windows
      programs and the AppImage.
- [ ] Make the app's repository public.
- [ ] Open the Chrome and Firefox store developer accounts and submit the extensions, using Phase J's listing drafts.

## Not planned

Everything in the spec's §1.16, and three suggestions nobody has decided on: a "new version available" notice, making
the geocoder's address a setting, and `CONTRIBUTING` and `SECURITY` files.
