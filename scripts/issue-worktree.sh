#!/usr/bin/env bash
# Crea (o riusa) il worktree dedicato alla issue N: ../Jarvis-hermes-N,
# branch esattamente feat/issue-N. Idempotente: se esiste già, lo riusa.
# Uso: ./scripts/issue-worktree.sh N   (da qualsiasi directory del repo)
set -euo pipefail

N="${1:?Uso: issue-worktree.sh N}"
ROOT="$(git rev-parse --show-toplevel)"
DIR="$(dirname "$ROOT")/Jarvis-hermes-$N"
BR="feat/issue-$N"

git fetch origin

if git worktree list --porcelain | grep -q "^worktree $DIR$"; then
  echo "$DIR"
  exit 0
fi

if git show-ref --verify --quiet "refs/heads/$BR"; then
  git worktree add "$DIR" "$BR"
elif git show-ref --verify --quiet "refs/remotes/origin/$BR"; then
  git worktree add "$DIR" -b "$BR" "origin/$BR"
else
  git worktree add "$DIR" -b "$BR" origin/main
fi

echo "$DIR"
