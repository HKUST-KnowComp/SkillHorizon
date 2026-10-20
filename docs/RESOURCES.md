# Resource Setup

FinanceOps and FieldOps generate their worlds without external database files.
SocialOps, LogisticsOps, ResearchOps, DocOps, and FactoryOps use database templates
and MCP support code from the `Infinite_context_compact-main` collection.

Obtain an authorized copy and retain this layout:

```text
external-assets/
  envs/
    C_06_01_01/           # SocialOps
    C_09_01_35/           # LogisticsOps
    C_11_01_01/           # ResearchOps
    C_25_05_01/           # DocOps
    C_50_05_01/           # FactoryOps
```

Each environment directory needs `data/database/*.json` and its `mcp_servers/`
support code. Do not rename individual database tables or support modules.

```bash
export SKILLHORIZON_EXTERNAL_ASSETS=/path/to/external-assets
python run.py build --environment DocOps --output tasks/doc-small
```

`--source-root /another/root` overrides the environment variable. Without either,
the builder looks under `assets/` in this checkout. Missing resource directories
are reported before creating a task output directory.

## Tokenizer Cache

Reference measurement uses tiktoken. The first use can download vocabulary files.
For offline use, point `TIKTOKEN_CACHE_DIR` to an existing populated cache:

```bash
export TIKTOKEN_CACHE_DIR=/path/to/tokenizer-cache
```

| Encoding | Cache filename |
|---|---|
| `o200k_base` | `fb374d419588a4632f3f557e76b4b70aebbca790` |
| `cl100k_base` | `9b5ad71b2ce5302211f9c61530b329a4922fc6a4` |

Without an explicit cache path, `run.py` uses `assets/tokenizers/`.
Resource acquisition and tokenizer downloads are separate from model API calls.

## Distribution

External database payloads and tokenizer caches are not bundled in this private
code checkout. Redistribution permissions and an accessible resource distribution
must be settled before the public release supports an end-to-end fresh setup of
all seven environments. See [resource notices](../THIRD_PARTY_NOTICES.md).
