# Recipe configuration model

`config/tools.yaml` defines external analyses declaratively. A recipe specifies the input artifact, how the program is launched, where output goes, the cache identity, and how results are written back.

## Execution flow

A single `analyze` run performs, in order:

1. Selects the input from the `files` manifest using `entity_type + file_role + format`;
2. Uses `input_kind` to check whether the path should actually be a file or a directory, and re-verifies the content hash;
3. Probes the external tool version, resolves the database path, and computes the database identity;
4. Computes the unique target path of the output artifact;
5. Renders `${...}` placeholders into an argument array;
6. Looks up a completed cache entry using input, arguments, tool version, and database identity;
7. On a cache miss, runs the external program and verifies that the file or directory output exists and is non-empty;
8. Computes the output content hash and writes results into `analysis_results`, `analysis_hits`, and `qc_results` through the result parser.

Configuration fields fall into five groups:

| Question | Corresponding fields |
|---|---|
| Which program, launched from where? | tool-level `executable`, `run_method`, version fields |
| Which archived data may be used as input? | `entity_type`, `file_role`, `format`, `input_kind` |
| Where do results go, as file or directory? | `output_subdir`, `output_kind`, `output_name`, `output_suffix` |
| How is the command line composed? | `arguments` and placeholders |
| How is the database identified and output machine-read? | `database*`, `result_parser`, and parser-specific fields |

## YAML hierarchy

`config/tools.yaml` has three levels: global launch defaults, tools, and recipes.

```yaml
version: 1

conda:
  bin: conda
  run_args:
    - run
    - --no-capture-output

tools:
  example_tool:                 # tool name; recorded in provenance
    description: Example tool
    executable: example
    run_method: "conda run --no-capture-output -n example"
    version_args: ["--version"]
    version_pattern: 'example\s+([^\s]+)'

    recipes:
      example_analysis:         # analysis name; passed to analyze --analysis
        description: Example recipe
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        input_kind: file
        output_kind: file
        output_suffix: .example.tsv
        arguments:
          - --input
          - ${input}
          - --output
          - ${output}
          - --threads
          - ${threads}
        result_parser: none
```

One tool may contain multiple recipes — for example, the same `blastp` can use different databases, parameters, and result limits. Recipe names should be unique across the whole configuration; otherwise lookups silently use the first matching entry.

## Tool-level fields

| Field | Required / default | Meaning |
|---|---|---|
| `description` | optional | Human-readable description |
| `executable` | optional, defaults to the tool name | Program name or absolute path actually executed |
| `run_method` | optional, defaults to direct execution | Launch prefix placed before `executable`; a string or a structured mapping |
| `version_args` | recommended | Argument list appended to the program for version probing |
| `version_pattern` | recommended | Regex extracting the version from merged stdout/stderr; the first capture group is the version |
| `recipes` | required | Mapping of recipes provided by this tool |

The simplest direct launch:

```yaml
executable: blastn
run_method: ""
```

A string launch prefix is split with shell-style quoting but executed without a shell:

```yaml
executable: busco
run_method: "mamba run -n busco_6.1.0"
```

The effective argument array is:

```text
mamba | run | -n | busco_6.1.0 | busco | <rendered arguments...>
```

Therefore `run_method` must not rely on pipes, redirection, `$VAR`, glob expansion, or command substitution. When you need those behaviors, build an explicit wrapper executable and let the wrapper manage its own internals.

The structured conda/mamba form separates the environment name from arguments more clearly:

```yaml
run_method:
  mode: conda
  bin: mamba
  env: busco_6.1.0
  args: [run, --no-capture-output]
```

Supported structured `mode` values:

| mode | Fields | Behavior |
|---|---|---|
| `conda` | `env` required; `bin`, `args` optional | Produces `<bin> <args> -n <env> <executable>` |
| `prefix` | `prefix` list | Prepends the list verbatim before the executable; suitable for container launchers |
| `path` | none | No prefix; runs the executable directly |

`tools-check` runs the version command for each tool:

```bash
operon --project . tools-check
```

The version string participates in the cache identity. After a program upgrade, results produced by the old version are never reused by mistake, even if every other parameter is identical.
