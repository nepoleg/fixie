#!/bin/sh
# Fixie helper jobs: mirror refresh, bundle import and push, Gradle read-only cache refresh.
set -eu

M=/mirror/monorepo.git

fail() { echo "fixie-helper: $2" >&2; exit "$1"; }

mirror() {
  [ -n "${FIXIE_REMOTE_URL:-}" ] || fail 2 "missing FIXIE_REMOTE_URL"
  if [ ! -d "$M" ]; then
    rm -rf "$M.tmp"
    git clone -q --mirror "$FIXIE_REMOTE_URL" "$M.tmp" || fail 4 "clone failed"
    mv "$M.tmp" "$M"
    echo "fixie-helper: mirror created" >&2
  fi
  git -C "$M" config gc.auto 0
  git -C "$M" remote set-url origin "$FIXIE_REMOTE_URL"
  git -C "$M" fetch -q --prune origin || fail 4 "fetch failed"
  echo "fixie-helper: mirror refreshed" >&2
}

import_bundle() {
  id=$1
  case "$id" in ''|*[!0-9]*) fail 2 "invalid ticket id" ;; esac
  [ "${#id}" -le 10 ] || fail 2 "invalid ticket id"
  [ -d "$M" ] || fail 2 "mirror missing"
  b=/ticket/out/branch.bundle
  if [ ! -e "$b" ] && [ ! -L "$b" ]; then
    echo "fixie-helper: no bundle"
    return 0
  fi
  { [ -f "$b" ] && [ "$(readlink -f "$b")" = "$b" ]; } || fail 3 "bundle is not a regular file"
  git -C "$M" bundle verify -q "$b" >/dev/null 2>&1 || fail 3 "bundle verify failed"
  heads=$(git -C "$M" bundle list-heads "$b") || fail 3 "bundle verify failed"
  ref="refs/heads/fixie/$id"
  [ "$(printf '%s\n' "$heads" | awk '{print $2}')" = "$ref" ] || fail 3 "bundle refs rejected"
  git -C "$M" -c transfer.fsckObjects=true fetch -q "$b" "+$ref:$ref" || fail 3 "bundle import failed"
  sha=$(git -C "$M" rev-parse --verify "$ref^{commit}")
  git -C "$M" -c remote.origin.mirror=false push -q origin "$ref:$ref" || fail 4 "push failed"
  echo "fixie-helper: pushed $sha"
}

gradle_cache() {
  [ -d /src ] || fail 2 "missing /src"
  [ -d /src/files-2.1 ] || fail 5 "empty modules-2 source"
  cd /gradle-ro
  exec 9>.lock
  flock -n 9 || fail 5 "gradle refresh busy"
  rm -f .modules-2.tmp
  old=$(readlink modules-2 2>/dev/null | cut -d/ -f1)
  new=$(mktemp -d v-XXXXXX)
  chmod 755 "$new"
  set --
  if [ -n "$old" ] && [ -d "$old/modules-2" ]; then set -- "--link-dest=/gradle-ro/$old/modules-2"; fi
  rsync -a --chmod=D755,F644 --exclude='*.lock' --exclude=gc.properties "$@" /src/ "$new/modules-2/" \
    || { rm -rf "$new"; fail 5 "gradle copy failed"; }
  ln -s "$new/modules-2" .modules-2.tmp
  mv -T .modules-2.tmp modules-2 || { rm -rf .modules-2.tmp "$new"; fail 5 "gradle switch failed"; }
  # ponytail: keeps one previous version; a build spanning two refreshes can lose files. Refresh while idle if that bites.
  for d in v-*; do [ "$d" = "$new" ] || [ "$d" = "$old" ] || rm -rf "$d"; done
  echo "fixie-helper: gradle cache $new"
}

case "${1:-}" in
  mirror) mirror ;;
  import) [ "$#" -eq 2 ] || fail 2 "invalid ticket id"; import_bundle "$2" ;;
  gradle) gradle_cache ;;
  *) fail 2 "unknown command" ;;
esac
