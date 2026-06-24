"""
Code Review Agent using LangGraph
Features:
  - Full GitHub repo review
  - GitHub PR diff review
  - Local file / folder review
  - Auto-fix: applies suggested fixes and submits a new PR
  - LangSmith observability: traces every node and LLM call
"""

import os
import re
import json
import base64
import requests
from pathlib import Path
from typing import TypedDict, List, Optional
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tracers.context import tracing_v2_enabled
from langgraph.graph import StateGraph, END

load_dotenv()

# ─────────────────────────────────────────────
# LangSmith Observability Setup
# ─────────────────────────────────────────────
# Set these in your .env to enable tracing:
#   LANGCHAIN_TRACING_V2=true
#   LANGCHAIN_API_KEY=your_langsmith_api_key
#   LANGCHAIN_PROJECT=code-review-agent

TRACING_ENABLED = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"

if TRACING_ENABLED:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT", "code-review-agent")
    print("[observability] LangSmith tracing ENABLED")
    print(f"[observability] Project: {os.environ['LANGCHAIN_PROJECT']}")
else:
    print("[observability] LangSmith tracing DISABLED (set LANGCHAIN_TRACING_V2=true to enable)")

# ─────────────────────────────────────────────
# State Definition
# ─────────────────────────────────────────────

class ReviewState(TypedDict):
    source: str                    # GitHub URL, PR URL, or local path
    mode: str                      # "repo" | "pr" | "local"
    auto_fix: bool                 # whether to apply fixes and submit PR
    pr_metadata: dict              # PR title, author, branch info (PR mode only)
    repo_owner: str                # parsed GitHub owner
    repo_name: str                 # parsed GitHub repo name
    base_branch: str               # base branch of the repo
    files: List[dict]              # fetched files with content
    chunks: List[dict]             # chunked units for LLM review
    reviews: List[dict]            # LLM review results per chunk
    fixed_files: List[dict]        # files with LLM-applied fixes
    fix_pr_url: str                # URL of the submitted fix PR
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

CHUNK_SIZE = 150
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
    source = state["source"].strip()
    owner, repo_name, base_branch = "", "", "main"

    if PR_PATTERN.match(source):
        m = PR_PATTERN.match(source)
        owner, repo_name = m.group(1), m.group(2)
        print(f"[detect_mode] Mode: PR review → {source}")
        return {**state, "mode": "pr", "repo_owner": owner, "repo_name": repo_name}

    elif source.startswith("https://github.com/"):
        m = REPO_PATTERN.match(source)
        if m:
            owner, repo_name = m.group(1), m.group(2)
            base_branch = m.group(3) or "main"
        print(f"[detect_mode] Mode: Full repo review → {source}")
        return {**state, "mode": "repo", "repo_owner": owner, "repo_name": repo_name, "base_branch": base_branch}

    else:
        print(f"[detect_mode] Mode: Local → {source}")
        return {**state, "mode": "local"}

# ─────────────────────────────────────────────
# Node 1a: Fetch Full Repo
# ─────────────────────────────────────────────

def fetch_repo(state: ReviewState) -> ReviewState:
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
    return {**state, "files": files, "base_branch": branch, "error": None}


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
    source = state["source"].strip()
    match = PR_PATTERN.match(source)
    if not match:
        return {**state, "error": "Invalid PR URL format."}

    owner, repo, pr_number = match.groups()
    headers = gh_headers()

    pr_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    pr_resp = requests.get(pr_url, headers=headers)
    if pr_resp.status_code != 200:
        return {**state, "error": f"Could not fetch PR #{pr_number}: {pr_resp.status_code}"}

    pr_data = pr_resp.json()
    base_branch = pr_data.get("base", {}).get("ref", "main")
    pr_metadata = {
        "number": pr_number,
        "title": pr_data.get("title", ""),
        "author": pr_data.get("user", {}).get("login", ""),
        "base_branch": base_branch,
        "head_branch": pr_data.get("head", {}).get("ref", ""),
        "state": pr_data.get("state", ""),
        "additions": pr_data.get("additions", 0),
        "deletions": pr_data.get("deletions", 0),
        "changed_files": pr_data.get("changed_files", 0),
        "body": (pr_data.get("body") or "")[:500],
    }

    print(f"[fetch_pr] PR #{pr_number}: \"{pr_metadata['title']}\" by @{pr_metadata['author']}")

    files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    files_resp = requests.get(files_url, headers=headers)
    if files_resp.status_code != 200:
        return {**state, "error": f"Could not fetch PR files: {files_resp.status_code}"}

    files = []
    for f in files_resp.json():
        filename = f.get("filename", "")
        status = f.get("status", "")
        patch = f.get("patch", "")
        if status == "removed" or Path(filename).suffix not in SUPPORTED_EXTENSIONS or not patch:
            continue
        content = ""
        raw_url = f.get("raw_url", "")
        if raw_url:
            cr = requests.get(raw_url, headers=headers)
            if cr.status_code == 200:
                content = cr.text
        files.append({
            "name": filename, "status": status, "patch": patch,
            "content": content,
            "additions": f.get("additions", 0), "deletions": f.get("deletions", 0),
        })

    if not files:
        return {**state, "error": "No reviewable file changes found in this PR."}

    print(f"[fetch_pr] {len(files)} file(s) with changes")
    return {**state, "files": files, "pr_metadata": pr_metadata,
            "base_branch": base_branch, "error": None}

# ─────────────────────────────────────────────
# Node 1c: Fetch Local
# ─────────────────────────────────────────────

def fetch_local(state: ReviewState) -> ReviewState:
    source = state["source"].strip()
    files = []
    if Path(source).is_file():
        files = [{"name": Path(source).name, "content": Path(source).read_text(errors="ignore"), "patch": None}]
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
# Routing / Error helpers
# ─────────────────────────────────────────────

def route_fetch(state: ReviewState) -> str:
    return state["mode"]

def has_error(state: ReviewState) -> str:
    return "error" if state.get("error") else "continue"

def error_node(state: ReviewState) -> ReviewState:
    state["final_report"] = f"# Error\n\n{state['error']}"
    return state

# ─────────────────────────────────────────────
# Node 2: Chunk Files
# ─────────────────────────────────────────────

def chunk_files(state: ReviewState) -> ReviewState:
    chunks = []
    is_pr = state["mode"] == "pr"
    for file in state["files"]:
        if is_pr:
            review_content = f"### Diff\n```diff\n{file['patch']}\n```"
            if file.get("content"):
                full_lines = file["content"].splitlines()[:80]
                review_content += f"\n\n### Full File Context (first 80 lines)\n```\n" + "\n".join(full_lines) + "\n```"
            chunks.append({
                "file": file["name"], "chunk": 1, "total_chunks": 1,
                "content": review_content, "full_content": file.get("content", ""),
                "status": file.get("status", "modified"),
                "additions": file.get("additions", 0), "deletions": file.get("deletions", 0),
                "is_diff": True,
            })
        else:
            lines = file["content"].splitlines()
            if len(lines) <= CHUNK_SIZE:
                chunks.append({
                    "file": file["name"], "chunk": 1, "total_chunks": 1,
                    "content": file["content"], "full_content": file["content"], "is_diff": False,
                })
            else:
                total_chunks = (len(lines) + CHUNK_SIZE - 1) // CHUNK_SIZE
                for i in range(total_chunks):
                    chunks.append({
                        "file": file["name"], "chunk": i + 1, "total_chunks": total_chunks,
                        "content": "\n".join(lines[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]),
                        "full_content": file["content"], "is_diff": False,
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
Return ONLY valid JSON, no markdown fences, no preamble."""


def review_code(state: ReviewState) -> ReviewState:
    reviews = []
    for chunk in state["chunks"]:
        print(f"[review_code] Reviewing {chunk['file']} (chunk {chunk['chunk']}/{chunk['total_chunks']})")
        system_prompt = PR_REVIEW_PROMPT if chunk.get("is_diff") else REPO_REVIEW_PROMPT
        if chunk.get("is_diff"):
            user_msg = (f"File: `{chunk['file']}` | Status: {chunk.get('status','modified')} "
                        f"| +{chunk.get('additions',0)} / -{chunk.get('deletions',0)}\n\n{chunk['content']}")
        else:
            user_msg = f"File: {chunk['file']} (chunk {chunk['chunk']} of {chunk['total_chunks']})\n\n```\n{chunk['content']}\n```"

        try:
            response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_msg)])
            raw = re.sub(r"^```json\s*|^```\s*|```$", "", response.content.strip(), flags=re.MULTILINE).strip()
            review_data = json.loads(raw)
        except Exception as e:
            review_data = {"bugs": [], "security": [], "quality": [], "performance": [],
                           "suggestions": [], "severity": "low", "error": str(e)}

        reviews.append({
            "file": chunk["file"], "chunk": chunk["chunk"], "total_chunks": chunk["total_chunks"],
            "is_diff": chunk.get("is_diff", False), "status": chunk.get("status"),
            "additions": chunk.get("additions", 0), "deletions": chunk.get("deletions", 0),
            "full_content": chunk.get("full_content", ""),
            "review": review_data,
        })
    return {**state, "reviews": reviews}

# ─────────────────────────────────────────────
# Node 4: Generate Report
# ─────────────────────────────────────────────

def generate_report(state: ReviewState) -> ReviewState:
    reviews = state["reviews"]
    is_pr = state["mode"] == "pr"

    file_map = {}
    for r in reviews:
        fname = r["file"]
        if fname not in file_map:
            file_map[fname] = {
                "bugs": [], "security": [], "quality": [], "performance": [],
                "suggestions": [], "severity": "low", "pr_summary": "", "approved": True,
                "status": r.get("status", "modified"),
                "additions": r.get("additions", 0), "deletions": r.get("deletions", 0),
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

    se = lambda s: {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(s, "⚪")
    lines = []

    if is_pr:
        meta = state.get("pr_metadata", {})
        lines += [
            "# 🔍 PR Code Review Report\n",
            f"**PR #{meta.get('number')}:** {meta.get('title', '')}",
            f"**Author:** @{meta.get('author', '')} | **{meta.get('head_branch', '')}** → **{meta.get('base_branch', '')}**",
            f"**Changes:** +{meta.get('additions',0)} / -{meta.get('deletions',0)} across {meta.get('changed_files',0)} file(s)",
        ]
        if meta.get("body"):
            lines.append(f"\n**PR Description:** {meta['body']}")
        lines.append("\n---\n")
        all_approved = all(d["approved"] for d in file_map.values())
        overall_sev = max(file_map.values(), key=lambda d: SEVERITY_ORDER.get(d["severity"], 1))["severity"]
        verdict = "✅ APPROVED" if all_approved else "❌ CHANGES REQUESTED"
        lines += [f"## Verdict: {verdict}", f"**Highest Severity:** {se(overall_sev)} {overall_sev.upper()}\n", "---\n"]
    else:
        lines += [
            "# 🔍 Code Review Report\n",
            f"**Source:** `{state['source']}`",
            f"**Files Reviewed:** {len(file_map)}\n",
            "---\n",
        ]

    lines.append("## 📊 Summary\n")
    if is_pr:
        lines += ["| File | Status | Severity | Approved | Bugs | Security | Quality | Perf |",
                  "|------|--------|----------|----------|------|----------|---------|------|"]
        for fname, data in file_map.items():
            st_emoji = {"added": "🆕", "modified": "✏️", "renamed": "🔀"}.get(data["status"], "✏️")
            lines.append(f"| `{fname.split('/')[-1]}` | {st_emoji} {data['status']} "
                         f"| {se(data['severity'])} {data['severity'].upper()} "
                         f"| {'✅' if data['approved'] else '❌'} "
                         f"| {len(data['bugs'])} | {len(data['security'])} | {len(data['quality'])} | {len(data['performance'])} |")
    else:
        lines += ["| File | Severity | Bugs | Security | Quality | Performance |",
                  "|------|----------|------|----------|---------|-------------|"]
        for fname, data in file_map.items():
            lines.append(f"| `{fname.split('/')[-1]}` | {se(data['severity'])} {data['severity'].upper()} "
                         f"| {len(data['bugs'])} | {len(data['security'])} | {len(data['quality'])} | {len(data['performance'])} |")
    lines.append("")

    lines += ["---\n", "## 📁 Detailed Findings\n"]
    sections = [("🐛 Bugs","bugs"),("🔒 Security","security"),("🧹 Quality","quality"),
                ("⚡ Performance","performance"),("💡 Suggestions","suggestions")]

    for fname, data in file_map.items():
        lines.append(f"### `{fname}`")
        if is_pr:
            lines.append(f"**Status:** {data['status']} | **Verdict:** {'✅ Approved' if data['approved'] else '❌ Changes Requested'} | **Severity:** {se(data['severity'])} {data['severity'].upper()}")
            if data.get("pr_summary"):
                lines.append(f"**Summary:** {data['pr_summary']}")
            lines.append(f"_+{data['additions']} / -{data['deletions']}_\n")
        else:
            lines.append(f"**Severity:** {se(data['severity'])} {data['severity'].upper()}\n")
        for title, key in sections:
            if data[key]:
                lines.append(f"**{title}:**")
                for item in data[key]:
                    lines.append(f"- {item}")
                lines.append("")
        lines.append("---\n")

    total = lambda key: sum(len(d[key]) for d in file_map.values())
    lines += [
        "## 📈 Overall Stats\n",
        f"- 🐛 **Bugs:** {total('bugs')}",
        f"- 🔒 **Security Issues:** {total('security')}",
        f"- 🧹 **Quality Issues:** {total('quality')}",
        f"- ⚡ **Performance Issues:** {total('performance')}",
        f"- 💡 **Suggestions:** {total('suggestions')}",
    ]

    if state.get("fix_pr_url"):
        lines += ["\n---\n", f"## 🔧 Auto-Fix PR\n",
                  f"Fixes have been applied and submitted: **{state['fix_pr_url']}**"]

    return {**state, "final_report": "\n".join(lines)}

# ─────────────────────────────────────────────
# Node 5: Apply Fixes (NEW)
# ─────────────────────────────────────────────

APPLY_FIX_PROMPT = """You are an expert software engineer. You will be given:
1. A source code file
2. A list of issues found during code review

Your job is to fix ALL the issues and return the complete corrected file.

Rules:
- Return ONLY the fixed source code, no explanations, no markdown fences
- Keep the same language, style, and overall structure
- Fix bugs, security issues, quality problems, and apply suggestions where safe
- Do not add unnecessary code or change unrelated parts
- The output must be valid, runnable code"""


def apply_fixes(state: ReviewState) -> ReviewState:
    """For each reviewed file, ask LLM to apply all suggested fixes."""
    if not state.get("auto_fix"):
        print("[apply_fixes] Skipping (auto_fix=False)")
        return state

    print("[apply_fixes] Applying LLM fixes to reviewed files...")

    # Build a map of file → aggregated issues
    file_issues = {}
    for r in state["reviews"]:
        fname = r["file"]
        if fname not in file_issues:
            file_issues[fname] = {"content": r.get("full_content", ""), "issues": []}
        rv = r["review"]
        for key in ["bugs", "security", "quality", "performance", "suggestions"]:
            file_issues[fname]["issues"].extend(rv.get(key, []))

    fixed_files = []
    for fname, data in file_issues.items():
        content = data["content"]
        issues = data["issues"]

        if not content or not issues:
            print(f"[apply_fixes] Skipping {fname} (no content or no issues)")
            continue

        print(f"[apply_fixes] Fixing {fname} ({len(issues)} issue(s))")
        issues_text = "\n".join(f"- {issue}" for issue in issues)
        user_msg = f"""File: {fname}

Issues to fix:
{issues_text}

Original code:
```
{content[:6000]}
```

Return ONLY the complete fixed code:"""

        try:
            response = llm.invoke([
                SystemMessage(content=APPLY_FIX_PROMPT),
                HumanMessage(content=user_msg)
            ])
            fixed_content = response.content.strip()
            # Strip markdown fences if LLM added them
            fixed_content = re.sub(r"^```[\w]*\n?", "", fixed_content)
            fixed_content = re.sub(r"\n?```$", "", fixed_content).strip()
            fixed_files.append({"name": fname, "content": fixed_content})
            print(f"[apply_fixes] Fixed {fname}")
        except Exception as e:
            print(f"[apply_fixes] Failed to fix {fname}: {e}")

    print(f"[apply_fixes] {len(fixed_files)} file(s) fixed")
    return {**state, "fixed_files": fixed_files}


def should_apply_fixes(state: ReviewState) -> str:
    return "apply" if state.get("auto_fix") else "skip"

# ─────────────────────────────────────────────
# Node 6: Submit Fix PR (NEW)
# ─────────────────────────────────────────────

def submit_fix_pr(state: ReviewState) -> ReviewState:
    """Create a new branch, commit fixed files, and open a PR on GitHub."""
    if not state.get("auto_fix") or not state.get("fixed_files"):
        return state

    owner = state.get("repo_owner", "")
    repo = state.get("repo_name", "")
    base_branch = state.get("base_branch", "main")
    headers = gh_headers()

    if not owner or not repo:
        print("[submit_fix_pr] Cannot submit PR: missing repo owner/name (only works for GitHub sources)")
        return state

    print(f"[submit_fix_pr] Submitting fix PR to {owner}/{repo}")

    # ── Step 1: Get base branch SHA ──
    ref_url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{base_branch}"
    ref_resp = requests.get(ref_url, headers=headers)
    if ref_resp.status_code != 200:
        print(f"[submit_fix_pr] Could not get base branch SHA: {ref_resp.status_code}")
        return state
    base_sha = ref_resp.json()["object"]["sha"]

    # ── Step 2: Create a new branch ──
    import time
    fix_branch = f"auto-fix/code-review-{int(time.time())}"
    branch_resp = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{fix_branch}", "sha": base_sha}
    )
    if branch_resp.status_code not in (200, 201):
        print(f"[submit_fix_pr] Could not create branch: {branch_resp.status_code} — {branch_resp.text}")
        return state
    print(f"[submit_fix_pr] Created branch: {fix_branch}")

    # ── Step 3: Commit each fixed file ──
    commit_count = 0
    for fixed in state["fixed_files"]:
        fname = fixed["name"]
        new_content = fixed["content"]

        # Get current file SHA (needed to update existing files)
        file_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{fname}?ref={fix_branch}"
        file_resp = requests.get(file_url, headers=headers)
        file_sha = file_resp.json().get("sha") if file_resp.status_code == 200 else None

        encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
        payload = {
            "message": f"fix: apply code review suggestions to {fname}",
            "content": encoded,
            "branch": fix_branch,
        }
        if file_sha:
            payload["sha"] = file_sha

        commit_resp = requests.put(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{fname}",
            headers=headers,
            json=payload
        )
        if commit_resp.status_code in (200, 201):
            print(f"[submit_fix_pr] Committed: {fname}")
            commit_count += 1
        else:
            print(f"[submit_fix_pr] Failed to commit {fname}: {commit_resp.status_code}")

    if commit_count == 0:
        print("[submit_fix_pr] No files committed, skipping PR creation")
        return state

    # ── Step 4: Open the PR ──
    pr_body = (
        "## Auto-Fix PR\n\n"
        "This PR was automatically generated by the **Code Review Agent**.\n\n"
        "### Files Fixed\n"
        + "\n".join(f"- `{f['name']}`" for f in state["fixed_files"])
        + "\n\n### What was fixed\n"
        "All issues identified in the code review report have been addressed, including bugs, "
        "security vulnerabilities, code quality improvements, and performance optimizations.\n\n"
        "> ⚠️ Please review these changes before merging."
    )

    pr_resp = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers=headers,
        json={
            "title": "fix: automated code review fixes",
            "body": pr_body,
            "head": fix_branch,
            "base": base_branch,
        }
    )
    if pr_resp.status_code in (200, 201):
        pr_url = pr_resp.json().get("html_url", "")
        print(f"[submit_fix_pr] PR submitted: {pr_url}")
        return {**state, "fix_pr_url": pr_url}
    else:
        print(f"[submit_fix_pr] PR creation failed: {pr_resp.status_code} — {pr_resp.text}")
        return state

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
    graph.add_node("apply_fixes", apply_fixes)
    graph.add_node("submit_fix_pr", submit_fix_pr)
    graph.add_node("error_node", error_node)

    graph.set_entry_point("detect_mode")

    graph.add_conditional_edges("detect_mode", route_fetch, {
        "pr": "fetch_pr", "repo": "fetch_repo", "local": "fetch_local",
    })

    for fetch_node in ["fetch_pr", "fetch_repo", "fetch_local"]:
        graph.add_conditional_edges(fetch_node, has_error, {
            "error": "error_node", "continue": "chunk_files",
        })

    graph.add_edge("chunk_files", "review_code")

    # After review: branch into fix flow or go straight to report
    graph.add_conditional_edges("review_code", should_apply_fixes, {
        "apply": "apply_fixes",
        "skip": "generate_report",
    })

    graph.add_edge("apply_fixes", "submit_fix_pr")
    graph.add_edge("submit_fix_pr", "generate_report")
    graph.add_edge("generate_report", END)
    graph.add_edge("error_node", END)

    return graph.compile()

# ─────────────────────────────────────────────
# Main Runner
# ─────────────────────────────────────────────

def run_review(source: str, auto_fix: bool = False) -> str:
    app = build_graph()

    initial_state: ReviewState = {
        "source": source,
        "mode": "",
        "auto_fix": auto_fix,
        "pr_metadata": {},
        "repo_owner": "",
        "repo_name": "",
        "base_branch": "main",
        "files": [],
        "chunks": [],
        "reviews": [],
        "fixed_files": [],
        "fix_pr_url": "",
        "final_report": "",
        "error": None,
    }

    print(f"\n🚀 Starting Code Review Agent")
    print(f"   Source    : {source}")
    print(f"   Auto-fix  : {'ON' if auto_fix else 'OFF'}")
    print(f"   Tracing   : {'ON (LangSmith)' if TRACING_ENABLED else 'OFF'}\n")

    if TRACING_ENABLED:
        with tracing_v2_enabled(project_name=os.getenv("LANGCHAIN_PROJECT", "code-review-agent")):
            final_state = app.invoke(initial_state)
    else:
        final_state = app.invoke(initial_state)

    report = final_state["final_report"]
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)

    with open("review_report.md", "w", encoding="utf-8") as f:
        f.write(report)
    print("\n✅ Report saved to: review_report.md")

    if final_state.get("fix_pr_url"):
        print(f"🔧 Fix PR submitted: {final_state['fix_pr_url']}")

    return report


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args:
        print("Usage: python agent.py <source> [--fix]")
        print("\nExamples:")
        print("  python agent.py https://github.com/Sujal-781/EasySettle")
        print("  python agent.py https://github.com/Sujal-781/EasySettle --fix")
        print("  python agent.py https://github.com/Sujal-781/EasySettle/pull/3")
        print("  python agent.py https://github.com/Sujal-781/EasySettle/pull/3 --fix")
        sys.exit(1)

    source = args[0]
    auto_fix = "--fix" in args
    run_review(source, auto_fix=auto_fix)
