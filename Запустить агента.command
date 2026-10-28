#!/bin/zsh
cd -- "${0:A:h}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
uv run travel-agent chat --model-backend codex --data-mode replay
printf '\nНажмите Enter, чтобы закрыть окно…'
read
