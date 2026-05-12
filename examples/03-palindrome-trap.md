# Example 3: palindrome (adversarial — stress-tests reviewer + fix loop)

A spec where the obvious implementation MISSES an edge case the test
should catch. Designed to fire the cycle loop and validate that the
tester finds real bugs and the coder fixes them.

## Paste into the lead chat

> Load a feature: title "palindrome check", description "Add
> `is_palindrome(s: str) -> bool` to math_utils.py. Returns True if `s`
> reads the same forwards and backwards. **Case-insensitive. Ignore
> non-alphanumeric characters.** These examples MUST pass:
> `'A man, a plan, a canal: Panama'` → True; `'race a car'` → False;
> `''` → True; `'a'` → True; `'12321'` → True. Tests should cover those
> five examples plus at least one with mixed casing.". repo_path is
> https://github.com/YOU/YOUR-SANDBOX-REPO. branch_prefix is
> feature/F-002-palindrome.

## The trap

A naive impl is:

```python
def is_palindrome(s: str) -> bool:
    return s.lower() == s.lower()[::-1]
```

This FAILS on `'A man, a plan, a canal: Panama'` — the spaces and
punctuation break reverse-equality. Coder needs to strip non-alphanumerics
first.

## Two possible outcomes

**Outcome A: coder gets it right first try.**
- Tester writes tests for the 5 required cases → all pass
- Reviewer endorses
- No cycle fires
- You learn: coder is reliable on simple specs

**Outcome B: coder writes the naive impl.**
- Tester writes test for `'A man, a plan, a canal: Panama'` → fails
- Tester emits `BUG_FOUND`
- Orchestrator resumes coder with the failure detail
- Coder fixes (strips non-alphanumerics), pushes
- Cycle 1 of 3, tests now pass
- Reviewer endorses
- You learn: the fix loop works end-to-end on a real bug

Either outcome teaches you something about the system's trustworthiness.

## What it exercises

- ✅ Tester catching real bugs via spec-driven test cases
- ✅ `address_review` resuming the coder with structured feedback
- ✅ The cycle counter incrementing
- ✅ PR comments showing the orchestrator-bot conversation
- ✅ Final merge with bug-fix history visible on the PR
