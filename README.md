# Code Review Agent 🔍

A LangGraph-based AI agent that automatically reviews code from any GitHub repository or Pull Request, applies fixes, and submits a PR — with full LangSmith observability.

---

## Features

- **Full repo review** — reviews every source file in a GitHub repo
- **PR review** — reviews only the changed lines in a Pull Request
- **Local review** — reviews files or folders on your machine
- **Auto-fix mode** — applies LLM-suggested fixes and submits a new PR automatically
- **LangSmith observability** — traces every LLM call, token usage, latency, and cost in real time

---

## Architecture

```
         detect_mode
              │
    ┌─────────┼─────────┐
  "repo"    "pr"     "local"
    │         │         │
fetch_repo fetch_pr fetch_local
    └─────────┼─────────┘
         (error check)
              │
         chunk_files
              │
         review_code
              │
    ┌─────────┴─────────┐
  --fix?              no fix
    │                   │
apply_fixes      generate_report
    │
submit_fix_pr
    │
generate_report
```

### Nodes

| Node | Responsibility |
|------|---------------|
| `detect_mode` | Auto-detects input as repo URL, PR URL, or local path |
| `fetch_repo` | Walks GitHub repo tree recursively, downloads all source files |
| `fetch_pr` | Fetches PR metadata + per-file diffs from GitHub API |
| `fetch_local` | Reads files from local file or folder |
| `chunk_files` | Splits large files into 150-line chunks for LLM context limits |
| `review_code` | Sends each chunk to LLM, gets structured JSON review per chunk |
| `apply_fixes` | Sends each file + its issues to LLM, gets back corrected code |
| `submit_fix_pr` | Creates branch, commits fixed files, opens PR via GitHub API |
| `generate_report` | Aggregates all reviews into a markdown report with severity table |
| `error_node` | Catches fetch errors and exits gracefully |

---

## How Auto-Fix Works

1. After review, each file's issues (bugs, security, quality, performance) are collected
2. The LLM receives the original file + all issues and returns the complete corrected file
3. A new branch `auto-fix/code-review-<timestamp>` is created on GitHub
4. Each fixed file is committed to that branch
5. A PR is opened from that branch → main with a summary of all changes

## How LangSmith Observability Works

- Setting `LANGCHAIN_TRACING_V2=true` activates LangChain's built-in tracing
- The entire graph runs inside a `tracing_v2_enabled` context
- Every LLM call is automatically intercepted and sent to LangSmith — no manual logging needed
- The LangSmith dashboard shows every node execution, LLM input/output, token count, latency, and total cost per run

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
Create a `.env` file:
```
OPENAI_API_KEY=sk-proj-xxxx
GITHUB_TOKEN=ghp_xxxx

# LangSmith observability (optional)
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__xxxx
LANGCHAIN_PROJECT=code-review-agent
```

**Getting API keys:**
- `OPENAI_API_KEY` → https://platform.openai.com/api-keys
- `GITHUB_TOKEN` → GitHub Settings → Developer Settings → Personal Access Tokens (Classic) → select `repo` scope
- `LANGCHAIN_API_KEY` → https://smith.langchain.com → Settings → API Keys

---

## Usage

```bash
# Review a full GitHub repo
python agent.py https://github.com/Sujal-781/EasySettle

# Review a specific PR
python agent.py https://github.com/Sujal-781/EasySettle/pull/1

# Review a local file or folder
python agent.py ./src/main.py

# Review + auto-fix + submit PR
python agent.py https://github.com/Sujal-781/EasySettle --fix

# PR review + auto-fix
python agent.py https://github.com/Sujal-781/EasySettle/pull/1 --fix
```

---

## Output

Every run generates `review_report.md` containing:

- **Summary table** — severity level per file (🔴 Critical / 🟠 High / 🟡 Medium / 🟢 Low)
- **Detailed findings** — bugs, security issues, quality problems, performance issues, suggestions per file
- **Overall stats** — total issue counts across all files
- **Auto-fix PR link** — if `--fix` was used

---

## Supported File Types

`.py` `.js` `.ts` `.jsx` `.tsx` `.java` `.cpp` `.c` `.cs` `.go` `.rb` `.php` `.rs` `.kt` `.swift` `.scala` `.html` `.css` `.sh` `.yaml` `.yml` `.json` `.sql`

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | ✅ Yes | OpenAI API key for LLM calls |
| `GITHUB_TOKEN` | ✅ Yes (for write) | GitHub classic token with `repo` scope |
| `LANGCHAIN_TRACING_V2` | ⚡ Optional | Set to `true` to enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | ⚡ Optional | LangSmith API key |
| `LANGCHAIN_PROJECT` | ⚡ Optional | LangSmith project name (default: `code-review-agent`) |

---

## Tech Stack

- **LangGraph** — agent graph orchestration and state management
- **LangChain** — LLM abstraction and tracing integration
- **OpenAI GPT-4o-mini** — code review and fix generation
- **GitHub REST API** — repo/PR fetching, branch creation, commits, PR submission
- **LangSmith** — observability, tracing, token usage tracking