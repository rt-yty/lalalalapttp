#!/bin/zsh
cd -- "${0:A:h}" || exit 1
umask 077
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m travel_agent.setup_yandex
elif command -v uv >/dev/null 2>&1; then
  uv run python -m travel_agent.setup_yandex
else
  printf 'Не найдено окружение прототипа. Откройте папку проекта в Codex.\n'
fi
printf '\nНажмите Enter, чтобы закрыть окно…'
read -r
