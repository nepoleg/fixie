# Fixie tasks

Tasks are ordered. Tasks 1–16 need no ADO credentials. See `spec.md` for the design.

## Foundation

### 1. Project setup
- Package layout, pytest, and loading of `config.toml` + `.env` into one settings object.
- Move the ADO connection out of `ticket_search.py`, so importing a module never calls Azure.
- **Done when:** `pytest` runs, and settings load with an empty `.env`, except for the required keys.

### 2. Domain model
- Plain data types:
  - ticket
  - comment
  - tag change
  - PR, PR thread
  - build
  - event
  - fixie status
  - run result (`result.json`)
- **Done when:** types exist and are used by the tasks below.

### 3. ADO gateway interface + fake
- One interface with every ADO call fixie makes:
  - find tagged tickets
  - get ticket, updates, comments, attachments
  - get PR, threads, builds, branch head
  - post/edit comment
  - create/update PR, link work item
  - attach file
- A fake implementation that loads tickets from JSON fixtures and records all writes.
- **Done when:** fake passes an interface contract test.

## Core logic (no Docker)

### 4. Status comment
- Format, parse, sign and verify (HMAC with `FIXIE_STATE_SECRET`).
- Trust check: last modified by `FIXIE_OWNER_EMAIL` and valid signature.
- **Done when:** unit tests cover a valid comment, an edited comment, a wrong editor and a missing comment.

### 5. Allowlist check
- Find who added the `fixie` tag from the update history; the latest add wins.
- **Done when:** tests cover allowed, not allowed, and removed-and-re-added tags.

### 6. Event collection
- Collect events since the last finished run, using ADO times only:
  - ticket comments, PR comments
  - PR pushes, failed builds
  - reopen and state changes
- Exclude fixie's own actions: bot sign, recorded head, recorded state, recorded builds.
- **Done when:** fixture tests show changes made during a run are kept and fixie's actions are dropped.

### 7. Ticket classification
- Decide one action per ticket:
  - new
  - skip
  - resume
  - rebuild state
  - retry / give up
  - clean up
  - stop (tag removed)
- **Done when:** there is one fixture test per case in the spec.

### 8. Input rendering
- Build `ticket.md`, `new.json`, `pr.md` and `attachments`.
- Convert HTML to Markdown, rewrite image links, drop bot comments, mark non-allowlisted authors.
- Parse the base branch from the ticket, with `dev` as the default.
- **Done when:** golden-file tests pass for a new ticket and a resumed ticket.

## Containers and git

### 9. Agent image
- Dockerfile: JDK 17, Gradle 8.12 distribution in the agent home folder, git, Claude Code, non-root user.
- `run-ticket.sh`:
  - clone from the mirror or fetch
  - start or resume the session
  - bundle the commits
  - copy the session log to `out`
- `CLAUDE.md` with workflow rules and the `result.json` format.
- **Done when:** the image builds, and `run-ticket.sh` works with a stub `claude`.

### 10. Firewall
- Allowlist script: Anthropic, `pkgs.dev.azure.com`, `europe-maven.pkg.dev`. Deny everything else and self-test at start.
- **Done when:** in the container, allowed hosts connect and `example.com` fails.

### 11. Fake agent
- An image mode or script that edits a file, commits and writes `result.json`, so no Claude usage is needed.
- **Done when:** it can replace `claude` in `run-ticket.sh`.

### 12. Git helper
- Mirror create and refresh with gc disabled.
- Bundle import into the mirror.
- Push with the token as an HTTP header.
- Tested against a local bare repo standing in for ADO.
- **Done when:** a bundle from a ticket volume ends up as a branch in the local "ADO" repo.

### 13. Gradle read-only cache
- Seed and refresh `fixie-gradle-ro` from the host `modules-2`, skipping lock and gc files, with a switch that is never half-done.
- **Done when:** a Gradle build in the container resolves cached jars with no network.

### 14. Docker runner
- Per ticket:
  1. create the container with volumes, limits, labels and env
  2. copy in `in`, then start it
  3. wait with a timeout
  4. read `out`, then remove the container
- **Done when:** it runs the fake agent and returns the parsed result, and a timeout kills the container.

## Orchestration

### 15. Publisher
- Check for new commits, push, create or update the PR, link the work item.
- Post bot-signed comments, and post the questions for "needs info".
- Attach the gzipped session log and finish the status comment.
- Every step is idempotent.
- **Done when:** fake-gateway tests pass, including a re-run after a crash.

### 16. Orchestrator loop + end-to-end test
- Polling, thread pool, in-flight set, mirror lock.
- Restart recovery via container labels.
- Retries, give up, cleanup of closed tickets.
- **Done when:** the end-to-end test passes with fake ADO, the local git remote and the fake agent, for a new ticket, a review comment and a reopen.

## Going live

### 17. Real Claude
- Run with the real Claude (OAuth token) on a small toy repo.
- **Done when:** a toy ticket produces a sensible commit and `result.json`.

### 18. Real ADO gateway, dry-run
- Implement the interface with the Azure DevOps Python lib.
- Dry-run mode: real reads, and writes are only logged. Use a read-only PAT on one test ticket.
- **Done when:** the dry-run log shows the correct classification and input files for the test ticket.

### 19. First live runs
- Enable writes on the test ticket, then run on a real, simple EETS ticket.
- **Done when:** fixie opens a PR, reacts to a review comment, and cleans up after merge.

## Open questions
- Which ticket states fixie acts on, and whether it changes the state itself.
- How a ticket names another base branch: a field, or a line in the description.
- PR title format, draft or ready, default reviewers.
- Values for poll interval, parallel runs, retry count, run timeout.
- Allowlist emails.
- Where the orchestrator runs: laptop or always-on machine.
