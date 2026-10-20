# Resource and Dependency Notices

## Environment Resources

Five environment builders use database templates and MCP support code from the
`Infinite_context_compact-main` collection:

| Environment | Resource identifier |
|---|---|
| SocialOps | `C_06_01_01` |
| LogisticsOps | `C_09_01_35` |
| ResearchOps | `C_11_01_01` |
| DocOps | `C_25_05_01` |
| FactoryOps | `C_50_05_01` |

These resources supply records for generated business worlds. They are not
observations of real users executing benchmark tasks. FinanceOps and FieldOps
construct their initial worlds programmatically.

The external database files and MCP support packages are not included in this
checkout. Their redistribution terms must be resolved with the rights holders
before bundling them for public release. See [Resource Setup](docs/RESOURCES.md)
for using an authorized external copy.

## Dependencies and Tokenization

Harbor, Inspect AI, other Python dependencies, and native agent CLIs retain their
respective licenses. Pinned host dependencies are listed in `requirements.txt`;
native container requirements are maintained in `integrations/native_agents/`.

Tokenization uses OpenAI's `o200k_base` and `cl100k_base` vocabularies through
tiktoken. These caches are downloaded by the tokenizer or supplied externally;
they are not bundled here.

## Project Code and Task Records

The repository retains seven task-construction manifests from the selected
benchmark release. Model-result tables and historical execution logs are not
part of this repository's current contents.

No project license is assigned by this code publication. Dependencies and
inherited material retain their existing terms; public repository access does
not grant permission to redistribute the excluded external resources.
