#!/bin/zsh
cd -- "${0:A:h}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
if curl --silent --max-time 2 http://127.0.0.1:8780/api/health | /usr/bin/grep -q '"ok"'; then
  open http://127.0.0.1:8780
  exit 0
fi
exec bash scripts/start-local.sh
