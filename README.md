# fixie

Agentic junior developer: a safe AI agent that takes tasks from your board, works on them in a secure environment and keeps you in the loop.

## Features

- **Secure**: the AI can't write to anything outside the sandbox. Its only output is commits in the ticket's working copy, and every change goes through PR review
  - Runs in a Docker sandbox: non-root, no host mounts, resource limits, timeout, firewall allowlist
  - Has read-only ADO access, and no write access anywhere
  - All writes (push, PR, comments, ticket state) are done by fixed Python code, not by the AI, so a harmful instruction in a ticket can't change anything
- **Scalable**: Python code, not the AI, scans ADO and decides what needs work, so idle tickets cost no AI tokens
  - Claude only starts when a ticket has new activity, and it gets small pointers instead of the full history
  - Parallel runs with a thread pool, never two runs on one ticket
  - Shared Gradle read-only cache and repo mirror for fast builds
- Human-in-the-loop: every change goes through PR review
- No database: the ticket holds fixie's state, so restarts lose nothing
- One Claude session per ticket that lasts as long as the PR
- Crash recovery with retries and a give-up limit
- Idempotent publishing steps
- Base branch override with a `base: <branch>` line in the ticket
- Dry-run mode: real reads, writes only logged

## Functions

- Poll ADO for open tickets tagged `fixie`
- Check who added the tag against the allowlist in `config.toml` (the owner is always allowed)
- Read and verify the signed status comment, or rebuild state from ADO history
- Collect new events since the last run: comments, PR threads, reviewer pushes, failed builds, reopens
- Classify each ticket: new, resume, skip, rebuild, retry / give up, clean up, stop
- Move tickets to Developing when a run starts
- Write `ticket.json` and run Claude Code in a sandboxed Docker container
- Clone or fetch from a shared repo mirror and work on branch `fixie/<id>`
- Push the agent's commits, create or update the PR and link it to the ticket
- Post bot-signed summaries or questions, reply to PR threads and resolve the threads the agent handled
- Attach run logs to the ticket (tokens redacted)
- Delete the ticket volume and `fixie/<id>` branch after the PR is merged or abandoned
