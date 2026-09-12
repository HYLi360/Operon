"""Environment scope, reconstruction, fingerprint and degradation contracts."""
import base64
from contextlib import closing
import json
import platform
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from operon.cli import main
from operon.config import Project
from operon.database import Database
from operon.environment import environment_summary, parse_probe_output
from operon.environment_capture import capture_local, export_conda, probe_command
from operon.errors import ValidationError
from operon.execution import SlurmConfig, render_slurm_script
from operon.workflow import run_external_command


def package(**overrides):
    return {"name": "demo", "version": "1.2", "build": "h0_1", "build_number": 1,
            "subdir": "linux-64", "sha256": "a" * 64,
            "url": "https://example.org/channel/linux-64/demo-1.2-h0_1.conda", **overrides}


def encoded(key, value):
    return key + "=" + base64.b64encode(value.encode()).decode() + "\n"


def document(records=None, extra="", complete=True):
    return parse_probe_output(
        "capture_schema=1\nos=Linux\nos_release=6.1\nmachine=x86_64\nhostname=node\n"
        "conda_prefix=/old/location\nconda_present=1\n"
        + "".join(encoded("package", json.dumps(item)) for item in (records if records is not None else [package()]))
        + extra + ("capture_complete=1\n" if complete else ""))


def test_artifact_export_and_independent_fingerprints():
    first = document(extra=encoded("cpu", "model name : CPU\nflags : avx sse\n"))
    second = document(extra="hostname=another\nconda_prefix=/new/location\n" + encoded("cpu", "flags : avx sse\nmodel name : CPU\n"))
    assert first["conda"]["package_fingerprint"] == second["conda"]["package_fingerprint"]
    assert first["hardware_fingerprint"] == second["hardware_fingerprint"]
    assert first["system_fingerprint"] == second["system_fingerprint"]
    assert first["conda"]["package_fingerprint"] != document([package(build="h0_2")])["conda"]["package_fingerprint"]
    assert first["hardware_fingerprint"] != document()["hardware_fingerprint"]
    assert export_conda(first) == "@EXPLICIT\n" + package()["url"] + "#" + "a" * 64 + "\n"
    spec = yaml.safe_load(export_conda(first, "yaml"))
    assert spec["dependencies"] == ["demo=1.2=h0_1"]
    assert "prefix" not in spec
    assert spec["channels"] == ["https://example.org/channel"]


@pytest.mark.parametrize("records,extra,complete", [
    ([], "", True), ([package(sha256="", md5="")], "", True),
    ([package(url="")], "", True), ([package()], "", False),
    ([{}], "", True), ([package()], "package=***\n", True),
    ([package()], encoded("package", "not json"), True),
])
def test_incomplete_inventory_cannot_be_exported(records, extra, complete):
    env = document(records, extra, complete)
    assert env["conda"]["status"] == "partial"
    with pytest.raises(ValidationError):
        export_conda(env)
    with pytest.raises(ValidationError):
        export_conda(env, "yaml")


def test_md5_fallback_redaction_and_pip_limitations():
    env = document([package(sha256="", md5="b" * 32,
                            url="https://user:secret@example.org/t/token/channel/linux-64/pkg.conda?token=secret")],
                   encoded("pip_distribution", "custom-1.0.dist-info\n"))
    serialized = json.dumps(env)
    assert "secret" not in serialized and "/t/token" not in serialized
    assert "#" + "b" * 32 in export_conda(env)
    assert env["conda"]["pip_distributions"] == ["custom-1.0.dist-info"]
    assert "pip/local" in env["conda"]["scope"]


@pytest.mark.parametrize("argv", [
    ["conda", "run", "-n", "with spaces", "blastp"],
    ["/bin/micromamba", "-r", "/root dir", "run", "--prefix=/env", "blastp"],
    ["mamba", "run", "--no-capture-output", "-p", "/env", "--", "blastp"],
])
def test_launcher_parsing(argv):
    command = probe_command(argv)
    assert command[:-3] == argv[:-1]
    assert command[-3:-1] == ["sh", "-c"]


@pytest.mark.parametrize("argv", [
    ["conda", "list"], ["conda", "run", "--unknown", "blastp"],
    ["conda", "run", "-n"], ["conda", "run"], ["docker", "run", "x"],
])
def test_unsupported_launcher_is_not_guessed(argv):
    assert probe_command(argv) is None


@pytest.fixture
def fake_conda(tmp_path, monkeypatch):
    prefix = tmp_path / "tool env"
    meta = prefix / "conda-meta"
    meta.mkdir(parents=True)
    (meta / "demo.json").write_text(json.dumps(package()))
    manager = tmp_path / "micromamba"
    manager.write_text('#!/bin/sh\n[ "$1" = run ] || exit 2\nshift\n[ "$1" = -p ] || exit 2\nexport CONDA_PREFIX="$2"\nshift 2\nexec "$@"\n')
    manager.chmod(0o755)
    monkeypatch.setenv("CONDA_PREFIX", "/wrong/controller/env")
    return [str(manager), "run", "-p", str(prefix)]


def test_actual_target_environment_and_workflow_roundtrip(tmp_path, fake_conda, capsys):
    project = Project.init(tmp_path / "project")
    with closing(Database(project.db_path)) as db:
        run = run_external_command(db, project, [*fake_conda, "true"], step="smoke")
        row = db.conn.execute("SELECT document FROM execution_environments WHERE environment_id=?",
                              (run["environment_id"],)).fetchone()
        env = json.loads(row["document"])
        assert env["conda_prefix"] == fake_conda[-1]
        assert env["conda"]["status"] == "captured"
    assert main(["--project", str(project.root), "environments", "export", run["environment_id"]]) == 0
    assert "@EXPLICIT" in capsys.readouterr().out
    assert main(["--project", str(project.root), "environments", "show", run["environment_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["system_fingerprint"] == env["system_fingerprint"]
    assert main(["--project", str(project.root), "environments", "list"]) == 0
    assert run["environment_id"] in capsys.readouterr().out
    assert main(["--project", str(project.root), "environments", "show", "missing"]) != 0


def test_slurm_probe_runs_after_setup_inside_launcher(tmp_path, fake_conda):
    probe = tmp_path / "job.env"
    script = render_slurm_script(
        job_name="smoke", command_line=shlex.join([*fake_conda, "true"]), cwd=str(tmp_path),
        stdout_path=str(tmp_path / "out"), stderr_path=str(tmp_path / "err"),
        exitcode_path=str(tmp_path / "exit"), threads=1,
        slurm=SlurmConfig(setup_commands=["export OMP_NUM_THREADS=3"]), probe_path=str(probe))
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    env = parse_probe_output(probe.read_text())
    assert probe.stat().st_mode & 0o077 == 0
    assert env["conda_prefix"] == fake_conda[-1]
    assert env["runtime_settings"]["OMP_NUM_THREADS"] == "3"
    assert env["conda"]["status"] == "captured"


def test_probe_failure_and_opaque_shell_are_explicit(tmp_path, monkeypatch):
    env = capture_local(["sh", "-c", "true"], tmp_path)
    assert env["capture_scope"] == "executor_only"
    assert env["conda"]["status"] == "unknown"
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("probe", 35)
    monkeypatch.setattr(subprocess, "run", fail)
    assert capture_local(["true"])["capture_status"] == "failed"


def test_each_chain_step_links_its_environment(tmp_path, fake_conda):
    project = Project.init(tmp_path / "project")
    with closing(Database(project.db_path)) as db:
        run = run_external_command(db, project, [], commands=[[*fake_conda, "true"], ["true"]], step="chain")
        steps = json.loads(run["execution_details"])["steps"]
        assert len(steps) == 2
        assert steps[0]["environment_id"] != steps[1]["environment_id"]
        for step in steps:
            assert db.conn.execute("SELECT 1 FROM execution_environments WHERE environment_id=?",
                                   (step["environment_id"],)).fetchone()


def test_missing_timeout_does_not_start_unbounded_probe(tmp_path, monkeypatch):
    import shutil
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "sh").symlink_to(shutil.which("sh"))
    monkeypatch.setenv("PATH", str(tools_dir))
    env = capture_local(["true"])
    assert env["capture_status"] == "unavailable"
    assert "timeout" in env["reason"]


def test_failed_launcher_probe_does_not_fail_payload(tmp_path, fake_conda):
    manager = Path(fake_conda[0])
    manager.write_text(manager.read_text().replace('exec "$@"', '[ "$1" = sh ] && exit 17\nexec "$@"'))
    project = Project.init(tmp_path / "project")
    with closing(Database(project.db_path)) as db:
        run = run_external_command(db, project, [*fake_conda, "true"], step="probe-fails")
        assert run["status"] == "completed"
        env = json.loads(db.conn.execute("SELECT document FROM execution_environments WHERE environment_id=?",
                                         (run["environment_id"],)).fetchone()["document"])
        assert env["capture_status"] == "failed"
        assert env["probe_exit_code"] == 17
        with pytest.raises(ValidationError):
            export_conda(env)


def test_empty_or_truncated_probe_output_is_not_complete(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    assert capture_local(["true"])["capture_status"] == "failed"
    truncated = parse_probe_output("capture_schema=1\npackage\n")
    assert truncated["capture_status"] == "partial"
    assert truncated["conda"]["status"] == "unknown"
    assert probe_command(["micromamba"]) is None
    with pytest.raises(ValidationError):
        export_conda(document(), "unsupported")


def test_direct_ssh_captures_target_and_removes_raw_probe(tmp_path, fake_conda):
    from tests.unit.test_execution import FakeSSHClient
    from operon.execution import SSHExecutor
    project = Project.init(tmp_path / "project")
    client = FakeSSHClient()
    executor = SSHExecutor(project, {"host": "fake.example.org", "scheduler": "none"},
                           SlurmConfig(), client_factory=lambda _: client)
    result = executor.run([*fake_conda, "true"], cwd=project.root,
                          stdout_path=project.logs_root / "out", stderr_path=project.logs_root / "err")
    assert result.exit_code == 0
    env = result.details["environment"]
    assert env["conda_prefix"] == fake_conda[-1]
    assert env["conda"]["package_fingerprint"] == document()["conda"]["package_fingerprint"]
    import re
    probe_paths = re.findall(r"/tmp/operon-env-[0-9a-f]+", "\n".join(client.commands))
    assert probe_paths and all(not Path(path).exists() for path in probe_paths)
    executor.close()


def test_capture_round_trip_redacts_home_and_hostname():
    env = capture_local(["true"])
    assert env["capture_status"] == "complete"
    assert "home" not in env
    assert env["hostname"].startswith("sha256:")
    assert env["system"]["os"] == platform.system()
    assert env["system_fingerprint"] and env["hardware_fingerprint"]
    home = str(Path.home())
    if home != "/":
        assert not any(isinstance(value, str) and home in value for value in env.values())


def test_environment_summary_from_captured_document():
    env = document(extra=encoded("distribution", 'PRETTY_NAME="Debian GNU/Linux 12"\nID=debian\n')
                   + encoded("memory", "1000 kB\n"))
    assert environment_summary(env) == "Debian GNU/Linux 12; 1000 kB; conda (1 packages)"
    partial = document(complete=False)
    assert environment_summary(partial).endswith("capture: partial")


def test_environments_list_includes_summary_column(tmp_path, capsys):
    project = Project.init(tmp_path / "project")
    with closing(Database(project.db_path)) as db:
        with db.transaction():
            environment_id = db.record_environment(document())
    assert main(["--project", str(project.root), "environments", "list"]) == 0
    out = capsys.readouterr().out
    assert "summary" in out
    assert environment_id in out
    assert "conda (1 packages)" in out
