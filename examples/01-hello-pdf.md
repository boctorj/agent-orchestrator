# Example 1: hello-pdf (single unit smoke test)

The simplest possible feature — verifies the full chain end-to-end:
lead → coder → tester → reviewer → PR → merge.

## Paste into the lead chat

> Load a feature: title "hello PDF", description "Create a file called
> hello.pdf at the repo root. Just plain text content saying 'hello from
> the coder agent' is fine — don't need real PDF generation.". repo_path
> is https://github.com/YOU/YOUR-SANDBOX-REPO. branch_prefix is
> feature/F-001-hello.

## Expected flow

1. Lead calls `load_feature` → returns `F-001`
2. Lead drafts a 1-unit plan, posts to chat
3. You: "approve"
4. Lead calls `spawn_unit('F-001', 'F-001-U-1')` — coder opens PR
5. Lead calls `cycle_review('F-001', 'F-001-U-1')` — tester + Copilot + reviewer
6. Lead reports `approved_awaiting_merge` with PR URL
7. You merge on github.com
8. You: "I merged F-001-U-1"
9. Lead calls `reconcile_unit_pr` → flips to `done`

## What it exercises

- ✅ Plan approval flow
- ✅ Single-unit cycle_review
- ✅ Self-approval fallback (REVIEW_RECOMMEND_MERGE)
- ✅ Branch protection requiring your merge
