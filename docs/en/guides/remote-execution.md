# Remote Execution with Slurm and SSH

`run-external` and `analyze` use the local subprocess backend (`local`) by default. The `execution:` section in `project.yaml` can switch execution to a local Slurm cluster (`slurm`) or an SSH host (`ssh`), such as an HPC head node or cloud VM. For an end-to-end setup that also mirrors and evicts data to the same remote, see [Remote-First Operation](remote-first.md).

All backends use the same provenance contract: exit code, start/end time, and log paths are written to `workflow_runs` and `logs/workflow.jsonl`; success still requires exit code 0 and non-empty expected outputs; input and output checksum validation is unchanged.

## Configuration

All fields are optional; existing projects do not need to be changed:

```yaml
# project.yaml
execution:
  backend: local            # local | slurm | ssh
  slurm:
    partition: ""
    time: "24:00:00"
    mem_gb: 0               # 0 = do not write --mem
    extra_sbatch: []        # Additional #SBATCH lines
    setup_commands: []      # For example: ["module load blast/2.15"]
    poll_interval: 15       # squeue polling interval in seconds
  ssh:
    host: ""
    user: ""
    port: 22
    key_file: ""            # Empty = SSH agent/default keys; passwords are not supported
    remote_root: ""         # Absolute remote POSIX path; empty = shared filesystem
    storage_remote: ""      # Name of the remotes: entry holding REMOTE_ONLY input
    scheduler: none         # none | slurm
    connect_timeout: 30
    known_hosts: ""         # Optional additional known_hosts file
    host_key_sha256: ""     # Optional SHA256:... host-key pin
    insecure_accept_unknown_host: false
```

Override `execution.backend` for one command:

```bash
operon analyze --analysis blastn_nt --backend slurm
operon run-external --step quast --backend ssh \
  --command 'quast -o qc/quast_out raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta' \
  --expected-output qc/quast_out/report.tsv
```

## Slurm backend

Prerequisites and behavior:

- The project directory must be on a filesystem shared with compute nodes.
- `sbatch` and `squeue` must be in `PATH`; missing commands are configuration errors.
- Each run writes `logs/<run_id>.sbatch`. `--cpus-per-task` uses the thread count. Optional time, partition, memory, `extra_sbatch`, and `setup_commands` are included.
- The job is submitted with `sbatch --parsable` and polled with `squeue` at `poll_interval`.
- After the job disappears, Operon reads the `logs/<run_id>.exitcode` file written by the script and falls back to `sacct` if needed.
- stdout/stderr are written to `logs/<run_id>.stdout.log` and `.stderr.log`.
- Local and remote Slurm both honor the configured polling interval. Briefly invisible exit-code files are retried, and warning lines before the submit output do not prevent job-ID parsing.
- Timeouts are controlled by `--timeout` in seconds; a timeout attempts `scancel`.

## SSH backend

Prerequisites and behavior:

- The controller may run on Linux or macOS. Local project paths are resolved
  before mapping, so macOS filesystem aliases such as `/var` and
  `/private/var` identify the same project root without weakening symlink
  escape checks. The SSH compute-side requirements below are unchanged.
- Paramiko is included in the standard `OperonDBS` installation.
- With `execution.ssh.scheduler: slurm`, commands are submitted and polled on the remote host with sbatch/squeue. Otherwise commands run directly on the host and stream stdout/stderr back to local log files.
- Slurm array submission (recipe `slurm.array: true`) works identically through the SSH backend when the remote scheduler is Slurm: the array manifest and sbatch script are staged over SFTP, the array is submitted with a remote `sbatch`, and per-task stdout/stderr, exit codes, and `sacct` accounting are pulled back per task. Direct SSH (`scheduler: none`) has no array support and falls back to per-file submission.
- Remote Slurm captures the execution environment inside the job, so provenance records the compute node rather than the SSH login node. Probe failure does not affect the job result.
- For a typical login-node-to-compute-node setup, configure the login node as `host` and set `scheduler: slurm`. Operon runs `sbatch` on the login node, and Slurm dispatches work. The login and compute nodes must see the same `remote_root`. A second SSH hop to a compute node is not currently supported.
- A non-empty absolute POSIX `remote_root` rewrites validated project path prefixes in argv/cwd. Path escapes through `..` or symlinks are rejected. An empty value means the local and remote filesystems are shared.
- When `storage_remote` is configured, its root is inherited by default. Setting a different explicit `remote_root` is a configuration error.
- Unknown hosts are rejected by default. Add the host key to `~/.ssh/known_hosts`, configure `known_hosts`, or pin `host_key_sha256`. Use `insecure_accept_unknown_host: true` only for temporary test environments that accept the risk.
- `analyze` uploads local inputs over SFTP; `run-external` does the same for every declared `--input` when `remote_root` is non-empty. Staged paths must resolve inside the local project root, including after symlink resolution. If `sha256sum` is unavailable remotely, SHA-256 is calculated through the SFTP stream. Directories use a full deterministic tree hash; size-only checks are never used. Different existing content is not overwritten.
- With `storage_remote`, a locally absent input is checked against local SQLite, the remote manifest, and actual remote content, then consumed in place under the remote root instead of being downloaded. A successful live check reconciles a stale `MISSING` file status to `REMOTE_ONLY` through the audit log.
- One lazy SSH connection is reused for tool-version detection, remote input validation, database checks, and all commands in a batch.
- Before a run, an existing expected-output path under `remote_root` is renamed to a `<path>.operon-prev-<uuid>` backup instead of being deleted; the backup is dropped once the run succeeds and the new output verifies, and a failed or interrupted run best-effort restores it in place. Retrieved outputs are compared again after transfer. A different existing local output is a conflict.
- On SSH direct-command timeout, Operon uses a permission-restricted remote PID file to send TERM and then KILL to the process group. If the PID file or termination command is unavailable, the error states that the remote process may still be running. Remote Slurm uses `scancel`, records whether the cancellation request was accepted, and retrieves available partial logs and the job-side environment probe.
- SSH direct mode requires util-linux `setsid` on the remote host. It is normally available on Linux. macOS/BSD remote hosts do not provide it; use a Linux Slurm host or the local backend instead.
- A remote `reference` database must be deployed in advance at the recipe `database` path and must declare `database_checksum`. A `mutable_cache` requires `database_version` and is created remotely if missing.
- Tool-version detection (`version_args + version_pattern`) runs through the same non-local backend.

## Recipe-level Slurm overrides

A recipe can override fields from `execution.slurm`:

```yaml
recipes:
  busco_autolineage:
    slurm:
      mem_gb: 64
      time: "72:00:00"
```

See [Recipe Field Reference](../reference/recipe-fields.md#slurm-resource-overrides) for the complete field list, including the `array` / `array_concurrency` job-array keys, which apply equally to local Slurm and to remote Slurm over SSH.

## Smoke-testing a deployment

The automated tests for Slurm and SSH use simulated sbatch/squeue and in-memory SSH/SFTP implementations. The SSH/SFTP, remote-only analysis, and remote Slurm paths have also been smoke-tested against real clusters:

- 2026-09-04 — Linux OpenSSH login node, shared GPFS filesystem, Slurm compute node;
- 2026-09-20 — OpenSSH login node with `sbatch`/`squeue`/`sacct` and a `cu` partition, mirror and compute root on one shared filesystem: `evict` → remote-only `analyze` of an HMMER recipe → `pull`, over 13 MB of input.

Each deployment should still run a short task of its own; the checklist, reference run and pitfalls below are what validated the 2026-09-20 deployment.

### Checklist

1. **Mirror reachability and host key.** `operon remotes` must list the mirror as `ok`; an unknown host is rejected until its key is in `known_hosts` or `host_key_sha256` is pinned.
2. **One filesystem, two node classes.** `remote_root` (inherited from `storage_remote` by default) must be visible from the login node that runs `sbatch` and from the compute nodes.
3. **Scheduler.** `sbatch`, `squeue` and `sacct` must be in the login node's non-interactive `PATH`, and the configured partition must exist.
4. **Tools on the compute side.** Every recipe `executable` must be on the `PATH` the submitted job inherits. `operon tools-check` probes the local machine only, so it cannot validate the remote side; a binary reachable only from an interactive shell, or only from a conda environment the non-interactive shell does not activate, fails at probe or submit time.
5. **Reference database.** Deploy it in advance at the remote target path and declare `database_checksum`; prefer a project-relative `database` so the path is mapped into the remote root (see [Databases on SSH remotes](../reference/recipe-fields.md#databases-on-ssh-remotes)).
6. **A short recipe over one small file**, so the smoke run takes minutes rather than hours.

To also exercise remote-only input consumption, configure `execution.ssh.storage_remote` as in [Remote-First Operation](remote-first.md) and evict the file before the run.

### Reference smoke run

```bash
operon evict --remote mycluster --file-id FIL_000001                    # input becomes REMOTE_ONLY
operon analyze --analysis <recipe> --entity-id <entity> --threads 24 --backend ssh
operon analyze --analysis <recipe> --entity-id <entity> --threads 24 --backend ssh   # must report cached
operon pull --remote mycluster --file-id FIL_000001                     # restore the local bytes
```

Then check both planes:

| Where | What to verify |
|---|---|
| `analysis_jobs` | `launcher = [ssh:user@host]`, `status = completed`, `output_sha256`, `environment_id`, `recipe_snapshot_id`, and a `command` naming the resolved database |
| `execution_details` | `scheduler_job_id`, `slurm_elapsed_seconds`, `host`, and the remote `script` path under `remote_root/logs/` |
| `execution_environments` | the probe describes the **compute node** (CPU model, cpuset), not the login node |
| remote host | `logs/<run_id>.sbatch` contains rewritten paths only — the `cd` and every project path inside `remote_root`; `<run_id>.exitcode` holds `0` |
| both sides | the retrieved output's SHA-256 equals `sha256sum` of the remote file |
| local `logs/` | `<run_id>.stdout.log` / `.stderr.log` are copies retrieved from the remote job |
| `changes` / `locations` | the eviction to `REMOTE_ONLY` and the restore to `CHECKSUM_VERIFIED` are audited, and the placeholder under `.operon/placeholders/` is gone after `pull` |

A second identical run must report `cached` for the file instead of submitting another job; if it resubmits, the cache identity changed (a different backend or host/root, an edited recipe, or a different resolved `database`).

### Pitfalls

- **A `database` outside the project root is never mapped.** Absolute values — including `~/...`, which is expanded before mapping — are passed through verbatim, so the compute side must have that exact path. A recipe that works locally with `/data/db/Pfam-A.hmm` or `~/resources/hmm/PF00010.hmm` fails remotely. A project-relative value is mapped into `remote_root` instead, which is what most recipes want.
- **`--dry-run` validates what needs no command execution.** The local database existence check runs, and for a remote `reference` database so does the provisioning stat, so a database that exists nowhere fails the preview with the message a real run reports — `reference database not found: <path>; edit config/tools.yaml`, or `remote reference database is not provisioned at <path>` — and the command exits non-zero. A remote `mutable_cache` has nothing to verify yet, and the tool version is still reported as `not probed (backend=…)` because probing it would execute a command. Validate a deployment with one real file, not with `--dry-run`.
- **A remote `reference` database must declare `database_checksum`.** Without it the run is rejected before anything is submitted: `remote reference databases require database_checksum so cache identity does not depend on a missing local path`.
- **The reference database is not a manifest file.** `push`, `evict` and `pull` move ingested files only, so `operon remotes` does not count the database and a new mirror root needs it redeployed by hand.
- **Remote-only semantics come from the backend, not from the file's status.** Any `ssh` run with a non-empty `remote_root` skips the local database existence check, requires `database_checksum`, and mixes the SSH location into the database cache identity. Results are therefore not shared between the `local` and `ssh` backends, or between two hosts/roots — a backend switch recomputes by design.
- **The input never travels back on its own.** A locally present input is uploaded over SFTP; a `REMOTE_ONLY` input is checked against the local manifest, the remote manifest and the live remote bytes, then consumed in place. Either way the bytes stay where they are, and `pull` remains an explicit step.
- **Tool outputs may embed the command that produced them.** HMMER's `--tblout`/`--domtblout` banner records the query and target paths, the full argv, the working directory and the date, so the same input analyzed locally and remotely yields different bytes even when the hit table is identical (observed on one file: 135 identical data lines, 5 differing banner lines, `output_sha256` `a64cc0c5…` → `1a0fc453…`). Treat `output_sha256` as host- and path-specific: a re-run on another backend overwrites the recorded output, and the older job row's hash then no longer matches the file on disk — such a row is marked `superseded` the next time it is hit. This is another reason the cache identity includes the SSH location.
- **Tool versions are probed through the backend, so keep the two installations compatible.** `qc-measure` payloads are the one place where a version mismatch is only a warning; for `analyze` results, a remote tool of a different version changes the recorded `tool_version` and therefore the cache identity.
