FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 FRONTEND_DIST=/app/frontend/dist PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN useradd --create-home --uid 10001 travel
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY --from=frontend /build/dist frontend/dist/
RUN pip install --no-cache-dir uv && uv sync --locked --no-dev
RUN mkdir -p /app/data && chown -R travel:travel /app
USER travel
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health')"
CMD ["uvicorn", "travel_agent.asgi:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--no-access-log"]
