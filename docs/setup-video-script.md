# Mnemos — Setup How-To Video (handoff package)

Everything a designer / video tool needs: the **design system**, the **scene script**, the
**rendered frames** (in `docs/setup-video-frames/`), and a **crystal-clear install reference**
(the commands the video teaches — copy-paste accurate as of repo `main`).

---

## 1. The brief

| | |
|---|---|
| **Title** | *Mnemos: Connect MCP. Get Continuity.* |
| **Goal** | Viewer goes from zero to (a) a working Mnemos MCP memory server in their AI client, and (b) Mnemos installed into a Hermes agent. |
| **Audience** | Developers / agent-builders, comfortable in a terminal. |
| **Runtime** | ~6 min. Two acts; can also split into two shorts (Act 1 ≈ 3:30 MCP, Act 2 ≈ 2:30 Hermes). |
| **Feeling** | Calm, premium, exact. The pitch is *one connection → durable memory → your agent stops forgetting you.* |

---

## 2. Design system (LOCKED)

Premium, **all-monochrome**, full-bleed near-black — the register of Linear / Vercel / Resend /
Apple / Teenage Engineering / Nothing. **No skeuomorphism** (no terminal window dots, scanlines,
CRT vignette, faux chrome). Whitespace and typography do the work.

- **Ground:** `#08090c`, with a whisper radial lift (`#101218` → `#08090c`). Full-bleed; no bordered frame.
- **Type:** **Inter** = the brand voice (wordmark + tagline only). **JetBrains Mono** = everything technical (commands, labels, config). Two weights (400 / 500). Sentence/lowercase.
- **Structure:** hairlines at 4–8% white; 96px margins; generous negative space.
- **Emphasis = luminance, not color.** Each slide is monochrome; the ONE payoff element is simply the **brightest white** (the `ready`, the `"mnemos"` key, the in-use tool, `mnemos` in the slot). There is no accent hue.
- **The living substrate (signature, on title + close):** an animated canvas connectome — ~300–330 nodes, dormant grey, periodic "thought" cascades that propagate in cold white, and an occasional **new bond** that snaps in (cold blue-white). Content slides carry a faint dim substrate fragment in a corner as connective tissue.
- **Substrate palette consts (rgb):** `DORM 178,186,202` · `LINE 150,160,180` · `HOT 224,231,247` (firing) · `NEW 150,176,228` (new bond). Bg `#08090c`.

---

## 3. The frames (`docs/setup-video-frames/`)

| # | File | Type | What it is |
|---|---|---|---|
| 1 | `01-title.html` | animated (canvas) | Title — living substrate + wordmark + tagline. Open in a browser; tap to fire. |
| 2 | `02-install.svg` | static | Install beat (`pip install` → `mnemos doctor` → **ready**). |
| 3 | `03-connect.svg` | static | Connect to client (`mnemos mcp install claude --write`). |
| 4 | `04-tools.svg` | static | Eight tools; `capture` shown in use. |
| 5 | `05-hermes.svg` | static | Hermes — Sidecar vs Provider. |
| 6 | `06-close.html` | animated (canvas) | Close — fuller settled substrate + CTA. |

All frames are 1280×720 (16:9). The two `.html` files are self-contained — open them in any browser to see the motion (this is the animation reference; a video tool screen-captures or rebuilds them). The `.svg` files render crisply at any scale.

---

## 4. Scene script

Each scene: **[VISUAL]** (use the matching frame) · **[ON-SCREEN]** (the cards/commands) · **[VO]** (narration).

### ACT 1 — Connect MCP, get continuity (~3:30)

**Scene 1 · Title** — `01-title.html`
- **[VISUAL]** The living substrate fires; one new bond snaps in.
- **[VO]** "Every new session, your agent starts from zero. Mnemos fixes that — local-first memory you connect once, over MCP. No cloud account, no database setup. Let's wire it up."

**Scene 2 · Install** — `02-install.svg`
- **[VISUAL]** Commands type in; `mnemos doctor` resolves to checks; the word **ready** lands bright.
- **[VO]** "Install Mnemos — clone it, install with the MCP extra, run the doctor. Green across the board: it's local, SQLite-backed, and needs no external services to remember."

**Scene 3 · Connect** — `03-connect.svg`
- **[VISUAL]** One command writes the client config; the `"mnemos"` entry lights.
- **[VO]** "Connect it to your client. For Claude Desktop, one command writes the config. Restart, and Mnemos is connected. Codex, Cursor, anything else — the same installer prints exactly what to paste."

**Scene 4 · It works** — `04-tools.svg`
- **[VISUAL]** Eight tools; `capture` burns brightest as the agent uses it.
- **[VO]** "Eight simple tools — and your agent learns no ontology to use them. It captures what matters, recalls it next time, corrects what's stale, and answers its own reflections. Paste a short starter prompt and you're done."

### ACT 2 — Add the Hermes plugin (~2:30)

**Scene 5 · Hermes** — `05-hermes.svg`
- **[VISUAL]** Two modes side by side; `mnemos` is the bright element in each.
- **[VO]** "Running Hermes? It has one external memory-provider slot, so Mnemos installs two ways. Sidecar keeps your existing provider and adds Mnemos beside it — the safe default. Provider makes Mnemos the provider. Either way it never touches your SOUL, MEMORY, USER, or AGENTS files."

**Scene 6 · Close** — `06-close.html`
- **[VISUAL]** A fuller, settled substrate — memory accumulated — under the wordmark, tagline, and repo URL.
- **[VO]** "However your agent runs, Mnemos is one connection away from durable memory. Local-first, yours, and it stops your agent from forgetting you."

---

## 5. INSTALL REFERENCE (the canonical instructions — keep these exact)

> These are the commands the video teaches. They are accurate to the current repo. Use them verbatim on the command cards and in the description.

### 5A · Connect the MCP server (any AI client)

**What you get:** your agent gains durable memory — startup context, capture, recall, correction, maintenance — with no cloud account and no database setup.

**Step 0 — Install Mnemos (once):**
```bash
git clone https://github.com/Riley-Coyote/mnemos.git
cd mnemos
python -m pip install -e ".[mcp]"
mnemos doctor          # prints readiness checks — should be all green
```
Published-package alternative (when available): `pipx install "mnemos-continuity[mcp]"`.
(The PyPI distribution is named `mnemos-continuity`; the command and import stay `mnemos`.)

**Step 1 — Connect Mnemos to your client.** Pick your client:

- **Claude Desktop** — writes the config for you:
  ```bash
  mnemos mcp install claude --write
  ```
  Then **restart Claude Desktop**. (To preview the config without writing it: `mnemos mcp install claude`.)

- **Codex**:
  ```bash
  mnemos mcp install codex
  ```
  This **prints** a `codex mcp add …` command — run that, then restart Codex.

- **Cursor / any other MCP client**:
  ```bash
  mnemos mcp install cursor      # or: mnemos mcp install generic
  ```
  This **prints a JSON snippet** — paste it into the client's MCP config, then restart.

**Step 2 — Verify.** Reopen the client. Mnemos's eight tools should be available:
`mnemos_context` · `mnemos_capture` · `mnemos_recall` · `mnemos_correct` · `mnemos_reflect` · `mnemos_maintain` · `mnemos_introduce` · `mnemos_health`.

**Step 3 — (optional) Tell the agent to use it** — paste this once:
```text
You have access to Mnemos MCP memory tools.
At the start of this session, call mnemos_context.
If Mnemos asks you to introduce yourself, call mnemos_introduce with your own model id and name.
Use mnemos_capture for stable preferences, decisions, project state, workflows, corrections, and context I should not have to repeat.
Use mnemos_recall before relying on memory from prior sessions.
Use mnemos_correct when a remembered fact is stale, wrong, superseded, or should be forgotten.
If the context packet asks you something about your own memory, answer it with mnemos_reflect, in your own words.
Use mnemos_health if I ask whether memory is working.
Do not mention tools unless I ask. Just use the memory system quietly and tell me what you remembered when it matters.
```

**Simple vs Advanced mode:** Simple (the default, 8 tools) is right for almost everyone. Need the
operator access? Use the supported CLI. The older advanced MCP surface is
experimental and blocked by default in 0.2.x.

### 5B · Install Mnemos into a Hermes agent

**The one decision first.** Hermes has exactly **one external `memory.provider` slot**, so Mnemos
installs in one of two modes. Choose based on whether that slot is already taken:

| Mode | Choose it when… | What it changes |
|---|---|---|
| **Sidecar** — *safe default* | Hermes already uses a provider (Honcho, Supermemory, Mem0, Hindsight, …). | **Preserves** `memory.provider`; adds Mnemos **beside** it via Hermes MCP. |
| **Provider** | You want Mnemos to **be** the memory provider. | Sets `memory.provider: mnemos` and writes the provider shim. |

> **Safety (both modes):** Mnemos **never overwrites** `SOUL.md`, `MEMORY.md`, `USER.md`,
> `AGENTS.md`, or project context files. Hermes's built-in `MEMORY.md` / `USER.md` stay active.

**Step 0 — Be in a persistent Mnemos checkout** (so the `mnemos` command still exists after Hermes restarts):
```bash
git clone https://github.com/Riley-Coyote/mnemos.git    # if not already present
cd mnemos
```

**Step 1 — Install. Choose ONE:**

- **Sidecar (recommended):**
  ```bash
  mnemos hermes quickstart --agent-safe
  mnemos hermes doctor
  ```
  `--agent-safe` is non-interactive: it **preserves** any existing provider, configures **only** the
  MCP sidecar, **refuses** risky provider replacement, and **reports** exactly what changed and what
  was preserved.

- **Provider (only if Mnemos should own the slot):**
  ```bash
  mnemos hermes quickstart --provider
  mnemos hermes doctor
  ```
  This sets:
  ```yaml
  memory:
    provider: mnemos
  ```

**Step 2 — Verify** with `mnemos hermes doctor` — it prints what changed and what was preserved.

**Step 3 — Restart Hermes.**

More detail: `HERMES_INSTALL.md` and `docs/hermes-integration.md` in the repo.

### 5C · Quick command appendix
```bash
# install
git clone https://github.com/Riley-Coyote/mnemos.git && cd mnemos
python -m pip install -e ".[mcp]"      # or: pipx install "mnemos-continuity[mcp]"
mnemos doctor

# MCP into a client (simple mode = default)
mnemos mcp install claude --write       # Claude Desktop (writes config)
mnemos mcp install codex                # prints a `codex mcp add …` command
mnemos mcp install cursor               # prints JSON to paste
mnemos mcp install generic              # prints JSON to paste
MNEMOS_ENABLE_EXPERIMENTAL=1 mnemos serve --mode advanced  # unsupported research only

# Hermes
mnemos hermes quickstart --agent-safe   # Sidecar (safe default)
mnemos hermes quickstart --provider     # Provider (mnemos becomes the provider)
mnemos hermes doctor                    # verify; reports changed vs preserved
```
