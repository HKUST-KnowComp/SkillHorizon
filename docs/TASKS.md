# Task Construction

## Custom Tasks

`run.py build` constructs an initial business world, materializes its skill
resources, and runs a public reference policy to check feasibility and measure
workload. It uses no model API.

The default parameters make small development tasks. Pass a JSON object with
`--config` to override constructor parameters, or `--seed` to select another
seed. If the JSON contains `seed`, that value takes precedence over `--seed`.

```bash
python run.py build --environment FinanceOps --output tasks/finance-custom \
  --config examples/finance-small.json
python run.py build --environment DocOps --output tasks/doc-custom \
  --config examples/doc-small.json
```

| Environment | Example construction controls |
|---|---|
| FinanceOps | `family`, `variant`, `stages`, `width`, `seed` |
| FieldOps | `groups`, `device_counts`, `business_scenario`, `resource_profile` |
| SocialOps | `campaigns`, `waves`, `scenario`, `dependency_pattern` |
| LogisticsOps | `cohorts`, `legs`, `scenario`, `dependency_pattern` |
| ResearchOps | `sections`, `rounds`, `studies_per_section`, `dependency_pattern` |
| DocOps | `documents`, `depth`, `topology`, `scenario` |
| FactoryOps | `levels`, `width`, `terminal_quantity`, `dependency_pattern` |

Parameters are passed to the environment's constructor. Valid combinations and
constraints are domain-specific; a seed changes an instance within its configured
mechanism. Measure workload after construction rather than assigning it from the
seed or configuration name.

## Selected Task Records

`manifests/` retains 150 construction records per environment, totaling 1,050.
These identify the selected tasks and their parameters, workload bands, and
recorded reference measurements. They do not contain model outcomes.

To inspect a selection without executing it:

```bash
python -c 'import json; d=json.load(open("manifests/FinanceOps.json")); print(json.dumps(d["tasks"][0], indent=2))'
```

To use its constructor parameters:

```bash
python -c 'import json; d=json.load(open("manifests/FinanceOps.json")); print(json.dumps(d["tasks"][0]["parameters"]))' > /tmp/finance-parameters.json
python run.py build --environment FinanceOps --output tasks/finance-selected \
  --config /tmp/finance-parameters.json
```

The manifests are construction records, not serialized worlds. Historical
reference policies and measurements are retained in each record; the current
builder uses its own reference entry point and writes a new measurement. Exact
reconstruction of the evaluated instances requires the matching constructor,
resource snapshot, and reference policy.

## Generated Bundle

| File | Role |
|---|---|
| `manifest.json` | Environment, parameters, reference-call count, measured workload |
| `public.json` | Initial public task messages |
| `reference.json` | Public reference actions and tool results for offline checks |
| `task/` | Initial state, skills, domain resources, and construction outputs |

FieldOps also writes a `harbor-task/` export, selected by its manifest. Consumers
resolve the task path from `manifest.json` rather than assuming one layout.

Agents receive public instructions and tool observations. Private evaluation
state and the reference solution stay outside their input. Each evaluation
starts from the initial checkpoint, not the completed reference world.
