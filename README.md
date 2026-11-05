# LitLens

LitLens is a literature review agent for Claude Code. Give it a research question and it finds real papers, scores them, reads the best 3-5, and reports what they achieved and where they fall short.

## What it does

1. **Searches** arXiv, Crossref, OpenAlex and Semantic Scholar from three angles, then adds the papers the results keep citing, which are usually the field's foundations.
2. **Scores** each candidate on citation velocity, recency and how well its abstract matches your question, before reading any full text.
3. **Reads** the top 3-5 papers and extracts each method, its benchmarks against named baselines, and the limitations its authors admit.
4. **Reports** an executive summary, a comparison matrix, deep-dives, research gaps, a screening table and numbered references.

Every paper in a report comes from a search in that run, and each number cites the table or section it came from. Anything LitLens couldn't read is labelled rather than filled in. If every source fails, it says so instead of writing from memory.

## Example

These are excerpts from a real report for `KV cache optimization in local LLMs`.

> **Executive Summary**
>
> Local-focused KV cache work goes beyond compressing the cache in place and manages it as swappable state across RAM and disk, using chunk-wise swapping [1], predicted loading from disk [2], persistent quantized caches [3] and kernel-bypass NVMe paths [4], while rolling windows with attention sinks [5] remain the simplest baseline. Reported gains are large but not directly comparable, because each paper uses its own hardware, batch size and baseline, and none of the five tests against another.

| Title | Key Contribution | Primary Metric / Benchmark | Limitations |
|---|---|---|---|
| [KVSwap](https://arxiv.org/abs/2511.11907) [2] (MobiSys; first public 2025) | Keeps the full KV on disk. A compact low-rank K cache in RAM predicts which groups of entries to preload, and a reuse buffer avoids re-reading them. | LLaMA3-8B, 32K context, batch 1, 310 MiB KV budget on NVMe: 6.85 tok/s vs 0.4 (FlexGen) and 6.38 (ShadowKV), and 9.22 for vLLM with 31 GiB in RAM (Tables 1, 4). | Lossy: the tight budget on eMMC loses 22.9% on RULER (Table 2). Prefill is not optimized yet (§7). |

## Install

There are two ways to install LitLens, and each uses a different command.

### Option 1: Plugin

```bash
claude plugin install litlens --marketplace Manishmaurya89/litlens
```

This needs Claude Code 2.1.292 or later. On an older version, run these instead:

```bash
claude plugin marketplace add Manishmaurya89/litlens
claude plugin install litlens@litlens-marketplace
```

Then start Claude Code and run:

```
/litlens:litlens <your research question>
```

To get a later version, run `claude plugin update litlens@litlens-marketplace`.

### Option 2: npx skills

Run this in the project where you want to use LitLens:

```bash
npx skills add Manishmaurya89/litlens --agent claude-code --yes
```

This copies LitLens into the project's `.claude/skills/` folder and adds a `skills-lock.json` file. Then start Claude Code in that project and run:

```
/litlens <your research question>
```

## Requirements

- [Claude Code](https://code.claude.com/docs)
- Python 3.9 or later, available as `python3`. There are no packages to install.

## Optional API keys

LitLens works without keys. Without them, Semantic Scholar's search is usually rate-limited and OpenAlex's is sometimes throttled, so reviews rely more on arXiv, Crossref and lookups by ID. Each report's coverage note says which sources failed.

To remove those limits, set these before starting Claude Code:

```bash
export OPENALEX_API_KEY="your-key"
export S2_API_KEY="your-key"
```

You can request keys from [OpenAlex](https://openalex.org/rest-api) and [Semantic Scholar](https://www.semanticscholar.org/product/api#api-key-form). Runs with valid keys haven't been tested yet.

## Cost per review

You pay for your own Claude usage. These are the costs Claude Code reported in test runs on 5 November 2026:

| Model | Outcome | Cost | Time |
|---|---|---|---|
| Sonnet 5.5 | Full report | $0.33-$0.64 | 1.5-3.5 min |
| Haiku 5.5 | Full report (one run) | $0.11 | 3.7 min |
| Sonnet 5.5 | Stops early, such as no topic or no relevant papers | $0.04-$0.12 | Under 1 min |

Larger models will cost more, and those haven't been measured.

## Known limits

- **macOS only so far.** LitLens has been tested only on macOS. Linux and Windows are untested, and `python3` may not exist on Windows.
- **Paywalled papers end up abstract-only.** Their reports say so, but they have little benchmark or limitation detail. In tests on materials science and engineering topics, only 1-2 of the 5 selected papers could be read in full.
- **Reports aren't independently checked.** The numbers in the test reports matched their sources, but no check runs on your report, so verify any number you plan to rely on. The model can also occasionally drift from the report format.

## Data sources

LitLens sends your search terms to the public APIs of arXiv, Crossref, OpenAlex and Semantic Scholar. It downloads open-access copies of the selected papers to a temporary folder on your machine.

LitLens is an independent project. It is not affiliated with or endorsed by arXiv, Crossref, OpenAlex, Semantic Scholar or any other data source. Each paper remains under its own license.

## Contributors

- **Manish Maurya** created LitLens, defined how it works and maintains it.
- **Claude**, Anthropic's AI assistant, built the skill and search script, ran the tests and wrote the documentation, working in [Claude Code](https://code.claude.com/docs).

LitLens is an independent project, not an Anthropic product.

## License

[MIT](LICENSE)
