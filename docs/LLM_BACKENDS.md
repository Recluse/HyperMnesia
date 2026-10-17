# LLM backends

HyperMnesia uses an instruct model for five optional operations:

- extracting durable memories from captured sessions;
- deciding whether similar memories should be kept, merged or superseded;
- regenerating project knowledge pages;
- proposing review-gated derived memories in Dreamer;
- producing citation-checked answers in Dialectic (`memory_ask`).

Retrieval itself does not require this model. Document and memory search use the separate
`bge-m3` embedder plus Postgres full-text search. The architecture map is deterministic SQL.

## One adapter, three transports

All model-backed operations use `hooks/_llm.py` and the same model setting.

| Backend | Set | Endpoint contract |
|---|---|---|
| Ollama | `HM_LLM_BACKEND=ollama` | `OLLAMA_URL/api/chat` |
| OpenAI-compatible | `HM_LLM_BACKEND=openai` | `HM_LLM_URL/chat/completions` |
| CLI | `HM_LLM_BACKEND=cli` | prompt appended as the final argument; input text on stdin; response on stdout |

`HM_LLM_BACKEND=auto` chooses OpenAI-compatible when `HM_LLM_URL` is set, CLI when
`HM_LLM_CMD` is set, and Ollama otherwise. Explicit selection is easier to audit in services and
scheduled jobs.

The adapter asks for temperature zero and expects plain response text. Each caller validates the
JSON it needs; malformed or empty output is treated as a failed model call. There is currently one
`HM_LLM_MODEL` for all passes. Per-pass model variables are not implemented.

## Ollama

Ollama is the straightforward local option and can also serve `bge-m3` for retrieval.

```bash
ollama pull qwen2.5:7b

export HM_LLM_BACKEND=ollama
export HM_LLM_MODEL=qwen2.5:7b
export OLLAMA_URL=http://127.0.0.1:11434
python3 hooks/mem_extract.py --dry-run --limit 1
```

The default model name is `qwen2.5:7b`. It is a compatibility baseline, not a claim that it is
the best model for every corpus. Use a model that follows strict JSON instructions and handles the
languages present in your sessions.

Choose Ollama when:

- the deployment is a laptop or Apple Silicon workstation;
- the same local service should handle both embeddings and an instruct model;
- throughput is modest and operational simplicity matters more than continuous batching.

## vLLM

[vLLM](https://vllm.ai/) is a good fit for a shared Linux GPU service. Its
[OpenAI-compatible server](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)
matches HyperMnesia's `openai` backend, so no HyperMnesia code change is required.

Start a server:

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 \
  --port 8000 \
  --api-key "$VLLM_API_KEY" \
  --generation-config vllm
```

Point HyperMnesia at the `/v1` base, not the full chat-completions path:

```bash
export HM_LLM_BACKEND=openai
export HM_LLM_URL=http://127.0.0.1:8000/v1
export HM_LLM_KEY="$VLLM_API_KEY"
export HM_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
python3 hooks/mem_dream.py --dry-run
```

Check the same request shape before scheduling work:

```bash
curl -fsS http://127.0.0.1:8000/v1/chat/completions \
  -H "authorization: Bearer $VLLM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","temperature":0,"messages":[{"role":"user","content":"Return only []"}]}'
```

`--generation-config vllm` keeps a model repository's `generation_config.json` from silently
changing sampling defaults. HyperMnesia still sends temperature zero, but explicit server defaults
make the service easier to reproduce.

Use vLLM when:

- one NVIDIA/AMD/other supported accelerator serves several jobs or users;
- continuous batching and OpenAI API compatibility matter;
- the model should be a Kubernetes service independent of the memory workers;
- you want to change model capacity without changing HyperMnesia clients.

vLLM is not currently an embedding backend in HyperMnesia. Although vLLM exposes an embeddings
API for supported pooling models, HyperMnesia's embedding adapter speaks Ollama `/api/embed` or
TEI `/embed`. Keep Ollama/TEI for `bge-m3`, or add and test an explicit adapter before replacing
it.

On Apple Silicon, upstream vLLM support is experimental and GPU acceleration is provided through
the community-maintained vLLM Metal plugin. Ollama remains the simpler supported choice for this
project's Mac path. On CPU-only servers, vLLM is possible but often adds complexity without a
throughput advantage for small scheduled workloads.

### vLLM security boundary

Do not expose the server directly. The vLLM documentation notes that `--api-key` protects the
documented `/v1`, `/v2` and `/inference` prefixes but not every endpoint. Bind to loopback for a
single host; in Kubernetes use a ClusterIP, NetworkPolicy and, where multiple trust zones exist,
an authenticated proxy. Treat prompts as sensitive even though extraction redacts known secret
shapes before sending transcript text.

## Other OpenAI-compatible providers

Any service implementing `POST /v1/chat/completions` with the normal
`choices[0].message.content` response can use the same adapter:

```bash
export HM_LLM_BACKEND=openai
export HM_LLM_URL=https://provider.example/v1
export HM_LLM_KEY='...'
export HM_LLM_MODEL=provider-model-name
```

This includes self-hosted gateways and hosted APIs. Confirm retention, training and regional
policies before sending transcripts. HyperMnesia removes recognizable structured secrets on the
extraction path, but redaction is best-effort and does not turn a remote model into a local one.

## CLI backend

The CLI transport is useful when an existing subscription-backed or local command already owns
authentication:

```bash
export HM_LLM_BACKEND=cli
export HM_LLM_CMD='model-cli run --no-color'
```

The command must accept the system prompt as its last argument, read the task input from stdin,
write only the final model response to stdout and return non-zero on failure. Avoid commands that
mix progress logs into stdout; strict JSON callers will reject them.

CLI startup overhead is paid once per model call. It is suitable for low-frequency schedules,
not for a high-throughput shared service.

## Sizing the model

The following is operational guidance, not a benchmark result:

| Workload | Practical starting point | What to test |
|---|---|---|
| extract | 7–8B instruct | valid JSON, durable-vs-transient classification, multilingual sessions |
| consolidate | 7–14B instruct | contradictions, negation, no detail loss in merges |
| reflect | 7–14B instruct | stable summaries and bounded output |
| Dreamer | 14B+ when available | premise fidelity, useful proposals, rejection rate |
| Dialectic | strongest affordable model | strict citation IDs, abstention, prompt-injection resistance |

A single 7B model is a reasonable low-cost start. If only one pass gets a larger model, Dreamer
and Dialectic benefit most from stronger reasoning and instruction following. Because the current
adapter has one model variable, running different models per pass requires separate service
environments or a small future routing extension.

Mixture-of-experts models can work when the serving backend supports their architecture. They
reduce active compute per token, but the full expert weights still consume storage and usually
accelerator memory. Choose them from measured latency, JSON validity and memory headroom rather
than parameter count alone.

## Context and output requirements

The extractor can pass up to roughly 60,000 characters of recent transcript. Other passes use
bounded groups or memory contexts. Select a model/server context length that covers the largest
enabled pass and leave room for its JSON answer. A request that truncates the prompt can still
return syntactically valid but semantically unsafe output.

HyperMnesia applies additional gates:

- extraction does not mark a transcript done unless it receives a parseable JSON array;
- consolidation makes no destructive choice on a failed verdict;
- Dreamer proposals cannot become memories without explicit review;
- Dialectic accepts only claims citing active memory IDs in the exact supplied context.

Those checks limit blast radius; they do not replace model evaluation.

## Evaluating a candidate

Before using a model on the full schedule:

1. run extract, consolidate and Dreamer with `--dry-run` against representative local inputs;
2. run the repository tests for memory context, Dreamer and Dialectic;
3. run the checked-in auto-depth evaluation probes;
4. inspect JSON failure rate, abstention rate, latency and review acceptance rate;
5. verify that no job marks work complete after an empty, truncated or refused response.

Keep model name, server version, quantization, context length and generation settings with the
evaluation result. “OpenAI-compatible” describes the wire protocol, not equivalent behavior.

## Kubernetes placement

Run vLLM as its own Deployment and ClusterIP service. Give it a GPU resource limit, a model cache
PVC or preloaded image, a startup probe generous enough for model loading and a readiness check on
`/health`. Point memory worker Jobs at `http://vllm:8000/v1`. Keep TEI as a separate service for
`bge-m3`.

The complete data-flow and scheduling choices are in
[deploy/k8s/README.md](../deploy/k8s/README.md). For larger installations, vLLM also publishes a
[Kubernetes production stack](https://docs.vllm.ai/projects/production-stack/en/latest/deployment/)
with Helm, CRD and gateway options. HyperMnesia only needs a stable OpenAI-compatible endpoint;
it does not require that stack.
