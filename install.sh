#!/usr/bin/env bash
# Bootstrap: clone (or update) skill-dump and install it.
#   curl -fsSL https://raw.githubusercontent.com/theontho/skill-dump/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --skip copilot-persistent
set -euo pipefail

repo=${SKILL_DUMP_REPO:-https://github.com/theontho/skill-dump.git}
home=${SKILL_DUMP_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/skill-dump}

command -v git >/dev/null 2>&1 || { echo "skill-dump: git is required" >&2; exit 1; }
if [[ -d $home/.git ]]; then
  git -C "$home" pull --ff-only --quiet
else
  mkdir -p "$(dirname "$home")"
  git clone --quiet "$repo" "$home"
fi
exec "$home/bin/skill-dump" install "$@"
