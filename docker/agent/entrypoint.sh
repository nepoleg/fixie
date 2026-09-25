#!/bin/bash
# Runs the firewall as root, then drops to the unprivileged agent user.
set -euo pipefail

if [ "${FIXIE_FIREWALL:-}" = "off" ]; then
  echo "fixie-firewall: disabled (FIXIE_FIREWALL=off)" >&2
else
  if ! /opt/fixie/init-firewall.sh; then
    echo "fixie-firewall: setup failed" >&2
    exit 1
  fi
fi

export HOME=/home/agent USER=agent LOGNAME=agent
exec setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all "$@"
