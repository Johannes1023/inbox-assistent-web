# Inbox-Assistent Web

A local-first macOS app that turns a phone-camera pile of photographed letters into organized, searchable PDFs — with an **optional, explicitly consented** AI classification step that runs entirely through the user's own ChatGPT or Claude subscription, never a bundled API key.

> **Status:** personal project, built as a hands-on demonstration of AI-assisted software engineering practice — planning, TDD, security review, and iterative hardening — documented end-to-end in the [commit history](../../commits/main) and [`docs/TECHNICAL.md`](docs/TECHNICAL.md).

![Platform](https://img.shields.io/badge/platform-macOS-black) ![Python](https://img.shields.io/badge/python-3.14-blue) ![Tests](https://img.shields.io/badge/tests-193%20passing-brightgreen) ![License](https://img.shields.io/badge/privacy-local--first-green)

---

## The problem

Physical mail doesn't stop existing because you photograph it. A phone full of `IMG_4101.jpg`, `IMG_4102.jpg`, `IMG_4103.jpg` — some of them pages of the *same* letter, in no particular order, with no date, sender, or subject attached — is not an archive. It's a backlog.

Sorting it by hand means: figuring out which photos belong together, fixing the crooked angle and orientation from a hasty phone snapshot, and typing out a consistent filename (`date_sender_subject.pdf`) for every single letter, twice a week, forever.

## What it does

1. **Pick a folder.** The app works only inside a folder you explicitly choose via the native macOS folder picker — it never sees the rest of your filesystem.
2. **Drop in photos.** JPG, JPEG, HEIC, PNG. Every import is verified byte-for-byte against the source file — nothing is trusted just because a browser said so.
3. **Group pages into letters** — either by hand, or by asking an AI model to do it.
4. **Review and confirm.** AI suggestions are never applied silently: every suggested date, sender, and title sits in an editable form, and a suggestion below a confidence threshold is flagged `needs_review` automatically.
5. **Export.** Each confirmed group becomes one searchable PDF (local OCR burns in an invisible text layer — the photo itself is never altered), and the source photos are renamed to match, never deleted.

Everything except the optional AI step runs 100% locally: OCR, deskewing, page-boundary detection, orientation correction, PDF assembly.

## Why an AI step, and why two providers

Grouping photographed pages into logical letters and extracting date/sender/subject is exactly the kind of fuzzy, context-dependent task a vision-capable LLM is good at and brittle heuristics are not. But sending someone's mail to a third party is not a decision software should make quietly.

So the design is:

- **Nothing is sent without an explicit, per-batch confirmation dialog** that names the exact provider and files involved.
- **No API key is bundled or required.** The app shells out to a CLI the user already has installed and authenticated — [Codex CLI](https://github.com/openai/codex) (ships inside ChatGPT.app) or [Claude Code](https://github.com/anthropics/claude-code) — so usage is billed against the user's own **ChatGPT or Claude subscription**, not a service the developer pays for.
- **Either provider, chosen per session.** A dropdown in the UI switches between them; the confirmation dialog and the on-disk evidence trail update accordingly. See [`docs/TECHNICAL.md#dual-ai-provider-design`](docs/TECHNICAL.md#dual-ai-provider-design) for how each subprocess is sandboxed (no tools, no filesystem access, no session persistence, schema-enforced JSON output).
- **The model never gets filesystem access.** It receives exactly the photos and OCR text for the files the user checked, replies with structured JSON validated against a strict schema, and every returned file ID is cross-checked against what was actually sent before anything reaches the UI.

## Architecture at a glance

```
┌─────────────────────────────────────────────────────┐
│  macOS .app bundle (double-click to launch)          │
│  Contents/MacOS/start  → spawns Python, opens browser│
└───────────────────────┬───────────────────────────────┘
                         │
          ┌──────────────▼───────────────┐
          │  Python stdlib HTTP server    │
          │  127.0.0.1 only, random token │
          └──────────────┬───────────────┘
                          │ JSON over localhost
          ┌──────────────▼───────────────┐
          │  Vanilla JS single-page UI    │
          │  (no build step, no framework)│
          └───────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
  Local image        Local OCR /        Optional AI
  processing         PDF assembly       classification
  (Pillow)           (PyMuPDF +         (Codex CLI /
                       Tesseract)        Claude Code CLI)
```

No database, no framework, no build tooling, no external network dependency for the core loop. Full breakdown: [`docs/TECHNICAL.md`](docs/TECHNICAL.md).

## Security & privacy posture

- HTTP server binds to `127.0.0.1` only, never `0.0.0.0`.
- Every request requires a random 32-byte token generated per launch; the token is compared with `secrets.compare_digest` (no timing side-channel).
- `Host` and `Origin` headers are checked against the actual bound port to block DNS-rebinding attacks from a malicious webpage.
- Strict `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy: no-referrer` on every response.
- Every filesystem path built from user/AI input is validated to stay inside the chosen folder (defense against `../` traversal from a crafted filename or model output).
- The app never overwrites a file: exports use exclusive-create semantics with full rollback if any step in a multi-file operation fails partway through.

Full threat-model writeup: [`docs/TECHNICAL.md#security-model`](docs/TECHNICAL.md#security-model).

## Engineering practice

This isn't a weekend script — it went through a deliberate, documented repair process on top of an existing working prototype:

| Phase | What happened |
|---|---|
| **Baseline** | Original app frozen as commit 1, before any change |
| **Regression net** | 84 tests written against the *unmodified* code first, to make every later change verifiable |
| **Bug fixes** | Each of 6 verified logic errors fixed behind its own red→green test |
| **Concurrency hardening** | Long AI/export operations moved off the global lock into a background-job model with live progress, so the UI never freezes for minutes |
| **Performance** | Server-side render cache + `ETag`/conditional requests cut a single form edit from 8 image re-fetches to 0 |
| **Two independent review rounds** | 27 additional issues found and fixed by re-reading the *entire* diff a second and third time, not just the parts that had changed |
| **Feature: dual AI provider** | Added Claude as a second backend, tested against a real, live API call with synthetic sample letters before shipping |
| **Live verification** | Not just unit tests — a real end-to-end run against Claude with generated sample letters, checked field-by-field |

**193 automated tests**, all passing, covering the HTTP layer, concurrency behavior, file-system edge cases (replaced files, missing disk space, exFAT/no-hardlink filesystems), and both AI provider code paths with mocked and live calls.

Full change log with rationale for every fix: `git log --oneline` in this repo, or [`docs/TECHNICAL.md#development-history`](docs/TECHNICAL.md#development-history).

## Try it

Prebuilt, ad-hoc-signed `.app` bundle: see [Releases](../../releases). Requires macOS 13+ and Python 3.14 with Pillow + PyMuPDF (the app checks for these itself and shows an install hint if missing).

```bash
git clone <this-repo>
cd inbox-assistent-web
python3 -m pytest tests/   # 193 tests, ~45s
open "Inbox-Assistent Web.app"
```

Full local dev setup: [`docs/TECHNICAL.md#running-locally`](docs/TECHNICAL.md#running-locally).

## License / usage note

Portfolio project. The app processes real personal mail for its author; this repository is published to demonstrate engineering practice, not as a maintained open-source product. No warranty — read the code before trusting it with your own documents.
