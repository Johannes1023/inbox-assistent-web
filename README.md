# Inbox-Assistent Web

A local-first macOS app that turns a phone-camera pile of photographed letters into organized, searchable PDFs, with an **optional, explicitly consented** AI classification step that runs entirely through the user's own ChatGPT or Claude subscription. No bundled API key.

![Platform](https://img.shields.io/badge/platform-macOS-black) ![Python](https://img.shields.io/badge/python-3.14-blue) ![Tests](https://img.shields.io/badge/tests-199%20passing-brightgreen) ![License](https://img.shields.io/badge/privacy-local--first-green)

---

## The problem

In Germany, official correspondence (tax notices, utility bills, insurance, authorities) is still routinely sent as physical paper mail rather than email, even in 2026. Photographing it on the go, on the train, at the mailbox, in a waiting room, is the practical way to not lose it. But physical mail doesn't stop existing just because you photographed it. A phone full of `IMG_4101.jpg`, `IMG_4102.jpg`, `IMG_4103.jpg`, some of them pages of the same letter and in no particular order, with no date, sender, or subject attached, isn't an archive. It's a backlog.

Sorting it by hand means figuring out which photos belong together, fixing the crooked angle from a hasty phone snapshot, and typing out a consistent filename (`date_sender_subject.pdf`) for every letter, twice a week, forever.

## What it does

1. **Pick a folder.** The app works only inside a folder you explicitly choose via the native macOS folder picker. It never sees the rest of your filesystem.
2. **Drop in photos.** JPG, JPEG, HEIC, PNG. Every import is verified byte-for-byte against the source file, so nothing is trusted just because a browser said so.
3. **Group pages into letters**, either by hand or by asking an AI model to do it.
4. **Review and confirm.** AI suggestions are never applied silently: every suggested date, sender, and title sits in an editable form, and a suggestion below a confidence threshold is flagged `needs_review` automatically.
5. **Export.** Each confirmed group becomes one searchable PDF (local OCR burns in an invisible text layer, the photo itself is never altered), and the source photos are renamed to match, never deleted.

Everything except the optional AI step runs entirely locally: OCR, deskewing, page-boundary detection, orientation correction, PDF assembly.

## Why an AI step, and why two providers

Grouping photographed pages into logical letters and pulling out date, sender, and subject is exactly the kind of fuzzy, context-dependent task a vision-capable LLM handles well and brittle heuristics don't. But sending someone's mail to a third party isn't a decision software should make quietly, so the design works like this:

- **Nothing is sent without an explicit, per-batch confirmation dialog** that names the exact provider and files involved.
- **No API key is bundled or required.** The app shells out to a CLI the user already has installed and authenticated, [Codex CLI](https://github.com/openai/codex) (ships inside ChatGPT.app) or [Claude Code](https://github.com/anthropics/claude-code), so usage is billed against the user's own **ChatGPT or Claude subscription**, not a service the developer pays for.
- **Either provider, chosen per session.** A dropdown in the UI switches between them, and the confirmation dialog and the on-disk evidence trail update to match. See [`docs/TECHNICAL.md#dual-ai-provider-design`](docs/TECHNICAL.md#dual-ai-provider-design) for how each subprocess is sandboxed: no tools, no filesystem access, no session persistence, schema-enforced JSON output.
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

No database, no framework, no build tooling, no external network dependency for the core loop. Full breakdown in [`docs/TECHNICAL.md`](docs/TECHNICAL.md).

## Security and privacy posture

- HTTP server binds to `127.0.0.1` only, never `0.0.0.0`.
- Every request requires a random 32-byte token generated per launch, compared with `secrets.compare_digest` so there's no timing side-channel.
- `Host` and `Origin` headers are checked against the actual bound port to block DNS-rebinding attacks from a malicious webpage.
- Strict `Content-Security-Policy`, `X-Content-Type-Options`, and `Referrer-Policy: no-referrer` on every response.
- Every filesystem path built from user or AI input is validated to stay inside the chosen folder, as defense against `../` traversal from a crafted filename or model output.
- The app never overwrites a file. Exports use exclusive-create semantics with full rollback if any step in a multi-file operation fails partway through.

Full threat-model writeup in [`docs/TECHNICAL.md#security-model`](docs/TECHNICAL.md#security-model).

## Engineering practice

This went through a deliberate, documented repair process on top of an existing working prototype:

| Phase | What happened |
|---|---|
| **Baseline** | Original app frozen as commit 1, before any change |
| **Regression net** | 84 tests written against the *unmodified* code first, to make every later change verifiable |
| **Bug fixes** | Each of 6 verified logic errors fixed behind its own red-to-green test |
| **Concurrency hardening** | Long AI/export operations moved off the global lock into a background-job model with live progress, so the UI never freezes for minutes |
| **Performance** | Server-side render cache plus `ETag`/conditional requests cut a single form edit from 8 image re-fetches to 0 |
| **Two independent review rounds** | 27 additional issues found and fixed by re-reading the entire diff a second and third time, not just the parts that had changed |
| **Feature: dual AI provider** | Added Claude as a second backend, tested against a real, live API call with synthetic sample letters before shipping |
| **Live verification** | A real end-to-end run against Claude with generated sample letters, checked field by field, not just unit tests |

**199 automated tests**, all passing, covering the HTTP layer, concurrency behavior, file-system edge cases (replaced files, missing disk space, exFAT/no-hardlink filesystems), and both AI provider code paths with mocked and live calls.

Full change log with rationale for every fix: `git log --oneline` in this repo, or [`docs/TECHNICAL.md#development-history`](docs/TECHNICAL.md#development-history).

## Try it

Prebuilt, ad-hoc-signed `.app` bundle: see [Releases](../../releases). Requires macOS 13+ and Python 3.14 with Pillow and PyMuPDF (the app checks for these itself and shows an install hint if missing).

```bash
git clone <this-repo>
cd inbox-assistent-web
python3 -m pytest tests/   # 199 tests, ~75s
open "Inbox-Assistent Web.app"
```

Full local dev setup in [`docs/TECHNICAL.md#running-locally`](docs/TECHNICAL.md#running-locally).

## License

MIT. Free to use, copy, modify, distribute. No warranty; read the code before trusting it with your own documents.
