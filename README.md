# Codex Belya

Codex Belya is a voice agentic AI companion for the Codex CLI. Inspired by the Egyptian term for the shop-floor assistant “belya” who keeps the garage humming, this agent keeps your coding workflow on track: you speak the tasks aloud (as we all do anyways when we are in the zone :D ), it coordinates with Codex CLI, keeps meticulous records, and even manages your git branches so you never lose the flow.

---

## Why Belya?

- **Conversational tasking.** Dictate multi-step coding requests and let Belya assemble the perfect prompt for Codex.
- **Full git control.** Check out, create, delete, commit, fetch, pull, push, and switch branches safely—capabilities the Codex CLI alone does not provide.
- **Codex Session memory.** Persist session history, branch context, Codex settings, and task outcomes in SQLite for seamless handoffs.
- **Codex utilization insight.** Capture Codex token usage, rate-limit windows, and auto-notify when you hit 80 / 90 / 95 % of quota.
- **Codex Session administration.** List and resume past Codex sessions, rename sessions, switch approval policies and models, compact context, or set working branches on demand.
- **LiveKit ready.** Works with the Agents Playground or any LiveKit project, re-uses stored room and participant IDs for quicker reconnects.

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.10+ | Tested with 3.10 and 3.11. |
| Node.js 18+ (includes `npx`) | Required to launch the Codex MCP server via `npx codex`. |
| Codex CLI | Install globally with `npm install -g @openai/codex`. |
| LiveKit project | The free tier is sufficient for experimentation, development and testing. |
| OpenAI Platform account | Access to Tier 1 (or higher) is required for API usage. |

### Quick setup

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

1. Ensure `.env` is populated and your Python environment is active.
2. Start the worker:
   ```bash
   python3 main.py start
   ```
3. Open [https://agents-playground.livekit.io/](https://agents-playground.livekit.io/):
   - Populate your .env with your LiveKit project URL, API key, and secret.
   - Join the room that matches the session (defaults to what the worker creates).
   - Begin speaking with Belya. It will:
     - Confirm the current git branch and ask it to create/switch branches.
     - Aggregate coding tasks and dispatch them to Codex.
     - Read back action summaries and prompt for next steps.
     - Manage session history, Codex utilization, and compress Codex context.

---

## Feature Matrix

| Capability | Details |
| --- | --- |
| Session orchestration | Start new Codex sessions, list historical sessions, switch between them, rename sessions, and compact context. |
| Persistent context | Store branch metadata, utilization metrics, and LiveKit room/participant IDs in SQLite. |
| Git automation | Full local control: check current branch, create/delete/switch branches, commit, fetch, pull, push; all via voice commands. |
| Token analytics | Track total tokens, last-task tokens, 5-hour & weekly usage; warn when thresholds are exceeded (you need to provide your user rate limits). |
| Codex configuration | Adjust approval policy and model on demand; defaults to risk-free “never” policy until the user requests changes. |
| Error resilience | Every function tool routes failures back to the voice assistant with user-friendly explanations for the error. |

---

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

- Huge thanks to Cole Medin for the [LiveKit voice assistant tutorial](https://www.youtube.com/watch?v=TXVyxJdlzQs), which provided a solid starting point for me to create the Voice Assistant agent.

---

## TODO

- Add a local frontend for quick microphone control and transcript review (will require local livekit server, but the cloud-hosted playground is working fine for me).
- Add RAG-enabled knowledge grounding using LangChain (project docs, codebase, etc.).
