# Jarvis Book Writer: brief for coding agents

Read this before changing anything. It says what the app is, how it is built, how to test and install it, and
what is unfinished. The owner is Justin (GitHub `hawpetossjustin25-collab`). He is not a programmer: explain
results in plain words and do the setup yourself instead of asking him to.

## What it is

One Windows desktop app (PyQt6, Python 3.12) where Jarvis, a voice and chat assistant, writes and manages
books and also runs Justin's computer, the web, research and tax-sale deal finding. **Every new feature goes into
this app, never a separate one** ("keep evrything with jarvis").

## Where things are

| What | Where |
|---|---|
| Code (GitHub) | https://github.com/hawpetossjustin25-collab/jarvis-book-writer, branch `main` |
| Installed app on the laptop | `%LOCALAPPDATA%\JarvisBookWriter\app` (replaced by every install; don't edit it in place) |
| Books, settings, keys | `%LOCALAPPDATA%\JarvisBooks` (`library.db` SQLite WAL, DPAPI-sealed `gemini.key` and `groq.key`) |
| Readable Markdown copies | `OneDrive\Documents\Jarvis Books` (`_Research` holds the researcher's notebooks) |
| Install, check scripts, logs | `%LOCALAPPDATA%\DevinConnect` (`jb_res_install.ps1` pins a commit in `$sha`; logs `install.log` (UTF-16), `res_install.log`, `res_check.out`) |
| Python | `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` |
| Local models | Ollama on `localhost:11434`: `qwen3:4b-instruct-2507-q4_K_M`, `hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M` (send `"think": false`) |

The laptop is an Intel i3-7100U with 2 cores, 16 GB RAM and no GPU, so local models are slow (about 4 tokens a second).

## Layout

```
jarvis_books/
  studio.py     wires everything together (engines, brain, researcher, upgrader, planner)
  llm/          adaptive engine: gemini.py, groq.py, local.py (Ollama), brain.py (fallback between them),
                discovery, capabilities, sizing, quotas, monitor, versioned config with rollback
  assistant/    tool declarations + permissions.py (yes/no gate in code), computer/desktop/planner/research tools
  computer/     files, terminal, web.py (search, read pages, downloads, GitHub), desktop control, OCR,
                taxliens.py (DeKalb, Fulton, Maricopa, Hennepin), online_sales.py, deal_scan.py
  research.py   24/7 background researcher (topics, interest detection, sourced findings, ResearchClock)
  upgrade.py    self-upgrade: edits a copy, runs the full test suite, retries until it passes
  writer/ craft/ memory/ storage/ audio/ voice/ publish/ export/ ui/   books, memory, narration, UI tabs
tests/          pytest suite (fake Gemini in tests/fakes.py, runs offline)
```

## Rules for changes

- Production quality only: real structure, no stubs or scaffolding, and tests that prove it works.
- Run the whole suite before saying anything works: `QT_QPA_PLATFORM=offscreen python -m pytest -q`
  (on Windows leave `QT_QPA_PLATFORM` unset). Add tests for every change.
- Any LLM work follows the adaptive engine: detect the model, size prompts to its real limits, fall back, and
  never hardcode budgets.
- Never commit, print or log keys. They live sealed in `%LOCALAPPDATA%\JarvisBooks`.
- Keep the permission gate: Change, Outside world and System tools wait for Justin's yes unless he turned on
  Full control. Catastrophic actions stay blocked even then.
- Tax-sale amounts: keep exact amounts (DeKalb tax due, GovEase starting bids) separate from estimates
  (Fulton and Maricopa yearly tax from assessed value). Never make up prices or coverage.
- Only report what you actually saw in test output or logs.

## Brains

- **Gemini** (free key) is the main brain for chat, voice (Gemini Live), writing, TTS and upgrades. The free daily
  quota runs out.
- **Groq** (free key, `openai/gpt-oss-120b` for chat, `openai/gpt-oss-20b` for research): about 8,000 tokens a
  minute. That's fine for chat and research but too small for upgrades.
- **Qwen3-4B** on the laptop: the researcher's default brain, and it writes upgrades after Gemini has been out for
  2 hours (`FALLBACK_AFTER_S` in `upgrade.py`).
- **LFM3** on the laptop: last-resort backup.

## Done and tested on the laptop

Long books (thousands of chapters), long-term memory and recursive learning, reading speed, character voices
stages 1 and 2, computer control with a Full control switch, web search, downloads and GitHub, the planner,
reminders and alarm, self-upgrades that keep fixing until the tests pass, tax sales (Atlanta/DeKalb and Fulton,
Phoenix/Maricopa, Minneapolis/Hennepin, any state, online auctions), the nationwide best-deals scan, clickable
links, the researcher's interest detection, and Groq chat with Gemini taking over.

## Unfinished

- Researcher: the laptop's internet address is treated as a robot by DuckDuckGo (202 robot check), Bing
  (results for the first word only), Google, Startpage and Mojeek, so web search falls back to Groq's
  browser_search tool (GroqSearch in llm/groq.py) and rests Bing after junk. Notes on the laptop still need checking.
- Character voices stage 3 (recast with 'different', swap_voices, hear_voice samples, player records ahead
  after a recast) needs checking on the laptop with real Gemini speech.
- YouTube upload: needs Justin's Google sign-in (OAuth client) and a private test upload.
- Web limits: DuckDuckGo blocks quick searches, Bid4Assets answers 403, and Zeus and RealAuction don't publish
  parcel prices.
- The Groq key was pasted in a chat, so it should be rotated.
