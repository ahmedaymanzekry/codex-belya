<p align="center">
  <img src="./resources/Codex Belya logo.png" alt="Codex Belya logo" width="300" height="300">
</p>
<meta name="image" property="og:image" content="[https://github.com/ahmedaymanzekry/codex-belya/raw/main/resources/Codex Belya logo.png]">

Codex Belya is the first "Voice Coding" agent made for professionals (Think of it like Iron Man's Jarvis from 2008). Belya is a voice agentic AI companion for the Codex CLI. Belya is mainly made for developers to enhance their experience of using Codex CLI or IDE extension.
The name is inspired by the Egyptian slang term for the shop-floor assistant “belya” who keeps the mechanic's garage organized and flowing. Likewise, Belya, the voice assistant agent, keeps your coding workflow light, interactive, and on track.
So, you basically talk to your code (as we all do anyways when we are in the zone :D ), Belya then coordinates with Codex CLI, keeps meticulous records, and even manages your git branches so you never lose the flow.

---

## Why Belya?

- **Multilingual Conversations.** Dictate multi-step coding requests and let Belya assemble the perfect prompt for Codex. Talk to it in your own language ([supported languages](https://platform.openai.com/docs/guides/speech-to-text/supported-languages#supported-languages))
- **Full git control.** Check status/diff/add/restore/reset/stash/merge, check out, create, delete, commit, fetch, pull, push, and switch branches safely—capabilities the Codex CLI alone does not provide.
- **Codex Session memory.** Persist session history, branch context, Codex settings, and task outcomes in SQLite for seamless handoffs.
- **Codex utilization insight.** Capture Codex token usage, rate-limit windows, and auto-notify when you hit 80 / 90 / 95 % of quota.
- **Codex Session administration.** List and resume past Codex sessions, rename sessions, switch approval policies and models, compact context, or set working branches on demand.
- **LiveKit ready.** Works with the Agents Playground or any LiveKit project, re-uses stored room and participant IDs for quicker reconnects.

---

## Accessibility & Voice-First Coding

Belya lets you operate your development workflow **hands-free**, which can meaningfully help developers with **vision challenges or limited mobility**. You can create and switch branches, stage/commit, review diffs, and dispatch tasks to Codex **entirely by voice**. The agent reads back confirmations before any risky action, summarizes diffs and errors in natural language, and logs transcripts for later review. So, please recommend Belya to any friend or colleage who would benefit from its features in his coding experience.

**Note:** initial Git credential and environment setup and rare complex merge conflicts may still require manual steps, but Belya will guide you verbally and confirm destructive commands with you before execution.

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.10+ | Tested with 3.10 and 3.11. |
| Node.js 18+ (includes `npx`) | Required to launch the Codex MCP server via `npx codex`. |
| Codex CLI | Install globally with `npm install -g @openai/codex`. |
| LiveKit API key and project | The free tier is sufficient for experimentation, development, testing and loads of work. |
| OpenAI Platform account and API key | Access to Tier 1 (or higher) is required for API usage. Rate limits are tier-dependant. |

### Prerequisite setup

```bash
# Python environment
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Node & Codex CLI
node --version             # confirm >= 18
npx --version
npm install -g codex       # optional; you can rely on npx -y codex instead
```

---

## Environment Variables

Create a `.env` file in the project root with the following values:

```
# LiveKit credentials (from https://cloud.livekit.io)
LIVEKIT_URL=https://<your-project>.livekit.cloud
LIVEKIT_API_KEY=<your-livekit-api-key>
LIVEKIT_API_SECRET=<your-livekit-api-secret>

# OpenAI (tested with Tier 1 access)
OPENAI_API_KEY=<your-openai-api-key>
```

> **LiveKit hint:** The free tier easily covers development and light usage.  
> **OpenAI hint:** Ensure the account is on a paid tier; Tier 1 was used during testing.

---

## Python Dependencies

Dependencies are listed in `requirements.txt`:

```
gitpython>=3.1.43
livekit-agents>=0.6.0
livekit-plugins-openai>=0.6.0
livekit-plugins-silero>=0.6.0
livekit-plugins-noise-cancellation>=0.6.0
python-dotenv>=1.0.1
```

Install them with:

```bash
pip install -r requirements.txt
```

---

## Installation Steps

1. **Clone the repository**
   ```bash
   git clone git@github.com:ahmedaymanzekry/codex-belya.git
   cd codex-belya
   ```
   You will need Github SSH keys configured.
2. **Create and activate a virtual environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
   You can also use your project's environment, but don't forget to install the requirements.
3. **Install Python requirements**
   ```bash
   pip install -r requirements.txt
   ```
4. **Verify Node and Codex CLI**
   ```bash
   node --version
   npx --version
   npx -y codex --help
   ```
5. **Configure environment variables**
   Create and edit .env with LiveKit & OpenAI keys as described above
   
---

## Running the Voice Assistant

1. Ensure `.env` is populated as described above and your Python environment is active.
2. Start Belya inside your project's repository (can be done through your editor's terminal):
   ```bash
   cd <path/to/your/current/project>
   python3 <path-to-belya-cloned-repository>/main.py start
   ```
3. Open [LiveKit Agents Playground](https://agents-playground.livekit.io/):
   - Create an account and start a project if you haven't already.
   - Join the room that matches the session (defaults to what the worker creates when you press 'connect').
   - Begin speaking with Belya.
   - Belya will:
     - Confirm the current git branch and ask if you want it to create/switch branches.
     - Aggregate coding tasks and dispatch them to Codex.
     - Read back Codex action and testing summaries and prompt for next steps.
     - Manage session history, Codex utilization, and compress Codex context.
4. Open your code editor and watch the magic happen (see your Codex code edits and reviews brought to life!).
5. Sit back! Relax! and start professional "Voice Coding".
---

## Feature Matrix

| Capability | Details |
| --- | --- |
| Session orchestration | Start new Codex sessions, list previous sessions, switch between them, rename sessions, and compact session context for an optimized Codex utilization. |
| Persistent context | Store branch metadata, utilization metrics, and LiveKit room/participant IDs in SQLite. |
| Git automation | Full local control: status, diff, add, restore, reset, stash, merge, mv, rm, clean, check current branch, create/delete/switch branches, commit, fetch, pull, push—all via voice commands. |
| Token analytics | Track total tokens, last-task tokens, 5-hour & weekly usage; warn when thresholds are exceeded (you need to tell Belya your user rate limits). |
| Codex configuration | Adjust approval policy and model on demand; defaults to risk-free “never” policy until the user requests changes. |
| Error resilience | Every function tool routes failures back to the voice assistant with user-friendly explanations for the error. |

---

## Voice Git Commands

- `status` – Report staged, unstaged, untracked, and ignored files plus the active branch.
- `add` – Stage specific paths or everything if no path is provided.
- `diff` – Summarize line additions/removals and include the raw staged/unstaged patches for deeper inspection.
- `restore` – Discard working tree changes or unstage files without touching the worktree.
- `reset` – Unstage paths or move the current branch with soft/mixed/hard/keep/merge resets.
- `stash` – Push, list, pop, apply, drop, or clear stash entries (optionally include untracked files).
- `merge` – Merge another branch into the current one with optional `--no-ff` or `--squash`.
- `mv` – Rename or move tracked files while keeping git history intact.
- `rm` – Remove tracked files from the index and working tree (`force` supported).
- `clean` – Delete untracked files (and optionally directories) when you confirm with `force=True`.
- Branch helpers – `check_current_branch`, `create_branch`, `delete_branch`, `switch_branch`, `push_branch`, `fetch_updates`, `pull_updates`, `commit_changes`.

## Example Workflow

```text
You: “Belya, let's start a new session for the auth refactor.”
Belya: “Sure, the repo is on branch main. Use the existing branch or create a new one?”
You: “Create feature/auth-refactor. Then send Codex a task…”
Belya: “Sounds Great! The new branch was created. The prompt I'm going to send to Codex is as follows:...”
You: “Go ahead and send it.”
Belya: “...”
Belya: “Codex finished your tasks. Codex has done the following:... Codex recommends... Would you like to start another coding task?”
```

Behind the scenes, Belya queues your instructions, issues the Codex prompt, tracks token usage, and logs everything for future recall.

---

## Acknowledgements

- Huge thanks to Tim from "Tech with Tim" channel for the [LiveKit voice assistant tutorial](https://www.youtube.com/watch?v=DNWLIAK4BUY). He provided me with a starting point to create the Voice Assistant agent.

---

## TODO

- Add a local frontend (will require local livekit server, but the cloud-hosted playground is working fine for now).
- Add RAG-enabled knowledge grounding using LangChain (project docs, codebase, etc.).
---

## License

Copyright 2025 Ahmed Zekry

Licensed under the MIT License.
