# Offensive-Security LLMs as Config Auditors

> **Focus:** how public "offensive-security" Hugging Face models behave when
> asked to review a self-hosted web deployment's reverse-proxy and
> host-trust middleware configuration — and why they are brainstorming
> assistants rather than auditors.
>
> **Key idea:** no model outranked a careful manual review. The one real
> finding (the 3B's spoofed-`Host: localhost` trust flip) restated an
> accepted-risk comment already in `nginx.conf`; it was fixed and
> regression-tested the same day (2026-10-05). Every model claim must be
> verified against the config itself.

---

## 1. Setup

Three open-weight "offensive-security" fine-tunes, served locally via
llama.cpp server containers (OpenAI-compatible `/v1/chat/completions`),
bound to loopback only (never exposed on any public surface):

| Container | Model (HF) | Quant | Role |
|---|---|---|---|
| `pentest-llm` :8080 | [`fawazo/qwen2.5-coder-3b-pentest-gguf`](https://huggingface.co/fawazo/qwen2.5-coder-3b-pentest-gguf) | Q4_K_M | the 3B below |
| `pentest-llm-redteam` :8083 | fine-tune [`LLM-PBE/Llama3.1-8b-instruct-LLMPC-Red-Team`](https://huggingface.co/LLM-PBE/Llama3.1-8b-instruct-LLMPC-Red-Team), quant [`mradermacher/Llama3.1-8b-instruct-LLMPC-Red-Team-i1-GGUF`](https://huggingface.co/mradermacher/Llama3.1-8b-instruct-LLMPC-Red-Team-i1-GGUF) | i1_Q4_K_M | the 8B (red-team) below |
| `pentest-llm-kali` :8082 | fine-tune [`suryanshp1/Llama-3.1-8B-kali-pentester`](https://huggingface.co/suryanshp1/Llama-3.1-8B-kali-pentester), quant [`mradermacher/Llama-3.1-8B-kali-pentester-GGUF`](https://huggingface.co/mradermacher/Llama-3.1-8B-kali-pentester-GGUF) | Q4_K_M | excluded — see §3 |

The review prompt embeds the target reverse-proxy + middleware configs with an
explicit "you are reviewing the owner's own deployment" framing (authorized
scope). Runner: `deploy/probes/run_pentest_review.py`.

## 2. Empirical results (2026-10-05)

Same prompt, same (already-hardened) config, `max_tokens` 1200, `temperature`
0.3. **Single run per model — anecdote, not benchmark:**

| Model | Behavior | Real findings | Errors |
|---|---|---|---|
| 3B pentest/code fine-tune | Structured analysis | One real finding: a spoofed `Host: localhost` passed the then-shared allowlist and flipped the middleware's host-trust branch to "trusted" (defense-in-depth held: nginx `/admin` 404 + `X-Pipeline-Key`). Caveat: the finding restates the accepted-risk comment in `nginx.conf` itself — rediscovered, not discovered | False alarm: claimed the trust marker could be forged (it is stripped at the public edge — not forgeable; the prompt's explicit "ways to forge the marker" focus likely induced it) |
| 8B red-team fine-tune | Structured list, **defensive posture** (mitigation/monitoring advice) | None | Called the host allowlist "allows any host" (it rejects unknown hosts — backwards, contradicting an in-context comment); called the trust-marker strip a weakness (it is the protection); four generic non-findings (DNS, buffering, CSP, rate-limit headroom) |

**Resolution (same day):** the spoofing vector is closed — the public edge now
serves only the funnel hostname (`$host_allowed_public`); loopback/tailnet
entries live only on the tailnet listener (`$host_allowed_tailnet`). Enforced
by real tests, not model opinions:

- `backend/tests/unit/test_nginx_trust_invariants.py` — config invariants
  (public allowlist = funnel hostname only, marker stripped on :80 / set on
  :8081, 444 guards, `TRUSTED_LOCAL_HOSTS` sync).
- `backend/tests/unit/test_public_surface_gates.py` — the middleware gates
  (pre-existing).
- `deploy/probes/public_edge_probes.sh` + `ws_flood_test.py` — live-edge
  probes (spoofed Host, forged marker, WS handshake flood).

## 3. Why the results look like this

- **Fine-tune quality + chat-template match dominate base-model size.** The
  8B model was a repack of an amateur fine-tune; the weak fine-tune outweighs
  the stronger base. The third candidate (kali-pentester 8B) never produced a
  review at all: a chat-template mismatch degenerated its run into a
  `<|im_start|>`-leaking repetition loop, and it was excluded.
- **"Offensive security" tags ≠ guaranteed offensive behavior.** The red-team
  model defaulted to defensive framing ("regularly review and update", "add
  monitoring") when given a real review task that explicitly asked for
  exploit paths.
- The 3B model is *not* better because of size — it simply produced one
  verifiable finding and one testable false alarm, the most useful
  signal-to-noise of the two. (Both were already answerable from the config
  file's own comments, which were passed to the models verbatim.)

## 4. Guidance

- Use these models for **attack-path brainstorming** and first-pass triage
  only. Cross-check every claim against the actual config (the 3B's trust-
  marker alarm and the red-team's allowlist claim were both wrong, in
  opposite directions).
- The reviewed config was already hardened when the bake-off ran; subtle
  remaining items live outside these files, so no model could be expected to
  find them.
- The trust model is now enforced by **real tests** (§2 Resolution) — those,
  plus a manual review, are the authoritative passes. The models have no
  vote.

---

## Relationship to other docs

- `.devin/global_rules.md` — one-paragraph summary + pointer to this doc.
- `docs/Schematics/08_Agent_MCP_LLM/04_Docker_Agent_Stack.md` — the platform
  LLM stack; the review containers above are a separate, loopback-only tool.
