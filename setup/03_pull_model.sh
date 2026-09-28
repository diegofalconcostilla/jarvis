#!/usr/bin/env bash
# Step 3: install ONE Ollama model, only after showing its size and getting a yes. Usage: setup/03_pull_model.sh qwen3:30b-a3b
set -euo pipefail
MODEL="${1:?usage: $0 <model:tag>   (browse https://ollama.com/library)}"
NAME="${MODEL%%:*}"; TAG="${MODEL#*:}"; [[ "$MODEL" == *:* ]] || TAG=latest
SIZE=$(curl -fsS -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  "https://registry.ollama.ai/v2/library/$NAME/manifests/$TAG" 2>/dev/null \
  | python3 -c "import sys,json; print(round(sum(l['size'] for l in json.load(sys.stdin)['layers'])/1e9,1))" 2>/dev/null) \
  || { echo "Model $MODEL not found in the Ollama library."; exit 1; }
FREE=$(df -g "$HOME" | awk 'NR==2 {print $4}')
echo "Model:  $MODEL"
echo "Size:   ${SIZE} GB download (needs about that much free memory to run; 32 GB Mac: stay under ~20 GB)"
echo "Disk:   ${FREE} GB free"
read -r -p "Install it? [y/N] " a
[[ "$a" == "y" || "$a" == "Y" ]] || { echo "Not installed."; exit 0; }
ollama pull "$MODEL"
echo "Installed. Use it with: export JARVIS_MODEL=$MODEL"
