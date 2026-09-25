#!/bin/bash
# Clones/updates the ticket branch, runs the agent, and collects its outputs.
set -euo pipefail

fail() {
  local code=$1
  shift
  echo "run-ticket: $*" >&2
  exit "$code"
}

rm -rf ~/out
mkdir -p ~/out

TICKET_RE='^[0-9]{1,10}$'
BRANCH_RE='^[A-Za-z0-9][A-Za-z0-9._/-]*$'
SESSION_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
MODEL_RE='^[A-Za-z0-9][]A-Za-z0-9._:[-]*$'

[[ "${TICKET_ID:-}" =~ $TICKET_RE ]] || fail 2 "invalid TICKET_ID"
[[ "${BASE_BRANCH:-}" =~ $BRANCH_RE && "${BASE_BRANCH:-}" != *..* && "${BASE_BRANCH:-}" != */ && "${BASE_BRANCH:-}" != *.lock ]] || fail 2 "invalid BASE_BRANCH"
[[ "${SESSION_ID:-}" =~ $SESSION_RE ]] || fail 2 "invalid SESSION_ID"
[[ "${RUN_MODE:-}" == "start" || "${RUN_MODE:-}" == "resume" ]] || fail 2 "invalid RUN_MODE"
if [ "${FIXIE_FAKE_AGENT:-}" != "1" ]; then
  [[ "${CLAUDE_MODEL:-}" =~ $MODEL_RE ]] || fail 2 "invalid CLAUDE_MODEL"
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || fail 2 "invalid CLAUDE_CODE_OAUTH_TOKEN"
fi

[ -f ~/in/ticket.md ] || fail 2 "missing /home/agent/in/ticket.md"
[ -f ~/in/new.json ] || fail 2 "missing /home/agent/in/new.json"

SESSION_FILE=~/.claude/projects/-home-agent-work/$SESSION_ID.jsonl

if [ "$RUN_MODE" = "resume" ] && { [ ! -d ~/work/.git ] || [ ! -f "$SESSION_FILE" ]; }; then
  echo "run-ticket: nothing to resume, starting fresh" >&2
  RUN_MODE=start
fi

if ! BASE_SHA=$(git --git-dir=/mirror/monorepo.git rev-parse --verify -q "refs/heads/$BASE_BRANCH"); then
  fail 3 "git setup failed"
fi

if ! (
  set -euo pipefail
  if [ "$RUN_MODE" = "start" ]; then
    rm -rf ~/work "$SESSION_FILE"
    git clone -q --shared --no-checkout /mirror/monorepo.git ~/work
    if git -C ~/work rev-parse --verify -q "origin/fixie/$TICKET_ID" >/dev/null; then
      git -C ~/work checkout -q -b "fixie/$TICKET_ID" "origin/fixie/$TICKET_ID"
    else
      git -C ~/work checkout -q -b "fixie/$TICKET_ID" "origin/$BASE_BRANCH"
    fi
  else
    git -C ~/work fetch -q origin
    git -C ~/work checkout -q "fixie/$TICKET_ID"
    if git -C ~/work rev-parse --verify -q "origin/fixie/$TICKET_ID" >/dev/null; then
      if ! git -C ~/work merge -q --ff-only "origin/fixie/$TICKET_ID"; then
        echo "run-ticket: branch diverged from origin, left to agent" >&2
      fi
    fi
  fi
); then
  fail 3 "git setup failed"
fi

cd ~/work
PROMPT='Your ticket is in ~/in/ticket.md. Events since your last run are in ~/in/new.json (an empty list on the first run). Open PR threads are in ~/in/pr.md if it exists, and attachments in ~/in/attachments. Do the work following your rules, then write ~/out/result.json.'

if [ "${FIXIE_FAKE_AGENT:-}" = "1" ]; then
  if /opt/fixie/fake-agent.sh </dev/null >~/out/claude.json; then
    agent_rc=0
  else
    agent_rc=$?
  fi
else
  if [ "$RUN_MODE" = "start" ]; then
    session_flag=(--session-id "$SESSION_ID")
  else
    session_flag=(--resume "$SESSION_ID")
  fi
  if claude -p "$PROMPT" "${session_flag[@]}" --model "$CLAUDE_MODEL" --dangerously-skip-permissions --output-format json --append-system-prompt "$(cat /opt/fixie/CLAUDE.md)" </dev/null >~/out/claude.json; then
    agent_rc=0
  else
    agent_rc=$?
  fi
fi

agent_failed=0
if [ "$agent_rc" -ne 0 ]; then
  echo "run-ticket: agent exited with $agent_rc" >&2
  agent_failed=1
fi

rm -f ~/out/branch.bundle ~/out/session.jsonl

if [ -f "$SESSION_FILE" ]; then
  cp "$SESSION_FILE" ~/out/session.jsonl
else
  newest=$(ls -t ~/.claude/projects/-home-agent-work/*.jsonl 2>/dev/null | head -n1) || true
  if [ -n "${newest:-}" ]; then
    cp "$newest" ~/out/session.jsonl
  else
    echo "run-ticket: no session log" >&2
  fi
fi

collect_failed=0
if ! git -C ~/work rev-parse --verify -q "refs/heads/fixie/$TICKET_ID" >/dev/null; then
  echo "run-ticket: branch fixie/$TICKET_ID missing" >&2
  collect_failed=1
elif ! n=$(git -C ~/work rev-list --count "$BASE_SHA..fixie/$TICKET_ID"); then
  echo "run-ticket: bundle range failed" >&2
  collect_failed=1
elif [ "$n" -eq 0 ]; then
  echo "run-ticket: no new commits, no bundle" >&2
else
  if ! git -C ~/work bundle create -q ~/out/branch.bundle "$BASE_SHA..fixie/$TICKET_ID"; then
    echo "run-ticket: bundle create failed" >&2
    collect_failed=1
  fi
fi

if [ "$collect_failed" -eq 1 ]; then
  exit 5
elif [ "$agent_failed" -eq 1 ]; then
  exit 4
fi
exit 0
