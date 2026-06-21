"""
Code Review Agent using LangGraph
Supports:
  - Full GitHub repo review
  - GitHub PR diff review
  - Local file / folder review
Checks: Code quality, bugs, security, performance, suggestions
"""

import os
import re
import json
import requests
from pathlib import Path
from typing import TypedDict, List, Optional
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

load_dotenv()

# ─────────────────────────────────────────────
# State Definition
# ─────────────────────────────────────────────

class ReviewState(TypedDict):
    source: str                    # GitHub URL, PR URL, or local path
    mode: str                      # "repo" | "pr" | "local"
    pr_metadata: dict              # PR title, author, branch info (PR mode only)
    files: List[dict]              # [{"name": ..., "content": ..., "patch": ...}]
    chunks: List[dict]             # chunked units for LLM review
    reviews: List[dict]            # LLM review results per chunk
    final_report: str              # Markdown report
    error: Optional[str]

# ─────────────────────────────────────────────
# LLM + Constants
# ─────────────────────────────────────────────

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cpp", ".c",
    ".cs", ".go", ".rb", ".php", ".rs", ".kt", ".swift", ".scala",
    ".html", ".css", ".sh", ".yaml", ".yml", ".json", ".sql"
}

CHUNK_SIZE = 150  # lines per chunk

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}

# ─────────────────────────────────────────────
# Helper: GitHub headers
# ─────────────────────────────────────────────

def gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

# ─────────────────────────────────────────────
# Node 0: Detect Mode
# ─────────────────────────────────────────────

PR_PATTERN = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")
REPO_PATTERN = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+))?(/.*)?$")

def detect_mode(state: ReviewState) -> ReviewState:
    """Detect whether the source is a PR URL, repo URL, or local path."""
    source = state["source"].strip()

    if PR_PATTERN.match(source):
        print(f"[detect_mode] Mode: PR review → {source}")
        return {**state, "mode": "pr"}
    elif source.startswith("https://github.com/"):
        print(f"[detect_mode] Mode: Full repo review → {source}")
        return {**state, "mode": "repo"}
    else:
        print(f"[detect_mode] Mode: Local file/folder → {source}")
        return {**state, "mode": "local"}

# ─────────────────────────────────────────────
# Node 1a: Fetch Full Repo
# ─────────────────────────────────────────────

def fetch_repo(state: ReviewState) -> ReviewState:
    """Fetch all supported files from a GitHub repo."""
    source = state["source"].strip()
    match = REPO_PATTERN.match(source)
    if not match:
        return {**state, "error": f"Cannot parse GitHub URL: {source}"}

    owner, repo, branch, path_suffix = match.groups()
    branch = branch or "main"
    base_path = (path_suffix or "").lstrip("/")
    api_base = f"https://api.github.com/repos/{owner}/{repo}/contents"
    headers = gh_headers()

    files = []
    _walk_tree(api_base, base_path, headers, files, branch)

    if not files:
        return {**state, "error": "No supported source files found in repo."}

    print(f"[fetch_repo] Fetched {len(files)} file(s)")
    return {**state, "files": files, "error": None}


def _walk_tree(api_base, path, headers, files, branch, depth=0):
    if depth > 3:
        return
    url = (f"{api_base}/{path}" if path else api_base) + f"?ref={branch}"
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print(f"[_walk_tree] {resp.status_code} → {url}")
        return
    data = resp.json()
    if isinstance(data, dict):
        data = [data]
    for item in data:
        if item["type"] == "file":
            if Path(item["name"]).suffix in SUPPORTED_EXTENSIONS and item.get("size", 0) < 100_000:
                r = requests.get(item["download_url"], headers=headers)
                if r.status_code == 200:
                    files.append({"name": item["path"], "content": r.text, "patch": None})
        elif item["type"] == "dir":
            _walk_tree(api_base, item["path"], headers, files, branch, depth + 1)

# ─────────────────────────────────────────────
# Node 1b: Fetch PR Diff
# ─────────────────────────────────────────────

def fetch_pr(state: ReviewState) -> ReviewState:
    """Fetch changed files and their diffs from a GitHub PR."""
    source = state["source"].strip()
    match = PR_PATTERN.match(source)
    if not match:
        return {**state, "error": "Invalid PR URL format."}

    owner, repo, pr_number = match.groups()
    headers = gh_headers()

    # Fetch PR metadata
    pr_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    pr_resp = requests.get(pr_url, headers=headers)
    if pr_resp.status_code != 200:
        return {**state, "error": f"Could not fetch PR #{pr_number}: {pr_resp.status_code} — {pr_resp.json().get('message', '')}"}

    pr_data = pr_resp.json()
    pr_metadata = {
        "number": pr_number,
        "title": pr_data.get("title", ""),
        "author": pr_data.get("user", {}).get("login", ""),
        "base_branch": pr_data.get("base", {}).get("ref", ""),
        "head_branch": pr_data.get("head", {}).get("ref", ""),
        "state": pr_data.get("state", ""),
        "additions": pr_data.get("additions", 0),
        "deletions": pr_data.get("deletions", 0),
        "changed_files": pr_data.get("changed_files", 0),
        "body": (pr_data.get("body") or "")[:500],  # PR description, capped
    }

    print(f"[fetch_pr] PR #{pr_number}: \"{pr_metadata['title']}\" by @{pr_metadata['author']}")
    print(f"[fetch_pr] +{pr_metadata['additions']} / -{pr_metadata['deletions']} across {pr_metadata['changed_files']} file(s)")

    # Fetch changed files with patches (diffs)
    files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    files_resp = requests.get(files_url, headers=headers)
    if files_resp.status_code != 200:
        return {**state, "error": f"Could not fetch PR files: {files_resp.status_code}"}

    raw_files = files_resp.json()
    files = []

    for f in raw_files:
        filename = f.get("filename", "")
        status = f.get("status", "")   # added, modified, removed, renamed
        patch = f.get("patch", "")     # the unified diff

        # Skip deleted files and unsupported types
        if status == "removed":
            continue
        if Path(filename).suffix not in SUPPORTED_EXTENSIONS:
            continue
        if not patch:
            continue

        # Fetch full file content for context (optional but helps LLM)
        content = ""
        raw_url = f.get("raw_url", "")
        if raw_url:
            content_resp = requests.get(raw_url, headers=headers)
            if content_resp.status_code == 200:
                content = content_resp.text

        files.append({
            "name": filename,
            "status": status,
            "patch": patch,        # diff hunks
            "content": content,    # full file (may be empty if too large)
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        })

    if not files:
        return {**state, "error": "No reviewable file changes found in this PR."}

    print(f"[fetch_pr] {len(files)} file(s) with changes to review")
    return {**state, "files": files, "pr_metadata": pr_metadata, "error": None}

# ─────────────────────────────────────────────
# Node 1c: Fetch Local
# ─────────────────────────────────────────────

def fetch_local(state: ReviewState) -> ReviewState:
    """Read files from local path."""
    source = state["source"].strip()
    files = []

    if Path(source).is_file():
        content = Path(source).read_text(errors="ignore")
        files = [{"name": Path(source).name, "content": content, "patch": None}]
    elif Path(source).is_dir():
        for path in Path(source).rglob("*"):
            if path.is_file() and path.suffix in SUPPORTED_EXTENSIONS:
                files.append({"name": str(path), "content": path.read_text(errors="ignore"), "patch": None})
    else:
        return {**state, "error": f"Cannot resolve local path: {source}"}

    if not files:
        return {**state, "error": "No supported files found locally."}

    print(f"[fetch_local] Found {len(files)} file(s)")
    return {**state, "files": files, "error": None}

# ─────────────────────────────────────────────
# Routing edges after detect_mode
# ─────────────────────────────────────────────

def route_fetch(state: ReviewState) -> str:
    return state["mode"]  # "pr" | "repo" | "local"

def has_error(state: ReviewState) -> str:
    return "error" if state.get("error") else "continue"

def error_node(state: ReviewState) -> ReviewState:
    state["final_report"] = f"# ❌ Error\n\n{state['error']}"
    return state

# ─────────────────────────────────────────────
# Node 2: Chunk Files
# ─────────────────────────────────────────────

def chunk_files(state: ReviewState) -> ReviewState:
    """
    For PR mode: each file's diff is one chunk (diffs are already focused).
    For repo/local: split large files into 150-line chunks.
    """
    chunks = []
    is_pr = state["mode"] == "pr"

    for file in state["files"]:
        if is_pr:
            # Use the patch (diff) as review content — much more focused
            review_content = f"### Diff\n```diff\n{file['patch']}\n```"
            if file.get("content"):
                # Provide full file as context (truncated)
                full_lines = file["content"].splitlines()[:80]
                review_content += f"\n\n### Full File Context (first 80 lines)\n```\n" + "\n".join(full_lines) + "\n```"
            chunks.append({
                "file": file["name"],
                "chunk": 1,
                "total_chunks": 1,
                "content": review_content,
                "status": file.get("status", "modified"),
                "additions": file.get("additions", 0),
                "deletions": file.get("deletions", 0),
                "is_diff": True,
            })
        else:
            lines = file["content"].splitlines()
            if len(lines) <= CHUNK_SIZE:
                chunks.append({
                    "file": file["name"],
                    "chunk": 1,
                    "total_chunks": 1,
                    "content": file["content"],
                    "is_diff": False,
                })
            else:
                total_chunks = (len(lines) + CHUNK_SIZE - 1) // CHUNK_SIZE
                for i in range(total_chunks):
                    chunk_lines = lines[i * CHUNK_SIZE: (i + 1) * CHUNK_SIZE]
                    chunks.append({
                        "file": file["name"],
                        "chunk": i + 1,
                        "total_chunks": total_chunks,
                        "content": "\n".join(chunk_lines),
                        "is_diff": False,
                    })

    print(f"[chunk_files] Created {len(chunks)} chunk(s)")
    return {**state, "chunks": chunks}

# ─────────────────────────────────────────────
# Node 3: Review Code
# ─────────────────────────────────────────────

REPO_REVIEW_PROMPT = """You are an expert code reviewer. Analyze the given code and return ONLY a JSON object:
{
  "bugs": ["specific bugs or potential runtime errors, with line refs"],
  "security": ["security vulnerabilities or risky patterns"],
  "quality": ["code quality issues: naming, complexity, duplication, etc."],
  "performance": ["performance bottlenecks or inefficiencies"],
  "suggestions": ["improvement suggestions and best practices"],
  "severity": "critical | high | medium | low"
}
Be specific and actionable. If a category has no issues, return [].
Return ONLY valid JSON, no markdown fences, no preamble."""

PR_REVIEW_PROMPT = """You are an expert code reviewer reviewing a Pull Request diff.
Focus ONLY on the changed lines (lines starting with + in the diff).
Return ONLY a JSON object:
{
  "bugs": ["bugs introduced by this change, with line refs from the diff"],
  "security": ["security issues introduced by this change"],
  "quality": ["quality issues in the new code: naming, logic, complexity"],
  "performance": ["performance problems introduced by this change"],
  "suggestions": ["improvements or alternative approaches for the changed code"],
  "pr_summary": "one sentence summary of what this file change does",
  "severity": "critical | high | medium | low",
  "approved": true or false
}
Be concise. Focus on what changed, not pre-existing code.
Return ONLY valid JSON, no markdown fences, no preamble."""


def review_code(state: ReviewState) -> ReviewState:
    reviews = []
    for chunk in state["chunks"]:
        label = f"{chunk['file']} (chunk {chunk['chunk']}/{chunk['total_chunks']})"
        print(f"[review_code] Reviewing {label}")

        system_prompt = PR_REVIEW_PROMPT if chunk.get("is_diff") else REPO_REVIEW_PROMPT

        if chunk.get("is_diff"):
            user_msg = (
                f"File: `{chunk['file']}` | Status: {chunk.get('status','modified')} "
                f"| +{chunk.get('additions',0)} / -{chunk.get('deletions',0)}\n\n"
                f"{chunk['content']}"
            )
        else:
            user_msg = f"File: {chunk['file']} (chunk {chunk['chunk']} of {chunk['total_chunks']})\n\n```\n{chunk['content']}\n```"

        try:
            response = llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_msg)
            ])
            raw = response.content.strip()
            raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
            review_data = json.loads(raw)
        except json.JSONDecodeError:
            review_data = {
                "bugs": [], "security": [], "quality": [], "performance": [],
                "suggestions": [], "severity": "low", "parse_error": response.content
            }
        except Exception as e:
            review_data = {
                "bugs": [], "security": [], "quality": [], "performance": [],
                "suggestions": [], "severity": "low", "error": str(e)
            }

        reviews.append({
            "file": chunk["file"],
            "chunk": chunk["chunk"],
            "total_chunks": chunk["total_chunks"],
            "is_diff": chunk.get("is_diff", False),
            "status": chunk.get("status"),
            "additions": chunk.get("additions", 0),
            "deletions": chunk.get("deletions", 0),
            "review": review_data,
        })

    return {**state, "reviews": reviews}

# ─────────────────────────────────────────────
# Node 4: Generate Report
# ─────────────────────────────────────────────

def generate_report(state: ReviewState) -> ReviewState:
    reviews = state["reviews"]
    is_pr = state["mode"] == "pr"

    # Aggregate by file
    file_map = {}
    for r in reviews:
        fname = r["file"]
        if fname not in file_map:
            file_map[fname] = {
                "bugs": [], "security": [], "quality": [], "performance": [],
                "suggestions": [], "severity": "low",
                "pr_summary": "", "approved": True,
                "status": r.get("status", "modified"),
                "additions": r.get("additions", 0),
                "deletions": r.get("deletions", 0),
            }
        rv = r["review"]
        for key in ["bugs", "security", "quality", "performance", "suggestions"]:
            file_map[fname][key].extend(rv.get(key, []))
        if rv.get("pr_summary"):
            file_map[fname]["pr_summary"] = rv["pr_summary"]
        if rv.get("approved") is False:
            file_map[fname]["approved"] = False
        cur = SEVERITY_ORDER.get(file_map[fname]["severity"], 1)
        new = SEVERITY_ORDER.get(rv.get("severity", "low"), 1)
        if new > cur:
            file_map[fname]["severity"] = rv.get("severity", "low")

    sev_emoji = lambda s: {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(s, "⚪")

    lines = []

    # ── Header ──
    if is_pr:
        meta = state.get("pr_metadata", {})
        lines += [
            "# 🔍 PR Code Review Report\n",
            f"**PR #{meta.get('number')}:** {meta.get('title', '')}",
            f"**Author:** @{meta.get('author', '')} | "
            f"**{meta.get('head_branch', '')}** → **{meta.get('base_branch', '')}**",
            f"**Changes:** +{meta.get('additions',0)} / -{meta.get('deletions',0)} "
            f"across {meta.get('changed_files',0)} file(s)",
        ]
        if meta.get("body"):
            lines.append(f"\n**PR Description:** {meta['body']}")
        lines.append("\n---\n")

        # Overall verdict
        all_approved = all(d["approved"] for d in file_map.values())
        overall_sev = max(file_map.values(), key=lambda d: SEVERITY_ORDER.get(d["severity"], 1))["severity"]
        verdict = "✅ APPROVED" if all_approved else "❌ CHANGES REQUESTED"
        lines += [
            f"## Verdict: {verdict}",
            f"**Highest Severity:** {sev_emoji(overall_sev)} {overall_sev.upper()}\n",
            "---\n",
        ]
    else:
        lines += [
            "# 🔍 Code Review Report\n",
            f"**Source:** `{state['source']}`",
            f"**Files Reviewed:** {len(file_map)}\n",
            "---\n",
        ]

    # ── Summary Table ──
    lines.append("## 📊 Summary\n")
    if is_pr:
        lines.append("| File | Status | Severity | Approved | Bugs | Security | Quality | Perf |")
        lines.append("|------|--------|----------|----------|------|----------|---------|------|")
        for fname, data in file_map.items():
            status_emoji = {"added": "🆕", "modified": "✏️", "renamed": "🔀"}.get(data["status"], "✏️")
            approved_icon = "✅" if data["approved"] else "❌"
            lines.append(
                f"| `{fname.split('/')[-1]}` | {status_emoji} {data['status']} "
                f"| {sev_emoji(data['severity'])} {data['severity'].upper()} "
                f"| {approved_icon} "
                f"| {len(data['bugs'])} | {len(data['security'])} "
                f"| {len(data['quality'])} | {len(data['performance'])} |"
            )
    else:
        lines.append("| File | Severity | Bugs | Security | Quality | Performance |")
        lines.append("|------|----------|------|----------|---------|-------------|")
        for fname, data in file_map.items():
            lines.append(
                f"| `{fname.split('/')[-1]}` | {sev_emoji(data['severity'])} {data['severity'].upper()} "
                f"| {len(data['bugs'])} | {len(data['security'])} "
                f"| {len(data['quality'])} | {len(data['performance'])} |"
            )
    lines.append("")

    # ── Per-File Details ──
    lines += ["---\n", "## 📁 Detailed Findings\n"]
    sections = [("🐛 Bugs", "bugs"), ("🔒 Security", "security"),
                ("🧹 Quality", "quality"), ("⚡ Performance", "performance"),
                ("💡 Suggestions", "suggestions")]

    for fname, data in file_map.items():
        lines.append(f"### `{fname}`")
        if is_pr:
            approved_str = "✅ Approved" if data["approved"] else "❌ Changes Requested"
            lines.append(f"**Status:** {data['status']} | **Verdict:** {approved_str} | "
                         f"**Severity:** {sev_emoji(data['severity'])} {data['severity'].upper()}")
            if data.get("pr_summary"):
                lines.append(f"**Summary:** {data['pr_summary']}")
            lines.append(f"_+{data['additions']} / -{data['deletions']}_\n")
        else:
            lines.append(f"**Severity:** {sev_emoji(data['severity'])} {data['severity'].upper()}\n")

        for title, key in sections:
            items = data[key]
            if items:
                lines.append(f"**{title}:**")
                for item in items:
                    lines.append(f"- {item}")
                lines.append("")

        lines.append("---\n")

    # ── Overall Stats ──
    total = lambda key: sum(len(d[key]) for d in file_map.values())
    lines += [
        "## 📈 Overall Stats\n",
        f"- 🐛 **Bugs:** {total('bugs')}",
        f"- 🔒 **Security Issues:** {total('security')}",
        f"- 🧹 **Quality Issues:** {total('quality')}",
        f"- ⚡ **Performance Issues:** {total('performance')}",
        f"- 💡 **Suggestions:** {total('suggestions')}",
    ]

    final_report = "\n".join(lines)
    return {**state, "final_report": final_report}

# ─────────────────────────────────────────────
# Build the Graph
# ─────────────────────────────────────────────

def build_graph():
    graph = StateGraph(ReviewState)

    graph.add_node("detect_mode", detect_mode)
    graph.add_node("fetch_repo", fetch_repo)
    graph.add_node("fetch_pr", fetch_pr)
    graph.add_node("fetch_local", fetch_local)
    graph.add_node("chunk_files", chunk_files)
    graph.add_node("review_code", review_code)
    graph.add_node("generate_report", generate_report)
    graph.add_node("error_node", error_node)

    graph.set_entry_point("detect_mode")

    # Route based on mode
    graph.add_conditional_edges("detect_mode", route_fetch, {
        "pr": "fetch_pr",
        "repo": "fetch_repo",
        "local": "fetch_local",
    })

    # After each fetch, check for errors
    for fetch_node in ["fetch_pr", "fetch_repo", "fetch_local"]:
        graph.add_conditional_edges(fetch_node, has_error, {
            "error": "error_node",
            "continue": "chunk_files",
        })

    graph.add_edge("chunk_files", "review_code")
    graph.add_edge("review_code", "generate_report")
    graph.add_edge("generate_report", END)
    graph.add_edge("error_node", END)

    return graph.compile()

# ─────────────────────────────────────────────
# Main Runner
# ─────────────────────────────────────────────

def run_review(source: str) -> str:
    app = build_graph()

    initial_state: ReviewState = {
        "source": source,
        "mode": "",
        "pr_metadata": {},
        "files": [],
        "chunks": [],
        "reviews": [],
        "final_report": "",
        "error": None,
    }

    print(f"\n🚀 Starting Code Review Agent for: {source}\n")
    final_state = app.invoke(initial_state)

    report = final_state["final_report"]
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)

    report_path = "review_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n✅ Report saved to: {report_path}")
    return report


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python agent.py <source>")
        print("\nExamples:")
        print("  # Full repo review")
        print("  python agent.py https://github.com/Sujal-781/EasySettle")
        print()
        print("  # PR review")
        print("  python agent.py https://github.com/Sujal-781/EasySettle/pull/3")
        print()
        print("  # Local file or folder")
        print("  python agent.py ./src/main.py")
        sys.exit(1)

    run_review(sys.argv[1])
