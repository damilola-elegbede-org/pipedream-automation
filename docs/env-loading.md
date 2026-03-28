# `.env.local` Loading — Canonical Path, Override Behavior, and Gitignore Rules

## Overview

The deploy script loads Pipedream credentials from `.env.local` at startup via
`load_and_set_env_local()` in `src/deploy/utils.py`.  Two paths are consulted
in a well-defined priority order so that the script works correctly whether run
from a fresh clone, a developer workstation with repo-local overrides, or a
headless CI runner.

---

## Resolution Order

| Priority | Path | Purpose |
|----------|------|---------|
| Lowest | `~/.openclaw-dara/credentials/pipedream/.env.local` | **Canonical** — all Pipedream secrets live here |
| Higher | `./env.local` *(repo root)* | **Override** — partial or dev-specific values |
| Highest | Existing `os.environ` values | **Runtime** — shell exports, CI secrets, etc. |

**The canonical file is loaded first** so all variables are present as a
baseline.  The repo-root `.env.local` is then layered on top: any key it
defines replaces the canonical value.  Values already present in the process
environment are never overwritten.

When an **explicit path** is passed to `load_and_set_env_local(env_path)`, that
file is the only source and the auto-resolution logic is skipped entirely.

---

## What Lives Where

### Canonical file — `~/.openclaw-dara/credentials/pipedream/.env.local`

Contains **all** Pipedream secrets:

```dotenv
PIPEDREAM_USERNAME=damilolaelegbede
PIPEDREAM_PROJECT_ID=proj_qzsZPn

PIPEDREAM_WORKFLOW_GMAIL_TO_NOTION=gmail-to-notion-p_6lCxdAp
PIPEDREAM_WORKFLOW_NOTION_TASK_TO_GCAL=create-google-event-from-notion-p_jmC1Q3L
PIPEDREAM_WORKFLOW_NOTION_UPDATE_TO_GCAL=update-google-event-from-notion-p_OKCkxKW
PIPEDREAM_WORKFLOW_GCAL_TO_NOTION=update-notion-task-from-google-calendar-p_WxC9rnG
PIPEDREAM_WORKFLOW_HORIZON_SCORES=update-notion-horizon-scores-p_D1Cr5eZ

PIPEDREAM_COOKIES=<base64-encoded-json>   # cached session cookies

NOTION_TOKEN=...
ANTHROPIC_API_KEY=...
```

This file is **not** in any repository.  It is managed manually or by the
OpenClaw credentials system.

### Repo-root file — `./env.local`

**Optional.** Checked into `.gitignore` so it is never committed.  Use it to:

- Override a single workflow ID during development without touching the canonical file.
- Store a freshly-captured `PIPEDREAM_COOKIES` value after a browser session (the
  deploy script writes new cookies here via `save_cookies_to_env_local()`).

A minimal repo-root `.env.local` might contain only:

```dotenv
PIPEDREAM_COOKIES=<latest-base64-session>
```

---

## Gitignore Rules

`.env.local` is already listed in `.gitignore` under the "Environment files (may
contain secrets)" section:

```gitignore
.env
.env.local
*.env
```

**Never commit `.env.local`.**  The canonical credentials file at
`~/.openclaw-dara/credentials/pipedream/.env.local` must never be committed to
any repository.

---

## Cookie Refresh Flow

1. User or agent runs the deploy script with expired or missing cookies.
2. `_inject_cached_cookies()` loads `PIPEDREAM_COOKIES` from the environment
   (which was set by `load_and_set_env_local`), injects them into the Playwright
   persistent context, then navigates to `pipedream.com` to verify the session.
3. If cookies are valid: the dashboard loads and deploy proceeds without manual
   login.
4. If cookies are invalid or expired: the script opens a headed browser and
   displays the interactive login prompt.  After a successful Google SSO login,
   `teardown_browser()` captures the new session cookies and writes them to the
   repo-root `.env.local` via `save_cookies_to_env_local()`.
5. On the next run the fresh cookies are in `.env.local` (higher priority than
   canonical), so the deploy proceeds headlessly again.

---

## Dry-Run Behavior

`--dry-run` enumerates all configured workflows and their steps **without**
launching a browser, injecting cookies, or making any network requests.  It
exits 0 when config and all referenced step scripts are valid.

```bash
python -m src.deploy.deploy_to_pipedream --dry-run --verbose
```

This is the fastest way to verify the env is correctly loaded and all workflow
mappings resolve.
