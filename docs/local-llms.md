# Local models with Ollama

Case Calendar can run its LLM work — the per-entry hearing/deadline extractor,
the case-summary writer, or both — on local open models through
[Ollama](https://ollama.com) instead of a hosted API. Ollama serves an
OpenAI-compatible endpoint, so it plugs in as a fourth provider alongside
Anthropic, OpenAI, and Gemini.

Why you might want it:

- **No API key and no per-token cost.** Everything runs on your machine; the
  cost estimate on every run logs `$0.0000`.
- **Data stays local.** Docket text and PDF contents never leave the host.
- **A benchmarking surface.** If you're tuning local models, you can score one
  head-to-head against the hosted providers with the same pipeline and the same
  dockets — see [Benchmarking](#benchmarking-a-local-model).

The trade-offs are real and worth reading before you rely on it — see
[Choosing a model](#choosing-a-model) (including the honesty note for
adversary-nation cases) and [Caveats](#caveats).

> Model names and hardware move fast. The families, sizes, and VRAM figures
> below were checked in June 2026; treat specific version numbers as examples
> and confirm current tags in the [Ollama library](https://ollama.com/library).

## The two tracks pull in different directions

Everything on this page comes back to one distinction, because the extractor
and the summarizer ask very different things of a model:

- **Extraction** — reads one docket entry at a time and emits structured JSON
  (date, key, significance). Short context, high volume, no opinions about the
  *subject* of the case. The limiting factor is **JSON reliability**, which a
  solid small/mid model already handles. This is the track whose per-token cost
  adds up on a hosted API, so it's the one most worth moving to your GPU.
- **Summaries** — low volume (one call per docket) but **long context** (tens of
  thousands of tokens of indictment / judgment text) and synthesis-heavy.
  Benefits from a more capable model and a large context window, and — for
  adversary-nation cases — a model you trust to describe the charges faithfully.

The default local model — `gemma4:e4b` — runs **both** tracks. It's a 9.6 GB
download (4.5B effective parameters, multimodal), so it wants a **16 GB** card
to run comfortably at the recommended 128K window; a 12 GB card works at a
reduced window, and an 8 GB card is too small — there, drop to the 7.2 GB
`gemma4:e2b` at a modest window or run summaries hosted. On a card that fits it,
the simplest setup is everything local. Summaries are the more demanding track,
and there are two ways to improve their quality. The larger `gemma4:31b` is
better, but its 20 GB of weights leave no room for a useful context window even
on a 24 GB card — it runs out of memory (or spills to system RAM and crawls) at
the windows summaries need — so it wants a **32 GB GPU**, which on the current
consumer line means an RTX 5090 (the 5080 is only 16 GB) or the older route of
two cards. On anything smaller the quality path is instead the **hybrid** setup:
keep extraction local and send only summaries to a hosted (or Western-model)
provider. Hybrid is also the answer for adversary-nation cases where you want a
model you specifically trust. The two tracks are configured
independently — see [Configure](#configure).

## Choosing a model

### Recommended models

All of these are text-capable open models on Ollama. Case Calendar only ever
sends **text** (docket text + text extracted from PDFs — OCR happens before the
LLM), so you never need a vision/multimodal variant.

| Model (family) | By | License | Best track | Notes |
| --- | --- | --- | --- | --- |
| **Gemma 4** (`e2b` / `e4b` / `26b` / `31b`) | Google | Apache 2.0 | both | Strong structured output + tool calling; sizes from phone-class to 31B. A good default that runs the whole pipeline. |
| **Mistral Small** (24B) | Mistral AI (France) | Apache 2.0 | summaries | Efficient; fits a 16 GB card at Q4. |
| **Llama 4 Scout** / Llama 3.x | Meta | Llama Community License | both | Long-context; widely supported. (License restricts only at >700M monthly users — irrelevant here.) |
| **gpt-oss** (20B) | OpenAI | open weights | both | Fits 16 GB; adjustable reasoning effort. |
| **Phi-4** (14B) | Microsoft | MIT | extraction | Small, strong instruction-following. |

For a single recommendation: **Gemma 4** is the cleanest all-rounder — Western
(Google), permissively licensed, good at the structured output extraction
needs, and capable enough for summaries — and it comes in several sizes
(`e2b` 7.2 GB / `e4b` 9.6 GB / `26b` 18 GB / `31b` 20 GB) so one fits your card.
It's the built-in Ollama default: selecting `LLM_PROVIDER=ollama` with no model
override runs **`gemma4:e4b` for both tracks** — the 9.6 GB default, which runs
comfortably on a mainstream 16 GB card (12 GB at a reduced window; an 8 GB card
needs the smaller `gemma4:e2b` or hosted summaries). On a **32 GB-or-larger**
card, trade UP to the higher-quality `gemma4:31b` with `LLM_MODEL=gemma4:31b`
(or `LLM_SUMMARY_MODEL=gemma4:31b` to upgrade only summaries, where the bigger
model helps most). It does **not** fit a 24 GB card at the context window
summaries need — its 20 GB of weights leave no room for the KV cache (see
[Context window](#context-window)) — so on 24 GB or less, keep `gemma4:e4b`
local and send summaries to a hosted provider if you want higher quality (the
hybrid setup). The two tracks are configured separately — see
[Configure](#configure).

China-developed models (**Qwen3 / Qwen 3.6** from Alibaba, **DeepSeek-R1**,
**Kimi**) are technically excellent — Qwen in particular is top-tier at
structured output — but carry a provenance caveat for this project's caseload.
Read the next section before using one on the summary track.

### Model provenance and honesty

This matters specifically because Case Calendar is often used to track
cybercrime and espionage prosecutions — including cases that name foreign
states as the adversary. **A model's training can bias how faithfully it
describes a case that is politically sensitive to the country that built it.**

Independent evaluations have documented that China-developed open models are
aligned to suppress or slant content sensitive to the Chinese government — for
example, [Promptfoo's analysis](https://www.promptfoo.dev/blog/deepseek-censorship/)
found DeepSeek-R1 refused a large share of China-controversy prompts and
returned a strong pro-government slant when it did answer, and similar audits
report Qwen models producing falsehoods on topics like Tiananmen while clearly
possessing the underlying knowledge. The open weights are *less* filtered than
the vendors' hosted chatbots, but the alignment is still present. It is most
likely to surface exactly where this project cares most: a summary of an
indictment alleging Chinese state-linked hacking or economic espionage.

The risk is **not uniform across the two tracks**:

- **Extraction is essentially unaffected.** Classifying "Motion Hearing set for
  3/5" into a date never touches the geopolitics of the case. A China-developed
  model is fine here if you want its structured-output strength.
- **Summaries are where the exposure is.** The summary writes prose about *who
  is charged with what*, drawn from the indictment. A refusal would at least be
  *visible*; the real worst case is quieter — a fluent, confident summary that
  softens the attribution, omits the state nexus, or slants the framing, reading
  exactly like a good one on its way to subscribers. A plausible-but-misleading
  summary erodes trust far more than a missing one, which is why the rest of the
  pipeline treats confident fabrication, not refusal, as its most dangerous
  failure mode — see the truthfulness guards in [AI case
  summaries](case-summaries.md).

Note what the project's own guards do and don't cover: the documents-only rule,
the grounding guard, and the rest catch **fabrication** and ground dates /
figures — but they do **not** catch **omission, softening, or slanting** of what
a document says. A summary that quietly under-states the charges is still
fully grounded in the document text, so it sails straight past them. The guards
don't neutralize this concern.

**Recommendation:** don't put a China-developed model on the **summary** track
for an adversary-nation caseload. Use a Western open model there (Gemma 4,
Mistral, Llama, gpt-oss) or keep summaries on a hosted Western model; use
whatever you like for extraction. And verify it yourself rather than taking
anyone's word — that's what the [benchmark harness](#benchmarking-a-local-model)
is for: run a real China-nexus indictment through two models and read the
summaries side by side.

### Reading a model tag

Ollama tags look like `gemma4:31b` or, spelled out, `gemma4:31b-it-q4_K_M`. The
parts:

- **`31b` / `e2b`** — the size (`e2b` / `e4b` are Gemma's "effective 2B/4B"
  edge sizes; the medium `26b`/`31b` are full models).
- **`-it`** — **instruction-tuned**: the model that follows directions, versus a
  base/pretrained model (`-pt`). Case Calendar's prompts *tell* the model what
  to do, so you always want the `-it` variant. A bare tag like `gemma4:31b` is
  an alias that already resolves to the instruction-tuned, Q4_K_M build.
- **`-q4_K_M`** — the **quantization**: 4-bit, k-quant, Medium. The common,
  well-balanced default. If a small model gives shaky JSON or thin summaries, a
  heavier quant (`q5_K_M`, `q6_K`, `q8_0`) buys quality for more RAM/less speed —
  often the cheapest fix before reaching for a bigger model.
- **`-mlx`** — an Apple-Silicon-optimized build (see [Apple
  Silicon](#apple-silicon-macs)). Text-only, which suits this project.

## Hardware requirements

The two numbers that decide what runs: **model weights** and **context (KV
cache)**. At the common `q4_K_M` quantization, weights are roughly **0.55 GB per
billion parameters** (about a 70–75% reduction from full precision). Context is
*extra* on top — a 32K-token window can add several GB, and the summary track is
what makes that window necessary. Ollama will spill what doesn't fit to CPU/RAM
and keep working, just slower, so "fits in VRAM" is about speed, not a hard
yes/no.

### VRAM by model size (Q4_K_M)

| Model size | Weights | Comfortable with 32K context |
| --- | --- | --- |
| 7–8B | ≈5–6 GB | 8–12 GB |
| 12–14B | ≈10–12 GB | 16 GB |
| 24–27B | ≈16–20 GB | 24 GB (or 16 GB at reduced context) |
| 31–32B | ≈18–22 GB | **32 GB** — won't fit a useful window on 24 GB |
| 70B | ≈40 GB | 32 GB with minimal CPU offload, or 2× 24 GB |

The "32K context" column is a reference point. The window this project
**recommends** is 128K (see [Context window](#context-window)), whose KV cache is
roughly 4× the 32K figure — so a model that only *just* fits 32K on a card will
overrun it at 128K. This is exactly why `gemma4:31b` (20 GB of weights) is not a
24 GB model for summaries: there's no room left for the cache at the window
summaries need.

Size a model by its actual download, not its parameter count — the multimodal
Gemma 4 variants are heavier than the per-billion rule suggests. The default
`gemma4:e4b` is **9.6 GB** (it carries vision/audio weights on top of its text
params), `gemma4:e2b` is 7.2 GB, `gemma4:26b` is 18 GB, and `gemma4:31b` is
20 GB.

### By GPU tier

The VRAM tier is what matters; the cards are examples across vendors.

| VRAM | Nvidia | AMD (ROCm) | Intel Arc | What runs |
| --- | --- | --- | --- | --- |
| **8 GB** | RTX 4060 / 5060 | RX 7600 | Arc A750 | too small for the default `gemma4:e4b` (9.6 GB) — use `gemma4:e2b` (7.2 GB) at a small window, or run summaries hosted |
| **12 GB** | RTX 3060 12GB / 5070 | RX 7700 XT | Arc B580 | runs the default `gemma4:e4b` at a reduced window; `gemma4:e2b` for the full 128K |
| **16 GB** | RTX 4060 Ti 16GB / 5070 Ti / 5080 | RX 7800 XT / 9070 XT | Arc A770 | **runs the default `gemma4:e4b` at the full 128K window** — the recommended local setup; also Mistral Small 24B or `gpt-oss:20b` at reduced context |
| **24 GB** | RTX 3090 / 4090 | RX 7900 XTX | — | runs the default `gemma4:e4b` for both tracks with room to spare, even at a 128K window. `gemma4:31b` does **not** fit a useful window here (its 20 GB of weights crowd out the KV cache) — for better summaries, use the hybrid setup (hosted summaries). See [Context window](#context-window) |
| **32 GB** | RTX 5090 | — | — | the home for the `gemma4:31b` summary upgrade at a real window; or a 70B with minimal offload |

A single consumer card tops out around **32B**, and only on the biggest one — a
32 GB **RTX 5090**. Mind the NVIDIA gap: 24 GB was the previous-gen flagship
(RTX 3090 / 4090), but the current line jumps from the 16 GB **RTX 5080**
straight to the 32 GB 5090, so on NVIDIA the `gemma4:31b` upgrade effectively
means a 5090. AMD has **no 32 GB consumer card at all** — the line tops out at
the 24 GB **RX 7900 XTX** (RDNA4's RX 9070 XT is 16 GB), which runs the default
`gemma4:e4b` well but is still short for a 31b summary at a real window. So on
AMD the 31b upgrade isn't a single-card option — use the hybrid (hosted) path or
two cards. 70B-class models likewise want 32 GB with offload, or two cards.

**Software support varies by vendor — VRAM is necessary but not sufficient.**
Nvidia (CUDA) is the most plug-and-play. AMD (ROCm) works well on recent Radeon
cards — the 24 GB RX 7900 XTX is a strong-value pick for running the default
`gemma4:e4b` locally — but doesn't cover every card. It's smoothest on bare Linux; on Windows, native
Ollama already drives the Radeon GPU directly, while running ROCm *inside* WSL2
needs a WSL-specific runtime (see below). Intel Arc runs local models (through `llama.cpp`'s Vulkan backend or
Intel's own tooling) but native Ollama support is still experimental in 2026 and
more hands-on — confirm it works before buying a card for this. Higher-VRAM
workstation cards exist beyond the consumer lineup (AMD Radeon Pro W7900 48 GB,
Intel Arc Pro), where the limiter is software maturity, not memory. Always check
your specific card against Ollama's [hardware support
page](https://docs.ollama.com/gpu).

### Windows (WSL2)

On Windows, Case Calendar itself runs inside **WSL2** (it's a Linux app, and
that's how this project is developed). Where you run **Ollama** depends on your
GPU vendor.

**Nvidia** is nearly transparent in WSL2: the Windows driver exposes
CUDA to WSL2 automatically (do *not* install a Linux GPU driver inside the
distro), and inference runs within a few percent of bare-metal Linux — so run
Ollama in WSL2 too.

**AMD**'s simplest path is the opposite: run Ollama
**natively on Windows**, where its ROCm/Vulkan build already drives recent Radeon
GPUs, and point Case Calendar at it from WSL2 with
`OLLAMA_BASE_URL=http://WINDOWS_HOST_IP:11434/v1` (the WSL default-gateway IP).
Running Ollama *inside* WSL2 on AMD is officially supported too, but takes an
extra step the Nvidia path doesn't: AMD's **ROCDXG** (`librocdxg`) runtime, which
reaches the Windows GPU driver through Microsoft's DXCore interface (`/dev/dxg`)
and supersedes the older roc4wsl method. It needs a current WSL-capable Adrenalin
driver (26.2.2 at the time of writing) and Ubuntu 22.04/24.04, and is installed
from the `librocdxg` Quickstart — not the standard Linux ROCm packages. See AMD's
[WSL how-to for Radeon and
Ryzen](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installryz/wsl/howto_wsl.html).

**Measured on an RX 7900 XTX (24 GB), gemma4, June 2026 — on AMD, prefer
native Windows over in-WSL2 ROCm.** Running Ollama *inside* WSL2 does work once
you add the ROCm backend, which the stock Linux install omits: drop the matching
`ollama-linux-amd64-rocm` bundle into `/usr/local/lib/ollama/` and set
`HSA_ENABLE_DXG_DETECTION=1` on the service so the runtime enumerates the GPU
through `/dev/dxg`. But on the same card, **native Windows was clearly better**
on two counts:

- **\~40% faster** generation on the small model — `gemma4:e4b` ran at \~83
  tok/s on native Windows vs \~58 tok/s in WSL2 (both at 100% GPU).
- It could **load `gemma4:31b`'s weights entirely in VRAM at 100% GPU**, which
  the WSL2 path could not — inside WSL2 the DXG/DXCore layer's memory overhead
  leaves too little room even for the inference compute buffer, so a realistic
  (10K+ token) prompt to the 31b failed with an out-of-memory HTTP 500 or a load
  timeout though the weights themselves loaded. But loading the weights is not
  the same as running summaries: either way, 24 GB has no room for a
  summary-sized context window on top of 31b's 20 GB of weights (see
  [Hardware](#hardware-requirements)) — WSL2 just hits the wall sooner. This is
  why the local default is `gemma4:e4b`, with 31b reserved for a 32 GB+ card.

Net: on AMD, run Ollama natively on Windows and point Case Calendar at it from
WSL2 (the `OLLAMA_BASE_URL` line above). Reserve in-WSL2 ROCm for small models
where the extra setup and the VRAM overhead are acceptable.

**Intel Arc** is the roughest path here: possible via Intel's tooling but layered
on already-experimental Ollama support. (Multi-GPU tensor parallelism via NCCL
doesn't work in WSL2, but that only affects multi-card rigs — single-GPU
inference is unaffected.)

### Apple Silicon (Macs)

Apple Silicon shares one pool of **unified memory** between CPU and GPU, so
usable model memory is roughly 65–75% of total RAM (an M-series Mac with 32 GB
can run a 24–32B model; 64 GB+ opens up larger ones). Recent Ollama ships
**MLX-optimized** builds for these chips — the `-mlx` tags in the library (e.g.
`gemma4:e2b-mlx`, `gemma4:31b-mlx`), which are text-only and a good match for
this project. Plain (GGUF) tags also run on Metal; MLX builds are tuned for
Apple's framework and tend to be faster on the same hardware.

## Configure

Ollama is **opt-in only**. The hosted providers auto-detect from their API
keys; Ollama has no key, so you select it explicitly in `.env`. No
`config.yaml` change is needed.

**Everything local** — both tracks. The default model is `gemma4:e4b` for
extraction *and* summaries, so the minimal config is one line:

```bash
LLM_PROVIDER=ollama
# Defaults to gemma4:e4b for both tracks (fits common consumer GPUs). If you
# have the VRAM, upgrade to the higher-quality 31B model — for both tracks
# (LLM_MODEL=gemma4:31b) or just summaries, where it helps most:
# LLM_SUMMARY_MODEL=gemma4:31b
```

**Hybrid** — local extraction, hosted (or Western-model) summaries. This is the
recommended setup for most consumer GPUs *and* the answer to the honesty
concern above:

```bash
LLM_EXTRACTION_PROVIDER=ollama   # local extraction
LLM_MODEL=gemma4:e4b
# summary track left to its hosted default (e.g. Anthropic Sonnet)
```

On startup, `sync` / `serve` / `summarize` log which model each track resolved
to (`extraction LLM: provider=ollama model=…` / `summary LLM: …`), so you can
confirm the wiring at a glance.

### Install and pull

```bash
# Install Ollama: https://ollama.com/download
ollama pull gemma4:e4b      # the default — both tracks
ollama pull gemma4:31b      # optional quality upgrade (summaries), if you have the VRAM
ollama serve                # usually already running as a service
```

### Context window

Local models default to a **small context window** — Ollama allocates
[4K on cards under 24 GB of VRAM and 32K on 24–48 GB](https://docs.ollama.com/context-length) —
and **silently truncates** anything longer instead of erroring. Extraction
sends one short entry at a time, so it's rarely affected. **Summaries are** —
they feed tens of thousands of tokens into one call, so with too small a window
the model would otherwise summarize only the first few pages.

Case Calendar guards against this rather than publishing half-baked output. It
learns the window (`OLLAMA_NUM_CTX` if set, otherwise the model's maximum via
Ollama's `/api/show`) and, if a prompt won't fit — checked before the call, and
again afterward against the tokens the server reports it actually read — it
**refuses instead of emitting a truncated result**: extraction skips the entry
(logged, retried next sync), the verify/dedupe passes return a no-op, and a
summary stores a short "this docket's documents are too large to summarize
within the model's configured context window" message on the index. Each refusal
logs a `WARNING` naming the remedy. So the fix below is about *avoiding* the
refusal, not preventing silent corruption.

**Set the window to 128K (`131072`) — the full window of the default
`gemma4:e4b`.** Ollama's own defaults are too small for summaries (4K under
24 GB of VRAM, 32K on 24–48 GB; it [recommends at least 64K](https://docs.ollama.com/context-length)
for large-context work), so you have to raise it. 128K comfortably covers
Case Calendar's summary prompts — the per-document char budgets bound how large
one gets — and the context-overflow guard cleanly refuses anything that still
wouldn't fit, so there's no downside to using the model's full window.

There are three places to set it; any one works:

1. **In Ollama itself** (simplest, applies to every model) — set the
   `OLLAMA_CONTEXT_LENGTH` environment variable on the server, or move the
   context-length slider in the Ollama desktop app's settings. See the
   [Ollama context length docs](https://docs.ollama.com/context-length):

   ```bash
   OLLAMA_CONTEXT_LENGTH=131072 ollama serve
   ```

2. **`OLLAMA_NUM_CTX`** in Case Calendar's `.env` (quick) — Case Calendar
   forwards it per request as the native `options.num_ctx`:

   ```bash
   OLLAMA_NUM_CTX=131072
   ```

   On Ollama this is reliable — Case Calendar talks to Ollama's native
   `/api/chat` endpoint. On a *non-Ollama* OpenAI-compatible server (see [Other
   servers](#other-openai-compatible-servers)) it rides through the OpenAI
   `extra_body` instead, and whether that server honors it varies, so verify it
   took effect or use option 1 or 3.

3. **A Modelfile `PARAMETER num_ctx`** (most reliable) — bake the window into a
   derived model and use that name:

   ```text
   FROM gemma4:e4b
   PARAMETER num_ctx 131072
   ```

   ```bash
   ollama create casecal-gemma -f ./Modelfile
   # then: LLM_MODEL=casecal-gemma
   ```

Confirm it took effect with `ollama ps`, which shows each loaded model's
allocated context and whether it's running on GPU or CPU. A bigger window costs
more VRAM (the KV cache in [Hardware](#hardware-requirements)): 128K fits a
24 GB card running `gemma4:e4b` comfortably; on a smaller card, drop it (64K is
the floor Ollama recommends for large-context work). Don't exceed what your
hardware can hold — see the warning below.

> **Don't set the window bigger than your computer can run.** Picking a 256K
> window (in `OLLAMA_NUM_CTX`, a Modelfile, or the Ollama desktop app) doesn't
> mean your GPU has the memory for it. If it doesn't, one of two things happens:
> Ollama keeps going by using your regular system memory — the answers are still
> correct, but it gets *much* slower — or, if there isn't enough memory at all,
> the request fails with an out-of-memory error.
>
> That out-of-memory error is a different problem from a prompt being too big,
> and the fix is the opposite: make the window *smaller* (or free up memory),
> not bigger. Case Calendar tells the two apart. When it runs out of memory it
> writes a clear warning saying so — it does not call it "documents too large."
> It also won't publish anything broken: it skips the entry, or leaves the
> summary blank. But the same error will happen on every run until you shrink
> the window or add memory. So pick a window size your card can actually run.

## Thinking models

Some open models (**Qwen3**, **DeepSeek**, **Gemma**) are *thinking* models:
they emit a hidden chain of reasoning before the answer, drawn from the **same
output budget** as the answer. That's a poor fit for high-volume structured
extraction — on a dense docket entry the reasoning can run to many thousands of
tokens, overrunning the output budget (the model hits the limit mid-thought and
returns an empty answer) and taking a minute or more per call.

Case Calendar handles this **automatically, per track**, on real Ollama (it uses
the native `/api/chat` endpoint — the only one that exposes the `think` control;
the OpenAI-compatible endpoint ignores it):

- **Extraction / verify / dedupe** (high volume): thinking is turned **off**, so
  the model emits the short JSON answer directly — fast and reliable.
- **Summaries** (rare — one per docket): thinking stays **on**, with a generous
  but **bounded** output budget, so the reasoning can finish and still produce
  the prose (an unbounded budget risks a runaway that can hang the GPU).

No configuration is needed — it keys off the model's reported `thinking`
capability. This is why a thinking model like Qwen3 works for extraction here at
all; without it, extraction would silently return nothing on the busiest entries.
On a *non-Ollama* OpenAI-compatible server there is no way to control thinking, so
a thinking model there may overrun on extraction — prefer a non-thinking model on
those servers, or run a thinking model on real Ollama.

## Other OpenAI-compatible servers

The provider is *named* `ollama`, but it also drives any local OpenAI-compatible
server. Case Calendar auto-detects which it's talking to: a real **Ollama**
server is driven through its native `/api/chat` endpoint (the only one that
supports [thinking control](#thinking-models)), while **LM Studio**,
**llama.cpp**'s `server`, **vLLM**, and similar — which speak the OpenAI API but
have no `/api/chat` — fall back to `/v1/chat/completions` and work with zero code
change, just without thinking control. For example, to drive LM Studio (handy on
Apple Silicon, with first-class MLX support and its own model browser):

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:1234/v1   # LM Studio's default port
LLM_MODEL=gemma-4-e4b
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Where the server listens. Point it at LM Studio, a remote box, etc. |
| `OLLAMA_NUM_CTX` | *(unset)* | Context window in tokens (see above). |

## Cost reporting

Local inference has no per-token API charge, so the cost estimator reports
`cost_est=$0.0000` for every local call and the run TOTAL stays at `$0.00`.
(This measures API billing only — your hardware and electricity aren't free, but
they aren't per-token either, which is the thing the estimator models.) The real
token counts are still logged on every `llm-tokens` line, so you can see how much
work each call did.

## Benchmarking a local model

The [model-comparison harness](development.md) builds a full store per model
from the same cached CourtListener data, so you can compare a local model's
extracted hearings/deadlines and summaries against the hosted providers (and
against each other) without re-spending on the hosted side — it caches their
responses.

Ollama isn't in the default comparison set — it needs a running server and a
pulled model, which a clean run can't assume — so add it as an extra column:

```bash
# Compare local Gemma 4 extraction against the committed default set:
uv run python model-comparison/build_provider_stores.py \
  --extra-variant ollama:gemma4:e4b \
  --validate

# Trust check: read one China-nexus indictment summarized two ways.
uv run python model-comparison/build_provider_stores.py \
  --extra-variant ollama:gemma4:31b \
  --extra-variant ollama:qwen3:32b \
  --case <a-china-nexus-case-id>
```

The spec is `ollama:<extraction-model>[,<summary-model>]` (the optional distinct
summary model is comma-separated, since an Ollama model tag like `gemma4:e4b`
already contains a colon). The harness pins each
column's models and replays the real pipeline, so the resulting store, ICS, and
index page reflect exactly what that local model produces — including, in the
second example, whether a China-developed model omits or softens anything a
Western model states plainly.

## Caveats

These aren't blockers, but they're where local differs from hosted — and exactly
what you'd want to measure when tuning:

- **JSON reliability for extraction.** Small local models adhere to structured
  output less reliably than the hosted small/fast tier. There's a cushion — the
  parser strips markdown fences and digs JSON out of chatter, and the request
  asks for JSON mode — but expect this to be the main accuracy variable. A
  heavier quant or a larger model helps.
- **Context window for summaries.** Covered above; the most common way a local
  setup produces wrong-looking output.
- **Model provenance.** Covered under [honesty](#model-provenance-and-honesty);
  the most important one for an adversary-nation caseload.
- **Speed.** Local throughput depends entirely on your hardware. A backfill
  `sync` over many dockets runs the extractor once per relevant entry, which can
  be slow on a CPU-only box.
- **Quality vs. the hosted defaults.** The hosted defaults were chosen against a
  measured scorecard (see [Cost](cost.md) and the comparison harness). A local
  model may match them on your caseload or may not — benchmark before relying on
  it for a public calendar.

## See also

- [Configuration](configuration.md) — every `config.yaml` option and the full
  environment-variable table.
- [Cost](cost.md) — measured per-provider numbers for the hosted providers.
- [Development](development.md) — the model-comparison harness and the cheap
  prompt-iteration workflow.
