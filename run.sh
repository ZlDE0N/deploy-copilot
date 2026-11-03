#!/usr/bin/env bash
# Start everything locally: embeddings (Ollama in Docker) + GPU generation (Docker Model Runner) + web UI on :8765
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
if ! docker ps --format '{{.Names}}' | grep -qx copilot-ollama; then
  docker start copilot-ollama >/dev/null 2>&1 || docker run -d --name copilot-ollama --restart unless-stopped \
    -p 127.0.0.1:11434:11434 -v "$PWD/models:/root/.ollama" ollama/ollama:latest >/dev/null
fi
for _ in $(seq 1 20); do curl -fs http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
docker model list 2>/dev/null | grep -q "ai/qwen2.5" || echo "NOTE: generation model missing. Run: docker model pull ai/qwen2.5 (4.4 GB). Ollama is used as a fallback meanwhile."
python3 ingest.py
python3 embed.py
exec python3 server.py
