# Code Review Agent 🔍

A LangGraph-based code review agent that analyzes GitHub repos or local files for bugs, security issues, code quality, performance, and suggestions.

## Architecture

```
fetch_code → chunk_files → review_code → generate_report
     ↓ (error)
  error_node
```

**Nodes:**
| Node | Job |
|------|-----|
| `fetch_code` | Fetch files from GitHub URL or local path |
| `chunk_files` | Split large files into 150-line chunks |
| `review_code` | LLM reviews each chunk (parallel-ready) |
| `generate_report` | Aggregates results into a markdown report |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in OPENAI_API_KEY in .env
```

## Usage

```bash
# Review a GitHub repo
python agent.py https://github.com/Sujal-781/Expensio

# Review a local folder
python agent.py ./my_project/

# Review a single file
python agent.py ./main.py
```

## Output

The agent generates `review_report.md` with:
- **Summary table** — severity per file
- **Per-file breakdown** — bugs, security, quality, performance, suggestions
- **Overall stats** — total issues count

## Supported File Types

`.py`, `.js`, `.ts`, `.java`, `.cpp`, `.go`, `.rb`, `.cs`, `.php`, `.rs`, `.kt`, `.swift`, `.sql`, `.sh`, `.yaml`, `.json`, and more.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | ✅ Yes | Your OpenAI API key |
| `GITHUB_TOKEN` | ⚡ Optional | GitHub PAT for private repos / higher rate limits |
