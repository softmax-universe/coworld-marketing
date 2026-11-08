"""Bounded normal Docker build and complete installed-package qualification.

The independent CI job deadline and disposable runner bound this CPU-only stage.
No registry push, hosted release, real provider call or GPU is performed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import tomllib
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

INSIDE = r"""
import hashlib, importlib.metadata, json, pathlib, subprocess, sys
root = pathlib.Path('/proof')
expected = json.loads((root / 'source-identities.json').read_text())
for name, digest in expected.items():
    assert hashlib.sha256((pathlib.Path('/app') / name).read_bytes()).hexdigest() == digest, name
subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--find-links', '/proof/wheels',
                '--target', '/tmp/test-deps', 'pytest>=8.3,<9', 'pytest-asyncio>=0.25,<1',
                'jsonschema>=4.23,<5'], check=True, timeout=60)
sys.path.insert(0, '/tmp/test-deps')
import marketing
from marketing.authoring import system_prompt
assert str(pathlib.Path(marketing.__file__).resolve()).startswith('/usr/local/lib/python3.12/site-packages/')
version = json.loads((root / 'version.json').read_text())
assert marketing.__version__ == importlib.metadata.version('coworld-marketing') == version
for name, digest in expected.items():
    if name.startswith('marketing/'):
        installed = pathlib.Path(marketing.__file__).parent / name.removeprefix('marketing/')
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest, name
assert system_prompt() == pathlib.Path('/source/docs/POLICY_GUIDE.md').read_text().split('\n---\n', 1)[1].strip()
(root / 'installed-image.json').write_text(json.dumps({
    'version': version, 'installed_module': marketing.__file__, 'source_files': len(expected),
    'canonical_guide_SHA256': hashlib.sha256(system_prompt().encode()).hexdigest(),
    'network': 'none', 'genuine_targets': 0}, indent=2))
import pytest
status = pytest.main(['-p', 'pytest_asyncio.plugin', '-c', '/source/pyproject.toml', '/source/tests',
                     '--import-mode=importlib', '-o', 'cache_dir=/proof/pytest-cache',
                     '--basetemp', '/proof/pytest-temp', '--junitxml', '/proof/tests.xml'])
assert str(pathlib.Path(marketing.__file__).resolve()).startswith('/usr/local/lib/python3.12/site-packages/')
raise SystemExit(status)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-tests", type=int, default=76)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    stop = started + 900
    suffix = uuid.uuid4().hex[:12]
    builder = "marketing-qualification-" + suffix
    image = "marketing-normal-qualification:" + suffix
    containers = []

    def save(name, value):
        (root / name).write_text(json.dumps(value, indent=2) + "\n")

    def guard():
        free = shutil.disk_usage(root).free
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        with (root / "resources.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "seconds": time.monotonic() - started,
                        "free_disk_bytes": free,
                        "available_RAM_bytes": available,
                        "owned_output_bytes": size,
                    }
                )
                + "\n"
            )
        assert time.monotonic() < stop, "900 second absolute work deadline"
        assert free >= 3 * 2**30 and available >= 2 * 2**30, "Host reserve"
        assert size <= 512 * 2**20, "Owned output ceiling"

    def run(label, command, timeout):
        guard()
        record = {
            "start_UTC": dt.datetime.now(dt.UTC).isoformat(),
            "command": command,
            "expected_seconds": min(timeout, 120),
            "timeout_seconds": timeout,
        }
        save(label + "-command.json", record)
        begin = time.monotonic()
        with (root / (label + ".stdout")).open("xb") as out, (root / (label + ".stderr")).open("xb") as err:
            child = subprocess.Popen(command, cwd=source, stdout=out, stderr=err, start_new_session=True)
            try:
                while child.poll() is None:
                    guard()
                    if time.monotonic() - begin >= timeout:
                        raise TimeoutError(label)
                    time.sleep(1)
                child.wait(timeout=10)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=15)
                record.update(returncode=child.returncode, elapsed_seconds=time.monotonic() - begin)
                save(label + "-duration.json", record)
            if child.returncode:
                raise RuntimeError((label, (root / (label + ".stderr")).read_text()[-2000:]))

    assert shutil.disk_usage(root).free >= 6 * 2**30, "Cold normal build requires 6 GiB"
    source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=source).strip()
    names = subprocess.check_output(["git", "ls-files", "marketing"], cwd=source, text=True).splitlines()
    names += ["pyproject.toml", "README.md", "docs/POLICY_GUIDE.md"]
    identities = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}
    save("source-identities.json", identities)
    save("version.json", tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"])
    save(
        "intent.json",
        {
            "source": source_revision,
            "start_UTC": dt.datetime.now(dt.UTC).isoformat(),
            "maximum_work_seconds": 900,
            "cleanup_seconds": 120,
            "independent_CI_job_timeout_seconds": 1200,
            "builder": builder,
            "maximum_builder_RAM_bytes": 2 * 2**30,
            "CPUs": 2,
            "minimum_cold_start_disk_bytes": 6 * 2**30,
            "minimum_runtime_disk_bytes": 3 * 2**30,
            "image": image,
            "commercial_calls_GPU_hosted_releases": 0,
        },
    )
    (root / "inside.py").write_text(INSIDE)
    (root / "wheels").mkdir()
    (root / "buildkit.toml").write_text('[worker.oci]\n  max-parallelism = 2\n  gc = true\n  maxUsedSpace = "3GB"\n')
    builder_created = False
    try:
        run(
            "builder",
            [
                "docker",
                "buildx",
                "create",
                "--name",
                builder,
                "--driver",
                "docker-container",
                "--buildkitd-config",
                str(root / "buildkit.toml"),
                "--driver-opt",
                "memory=2g",
                "--driver-opt",
                "memory-swap=2g",
                "--driver-opt",
                "cpu-period=100000",
                "--driver-opt",
                "cpu-quota=200000",
            ],
            30,
        )
        builder_created = True
        run(
            "normal-build",
            [
                "docker",
                "buildx",
                "build",
                "--builder",
                builder,
                "--platform",
                "linux/amd64",
                "--load",
                "--progress",
                "plain",
                "--tag",
                image,
                "--file",
                "Dockerfile",
                ".",
            ],
            480,
        )
        inspection = json.loads(subprocess.check_output(["docker", "image", "inspect", image], timeout=15))[0]
        save("image-inspect.json", inspection)
        save(
            "NORMAL-IMAGE-BUILD-PASS.json",
            {
                "source": source_revision,
                "image": image,
                "image_id": inspection["Id"],
                "Dockerfile_SHA256": hashlib.sha256((source / "Dockerfile").read_bytes()).hexdigest(),
                "new_hosted_model_GPU_optimizer": 0,
            },
        )
        for phase in ("download", "offline-suite"):
            name = builder + "-" + phase
            command = [
                "docker",
                "create",
                "--name",
                name,
                "--label",
                "marketing.qualification=" + suffix,
                "--cpus",
                "2",
                "--memory",
                "1280m",
                "--memory-swap",
                "1280m",
                "--workdir",
                "/tmp",
                "--mount",
                f"type=bind,src={source},dst=/source,readonly",
                "--mount",
                f"type=bind,src={root},dst=/proof",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "OPENBLAS_NUM_THREADS=2",
            ]
            if phase == "offline-suite":
                command += ["--network", "none"]
            command += [image, "timeout", "--signal=TERM", "330", "python"]
            if phase == "download":
                command += [
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--dest",
                    "/proof/wheels",
                    "pytest>=8.3,<9",
                    "pytest-asyncio>=0.25,<1",
                    "jsonschema>=4.23,<5",
                ]
            else:
                command += ["/proof/inside.py"]
            run(phase + "-create", command, 20)
            containers.append(name)
            run(phase, ["docker", "start", "--attach", name], 350)
            state = json.loads(subprocess.check_output(["docker", "inspect", name], timeout=15))[0]
            save(phase + "-inspection.json", state)
            assert state["Config"]["Labels"]["marketing.qualification"] == suffix
            assert not state["State"]["Running"] and state["State"]["ExitCode"] == 0
            assert not state["State"]["OOMKilled"]
        cases = list(ET.parse(root / "tests.xml").iter("testcase"))
        assert len(cases) == args.expected_tests
        assert all(c.find("failure") is None and c.find("error") is None for c in cases)
        run(
            "image-export",
            [
                "bash",
                "-c",
                'set -o pipefail; docker image save "$1" | gzip -1 > "$2"',
                "image-export",
                image,
                str(root / "normal-image.tar.gz"),
            ],
            120,
        )
        assert (root / "normal-image.tar.gz").stat().st_size <= 256 * 2**20
        save(
            "summary.json",
            {
                "source": source_revision,
                "image_id": inspection["Id"],
                "cases": len(cases),
                "failed": 0,
                "elapsed_seconds": time.monotonic() - started,
                "installed_module_verified_before_and_after_suite": True,
                "commercial_calls_GPU_optimizer": 0,
                "teacher_qualification_and_scientific_acceptance": False,
            },
        )
    finally:
        for name in containers:
            data = json.loads(subprocess.check_output(["docker", "inspect", name], timeout=15))[0]
            assert data["Config"]["Labels"]["marketing.qualification"] == suffix
            subprocess.run(["docker", "stop", "--time", "10", data["Id"]], capture_output=True, timeout=20, check=True)
            subprocess.run(["docker", "rm", data["Id"]], capture_output=True, timeout=20, check=True)
        if builder_created:
            subprocess.run(["docker", "buildx", "rm", builder], capture_output=True, timeout=30, check=True)
        save(
            "cleanup.json",
            {
                "exact_owned_containers_removed": containers,
                "builder_removed": builder_created,
                "elapsed_seconds": time.monotonic() - started,
            },
        )
        files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        with (root / "SHA256SUMS").open("x") as stream:
            for path in files:
                with path.open("rb") as reader:
                    digest = hashlib.file_digest(reader, "sha256").hexdigest()
                stream.write(digest + "  " + str(path.relative_to(root)) + "\n")


if __name__ == "__main__":
    main()
