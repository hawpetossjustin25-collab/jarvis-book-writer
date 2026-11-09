# Jarvis Book Writer

A Windows desktop app for writing books by talking to it. You tell Jarvis the story you want, by voice or by
typing, and it plans the book, writes chapters the way professional novelists structure them, keeps track of
the characters and plot, reads your books back to you, and turns them into audiobooks. Unfinished books
work too.

All AI runs on **Google Gemini**: writing, planning, editing, the Jarvis voice (Gemini Live), narration
(Gemini TTS) and search (Gemini embeddings).

## Features

| Tab | What it does |
|---|---|
| **Jarvis** | Live voice conversation (Gemini Live). Interrupt it any time. Optional on-device "Hey Jarvis" wake word. Jarvis can open, read aloud, search, write and revise any book in your library. |
| **Chat** | The same assistant by text, with the same tools. History is saved. |
| **Library** | Every book with its genre, word count and chapter status. Write the next chapter(s) in the background. Export DOCX / EPUB / Markdown / M4B / MP3. |
| **Write** | Manuscript editor with autosave and full version history (restore any earlier version). Write or revise a chapter with AI, then *Accept* it so the story bible updates. |
| **Listen** | Audiobook player for any book, finished or not. Passages are recorded ahead of playback and cached, so editing a paragraph only re-records that paragraph. Remembers your place. |
| **Settings** | Gemini key (encrypted with Windows DPAPI), narrator and Jarvis voices, author name, and the adaptive engine's live status, quota and rollback. |
| **War Room** | Go over any book with Jarvis by voice or text in one shared conversation. *Fill me in* briefings that separate what is written from what is only planned, *Find gaps* with options, and ideas Jarvis pitches that stay ideas until you Accept, Edit + accept or Reject them. Every accepted decision can be undone, and *End session + recap* saves the decisions, open questions and next steps. |
| **Publish** | Sign in to YouTube in your browser (no password is given to the app; tokens are encrypted on this PC), then post any book or chapters as a video with the cover, title and a moving waveform. Uploads run in the background, resume after a dropped connection, add chapter timestamps and a per-book playlist, and are marked as AI-narrated. Post history shows links and flags videos whose chapters were edited since. |

## How the writing works

The pipeline follows how professional authors and research systems (Agents' Room, DOC, Re3) write long fiction:

1. **Architect** turns your idea into a book plan: premise, logline, audience, point of view, tense, structure
   (Save the Cat, Hero's Journey, three-act, seven-point, Kishōtenketsu, mystery), themes, characters, places
   and plot threads. Book and chapter lengths come from genre norms (`jarvis_books/craft/genres.py`).
2. **Outliner** writes a chapter-by-chapter outline mapped to the structure's beats.
3. **Scene planner** breaks each chapter into scenes (POV, setting, goal, conflict, outcome, beats).
4. **Writer** drafts scene groups, sized to what the current Gemini model reliably produces in one call, with
   the story bible, a summary of the story so far, the previous chapter's ending and relevant earlier
   passages (keyword + semantic search) in context.
5. **Continuity checks** (deterministic: dead characters acting, tense drift, repeated phrases, AI artifacts,
   formatting leaks, length) and an **editor** review, followed by a **reviser** pass when needed. A truncated
   revision never replaces a good draft.
6. **Curator**: when a chapter is accepted, the summary, character states, new characters and places, and open
   and resolved threads are written into the story bible.

## Storage

- Canonical data: SQLite (WAL) in `%LOCALAPPDATA%\JarvisBooks\library.db`. Every save keeps a revision.
- Readable copies: every book is mirrored as Markdown (outline, story bible, chapters, full manuscript) to
  `OneDrive\Documents\Jarvis Books\`, with rolling database backups in `_Backups`.
- Growth: there's no fixed size limit, so storage grows with free disk. The capacity monitor only ever evicts
  *regenerable audio cache*, least recently used first. Manuscripts and revisions are never deleted
  automatically.

## Adaptive Gemini engine

`jarvis_books/llm/` discovers the models your key can use and verifies their capabilities with small probes.
It sizes each request (system prompt, history, story memory, retrieved passages, output) to each model's real
limits, learns rate and daily quotas from Gemini's responses, retries with backoff, and falls back only to
other Gemini models. Every configuration is versioned, and degraded changes roll back automatically.
Capability probes run in the background, so the app opens immediately.

## Privacy

On Gemini's free tier, Google may use prompts and responses to improve its products. Use a paid
(billing-enabled) key if your manuscripts must stay out of that. The API key is stored encrypted for your
Windows user only and is never written to logs or the OneDrive mirror.

When Gemini's daily speech quota runs out, narration continues with Microsoft Edge neural voices (a free
text-to-speech service, not an AI writer), so audiobooks keep playing.

## Install (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
```

This installs to `%LOCALAPPDATA%\JarvisBookWriter\app`, installs dependencies, runs the test suite and puts a
**Jarvis Book Writer** icon on the OneDrive Desktop. Re-running it updates the app without touching your books.

## Development

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest -q
python -m jarvis_books            # run the app
python -m jarvis_books --smoke-test
```

The tests use a fake Gemini client (`tests/fakes.py`), so they run offline and fast. `JARVIS_BOOKS_HOME` and
`JARVIS_BOOKS_MIRROR` redirect the data folders.

## Layout

```
jarvis_books/
  storage/    SQLite library, revisions, OneDrive mirror, backups, capacity monitor
  craft/      genre profiles, story structures, manuscript rules
  llm/        adaptive Gemini engine (discovery, capabilities, sizing, quotas, monitor, rollback)
  memory/     passage index (FTS5 + embeddings) and context builder
  writer/     multi-agent writing pipeline and continuity checks
  assistant/  tool declarations shared by text chat and voice, function-calling chat
  audio/      narration cache, Gemini/Edge TTS, M4B/MP3 export
  export/     DOCX (standard manuscript format), EPUB 3, Markdown
  voice/      Gemini Live WebSocket session
  ui/         PyQt6 window, audiobook player, mic/speaker bridge
  studio.py   wires everything together
```

## Permissions (yes/no)

Every Jarvis tool has a level in `jarvis_books/assistant/permissions.py`: **Read** and **Safe change** run straight
away; **Change** (writing, rewriting, story bible, outline, exports, accepting ideas), **Outside world**
(YouTube) and **System** wait for the author's yes. The check is in code, not in Jarvis's prompt: the app stores
the exact action, shows a yes/no card in the Jarvis, Chat and War Room tabs, and runs it only on the author's own
typed or spoken yes (or the Yes button). A yes is single-use and expires after 2 minutes. "Don't ask again this
session" exists for Change-level tools only. Every request and answer is logged in Settings.

## Jarvis controls the app

Jarvis can report what the app is doing (`app_status`), switch tabs (`open_tab`), read and change settings
(`get_settings`, `change_setting`), press buttons (`app_action`: mute/unmute, hang up, open a book's folder) and
`undo_last_change` (chapter rewrites, outline/title changes, new chapters, story bible entries, accepted War Room
ideas and settings). Setting changes and undo ask yes/no first. A yes to "undo" only undoes the change it named,
never something newer.

## Upgrading the app by voice

Ask Jarvis for a change to the app itself ("add a word-count goal"). After your yes, `build_upgrade` has Gemini
edit a separate copy of the app (only `.py` files in `jarvis_books/` and `tests/`; the permission gate, the
approval card, the upgrader, key storage and their tests are protected). The whole test suite then runs on that
copy with a throwaway data folder and no secrets in its environment, and failures go back to Gemini, up to 3 tries.
Only a copy whose tests pass can be installed, and only after a second yes (`install_upgrade`). Installing backs up
every file it replaces, and `undo_upgrade` restores them. Settings > App upgrades shows each change's diff and test
results. Updates installed from GitHub replace local upgrades.
