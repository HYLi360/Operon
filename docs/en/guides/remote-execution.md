# Remote Execution with Slurm and SSH

`run-external` and `analyze` use the local subprocess backend (`local`) by default. The `execution:` section in `project.yaml` can switch execution to a local Slurm cluster (`slurm`) or an SSH host (`ssh`), such as an HPC head node or cloud VM.

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

- Install the optional dependency: `pip install 'operon[remote]'` or `pip install paramiko`.
- With `execution.ssh.scheduler: slurm`, commands are submitted and polled on the remote host with sbatch/squeue. Otherwise commands run directly on the host and stream stdout/stderr back to local log files.
- Remote Slurm captures the execution environment inside the job, so provenance records the compute node rather than the SSH login node. Probe failure does not affect the job result.
- For a typical login-node-to-compute-node setup, configure the login node as `host` and set `scheduler: slurm`. Operon runs `sbatch` on the login node, and Slurm dispatches work. The login and compute nodes must see the same `remote_root`. A second SSH hop to a compute node is not currently supported.
- A non-empty absolute POSIX `remote_root` rewrites validated project path prefixes in argv/cwd. Path escapes through `..` or symlinks are rejected. An empty value means the local and remote filesystems are shared.
- When `storage_remote` is configured, its root is inherited by default. Setting a different explicit `remote_root` is a configuration error.
- Unknown hosts are rejected by default. Add the host key to `~/.ssh/known_hosts`, configure `known_hosts`, or pin `host_key_sha256`. Use `insecure_accept_unknown_host: true` only for temporary test environments that accept the risk.
- `analyze` uploads local inputs over SFTP; `run-external` does the same for every declared `--input` when `remote_root` is non-empty. Staged paths must resolve inside the local project root, including after symlink resolution. If `sha256sum` is unavailable remotely, SHA-256 is calculated through the SFTP stream. Directories use a full deterministic tree hash; size-only checks are never used. Different existing content is not overwritten.
- With `storage_remote`, a locally absent input is checked against local SQLite, the remote manifest, and actual remote content, then consumed in place under the remote root instead of being downloaded. A successful live check reconciles a stale `MISSING` file status to `REMOTE_ONLY` through the audit log.
- One lazy SSH connection is reused for tool-version detection, remote input validation, database checks, and all commands in a batch.
- Before a run, only exact expected-output paths contained in `remote_root` are removed. Retrieved outputs are compared again after transfer. A different existing local output is a conflict.
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

See [Recipe Field Reference](../reference/recipe-fields.md#slurm-resource-overrides) for the complete field list.

> The automated tests for Slurm and SSH use simulated sbatch/squeue and in-memory SSH/SFTP implementations. The SSH/SFTP, remote-only analysis, and remote Slurm paths were also smoke-tested on 2026-09-04 against a Linux OpenSSH login node, a shared GPFS filesystem, and a Slurm compute node. Each deployment should still run a short local smoke task to validate its host keys, filesystem visibility, partitions, submission, cancellation, polling, and output retrieval.
