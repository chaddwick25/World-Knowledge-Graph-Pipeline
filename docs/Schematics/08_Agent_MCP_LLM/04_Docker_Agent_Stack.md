# Docker Agent Stack: Ollama + Goose on the RTX 4070 Ti Super

> **Focus:** how the local agent runs entirely in Docker. Ollama serves the
> model, Goose is the MCP-native agent, with the GPU dedicated to the
> application side.
>
> **Key idea:** both pieces are container-native (zero `requirements.txt`
> impact, like the redis/postgres services) and GPU-pinned: the 16 GB card
> serves the app (ollama, always resident), the 8 GB card runs the pipeline
> worker. Goose is on-demand (`restart: "no"`, its default command exits
> immediately) and talks to Ollama + the Vite MCP server over host
> networking.

---

## 1. Hardware

Two GPUs (`nvidia-smi`):

```
0  NVIDIA GeForce RTX 2070        8 GB   → idle/spare (no container pins to it)
1  NVIDIA GeForce RTX 4070 Ti SUPER 16 GB → ollama LLM serving + pipeline worker
```

In practice both ollama and the worker set `NVIDIA_VISIBLE_DEVICES=1` (the
2070 pinning documented in AGENTS.md was never applied; the 2026-09-11 OOM
from that sharing is resolved by the 8b switch — see
`05_LLM_Capacity_Envelope.md` §4).

Inside a container the selected device always appears as `cuda:0`. The
worker's GPU scheduling (`gv_nle` section of `pipeline/hyperparams.yaml`) reflects the remap. Caveat: IE-scale
spectral runs need the 16 GB; those runs must temporarily unpin or run when
the app is idle.

---

## 2. Ollama Service

```yaml
ollama:
  image: ollama/ollama
  runtime: nvidia
  environment:
    - NVIDIA_VISIBLE_DEVICES=1          # 4070 Ti Super — app side, exclusive
    - OLLAMA_KEEP_ALIVE=-1              # model always resident (no cold reloads)
    - OLLAMA_NUM_PARALLEL=6             # qwen3:8b resident 8.7 GB; load-tested ceiling 2026-09-16 (see 05_LLM_Capacity_Envelope.md)
    - OLLAMA_MAX_LOADED_MODELS=1
    - OLLAMA_MAX_QUEUE=32               # fail fast instead of queueing 512
  ports: ["11434:11434"]
  volumes: [ollama_models:/root/.ollama]
```

Model: `docker compose exec ollama ollama pull qwen3:8b` (~5.2 GB, one
time; qwen3:14b was deleted 2026-09-16). The backend reaches it as
`http://ollama:11434/v1` (`LLM_BASE_URL`).

---

## 3. Goose Service

```yaml
goose:
  image: ghcr.io/block/goose:latest
  network_mode: host                    # localhost reaches Vite (:5173) + Ollama (:11434)
  environment:
    - GOOSE_PROVIDER=ollama
    - GOOSE_MODEL=qwen3:8b
    - OLLAMA_HOST=http://localhost:11434
    - GOOSE_TELEMETRY_OPT_IN=false      # skip the interactive prompt
  volumes:
    - ./goose/config:/home/goose/.config/goose   # DIRECTORY mount, not a file
    - ./goose:/workspace/goose
  stdin_open: true
  tty: true
  restart: "no"                         # on-demand, not a daemon
```

**Config-directory mount gotcha:** goose rewrites `config.yaml` on session
start (telemetry choice, paths), and atomic renames fail against a
single-file bind mount: "Device or resource busy (os error 16)". Mount
`./goose/config/` as a directory (`goose/config/config.yaml` holds the
`worldkg-mcp` `streamable_http` extension pointing at
`http://localhost:5173/__mcp`). Goose normalizes the config on first run
(appends its bundled platform extensions) and writes a `.bak`, safe to
delete.

**On-demand, not a daemon:** the image's default command (`goose --help`)
exits immediately. With `restart: unless-stopped` the service crash-loops.
Use `docker compose run --rm goose session` (interactive TUI) or
`docker compose run --rm goose run -t "…"` (headless).

---

## 4. Runbook

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3:8b
docker compose run --rm goose session            # interactive agent
docker compose run --rm goose run -t "Which cafes are within 50km of Belize City?"
```

Runtime requirements: backend :8000, Vite dev server :5173 (browser open
for overlay tools; data tools work headless), ollama :11434.

---

## 5. Agent Config & Recipe

- `goose/config/config.yaml`: provider via env; `worldkg-mcp` extension
  (`type: streamable_http`, `uri: http://localhost:5173/__mcp`,
  `enabled: true`)
- `goose/worldkg-recipe.yaml`: agent instructions, tool semantics, template
  vocabulary (FILTER-AGGREGATE-MEASURE → radius overlay, etc.), and the
  orchestration pattern (data tool → renderToolOverlay → synthesize)

---

## 6. Model Rationale

`qwen3:8b` (Q4, 8.7 GB resident at 6 slots) is the platform model as of
2026-09-16: an A/B replay of the exact production prompts showed equivalent
quality on all three task shapes (synthesis, tool selection, parser refine)
at ~1.6x the generation speed (96–103 vs 61–63 tok/s), and the smaller
resident footprint resolves the worker OOM on the shared 4070. `qwen3:14b`
was deleted. See `05_LLM_Capacity_Envelope.md` for the measured envelope
and `docs/plans/LLM_OLLAMA_4070_CAPACITY_ENVELOPE.md` for the A/B evidence.
Swap to a cloud provider by changing `LLM_BASE_URL` + `LLM_API_STYLE=openai`
the tool surface and `LLMService` don't care where the model runs.
