#!/bin/bash
# Claude stand-in for tests: edits, commits, and writes outputs without any network.
set -euo pipefail
sleep "${FIXIE_FAKE_SLEEP:-0}"

if [ "${FIXIE_FAKE_COMMIT:-}" != "0" ]; then
  echo "fake run $RUN_MODE" >>FIXIE_FAKE.md
  git add FIXIE_FAKE.md
  git commit -q -m "fake: update FIXIE_FAKE.md"
fi

if [ "${FIXIE_FAKE_REWRITE:-}" = "1" ]; then
  git reset -q --soft HEAD~2
  git commit -q -m "fake: squashed"
fi

if [ "${FIXIE_FAKE_RESULT:-}" != "0" ]; then
  printf '{"status": "done", "summary": "Fake agent appended a line to FIXIE_FAKE.md.", "questions": []}\n' >~/out/result.json
fi

mkdir -p ~/.claude/projects/-home-agent-work
printf '{"type":"fake","sessionId":"%s","mode":"%s"}\n' "$SESSION_ID" "$RUN_MODE" >>~/.claude/projects/-home-agent-work/"$SESSION_ID".jsonl

printf '{"type":"result","subtype":"success","is_error":false,"session_id":"%s","result":"fake"}' "$SESSION_ID"

exit "${FIXIE_FAKE_EXIT:-0}"
