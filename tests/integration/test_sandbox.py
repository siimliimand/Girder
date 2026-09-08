"""WP-1.5 — sandbox integration tests (Phase 0 exit criteria).

Requires a container runtime (podman or docker) and the ``alpine:3.20`` image;
skipped otherwise. These verify the mechanical properties the whole system's
trust model rests on:

* an unprivileged container runs our mock script,
* it cannot see the host filesystem,
* it cannot write the read-only test bind **even as in-guest root**, nor
  remount it writable (CAP_SYS_ADMIN dropped),
* it sees the shared package-cache mount,
* the orchestrator-side timeout kills it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from girder.sandbox.engine import ContainerSpec, SandboxTimeout
from girder.sandbox.podman import PodmanEngine, detect_runtime

pytestmark = pytest.mark.integration

try:
    RUNTIME = detect_runtime()
    ENGINE = PodmanEngine(RUNTIME)
except RuntimeError:  # pragma: no cover - CI without a runtime
    pytest.skip("no container runtime available", allow_module_level=True)

IMAGE = "alpine:3.20"
COUNTER = 0


async def _start_container(tmp_path: Path, **spec_kwargs: object) -> str:
    global COUNTER
    COUNTER += 1
    spec = ContainerSpec(
        name=f"girder-test-{COUNTER}-{int(time.time())}",
        image=IMAGE,
        **spec_kwargs,  # type: ignore[arg-type]
    )
    await ENGINE.start(spec)
    return spec.name


@pytest.fixture
async def sandbox(tmp_path: Path):  # type: ignore[no-untyped-def]
    name = await _start_container(tmp_path)
    yield name
    await ENGINE.kill(name)


async def test_mock_script_runs(sandbox: str) -> None:
    result = await ENGINE.exec(sandbox, ["echo", "hello-from-mock"])
    assert result.exit_code == 0
    assert "hello-from-mock" in result.stdout


async def test_container_cannot_see_host_filesystem(tmp_path: Path) -> None:
    marker = Path("/tmp") / f"girder-host-marker-{COUNTER}-{int(time.time() * 1000) % 100000}"
    marker.write_text("host-secret")
    try:
        name = await _start_container(tmp_path)
        try:
            result = await ENGINE.exec(name, ["cat", str(marker)])
            assert result.exit_code != 0  # path does not exist inside the guest
            assert "host-secret" not in result.stdout
        finally:
            await ENGINE.kill(name)
    finally:
        marker.unlink()


async def test_no_host_env_secrets_propagated(tmp_path: Path) -> None:
    name = await _start_container(tmp_path)
    try:
        result = await ENGINE.exec(name, ["sh", "-c", "env | grep -ci github_token || true"])
        # grep -c prints 0 matches as "0"; a propagated var would print >= 1
        assert result.stdout.strip().splitlines()[-1] == "0"
    finally:
        await ENGINE.kill(name)


async def test_readonly_test_mount_blocks_writes(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_real.py").write_text("def test_x(): pass")
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("print('app')")

    # Pristine snapshot materialized by the "orchestrator" (host side).
    snapshot = tmp_path / "snapshot-tests"
    snapshot.mkdir()
    (snapshot / "test_real.py").write_text("def test_x(): pass")

    name = await _start_container(tmp_path, worktree=worktree, test_snapshot=(snapshot, "tests"))
    try:
        # sanity: worktree itself is writable
        w = await ENGINE.exec(name, ["sh", "-c", "echo x > /workspace/src/app2.py"])
        assert w.exit_code == 0, w.stderr

        # writing INTO the shadowed test dir fails (EROFS) — as guest root
        r = await ENGINE.exec(name, ["touch", "/workspace/tests/evil.py"])
        assert r.exit_code != 0
        assert "Read-only" in r.stderr or "read-only" in r.stderr

        # modifying an existing test file also fails
        r = await ENGINE.exec(
            name, ["sh", "-c", "echo 'assert False' >> /workspace/tests/test_real.py"]
        )
        assert r.exit_code != 0

        # deleting/renaming blocked too
        r = await ENGINE.exec(name, ["rm", "/workspace/tests/test_real.py"])
        assert r.exit_code != 0

        # the snapshot content is visible (tests are readable)
        r = await ENGINE.exec(name, ["cat", "/workspace/tests/test_real.py"])
        assert r.exit_code == 0 and "def test_x" in r.stdout
    finally:
        await ENGINE.kill(name)


async def test_readonly_mount_cannot_be_remounted(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    name = await _start_container(tmp_path, worktree=worktree, test_snapshot=(snapshot, "tests"))
    try:
        # explicitly as in-guest root (uid 0): remount requires CAP_SYS_ADMIN,
        # which the container dropped at start — per-exec user cannot regain it
        r = await ENGINE.exec(
            name,
            ["sh", "-c", "mount -o remount,rw /workspace/tests 2>&1; echo rc=$?"],
            timeout_s=15,
            user="0",
        )
        assert "rc=0" not in r.stdout
        # after the failed remount, writes must still fail — even as root
        r = await ENGINE.exec(name, ["touch", "/workspace/tests/x"], user="0")
        assert r.exit_code != 0
        # and mounting anything fresh is equally impossible
        r = await ENGINE.exec(
            name, ["sh", "-c", "mount -t tmpfs none /mnt 2>&1; echo rc=$?"], user="0"
        )
        assert "rc=0" not in r.stdout
    finally:
        await ENGINE.kill(name)


async def test_shared_package_cache_visible(tmp_path: Path) -> None:
    cache = tmp_path / "pip-cache"
    cache.mkdir()
    (cache / "MARKER").write_text("cache-warm")

    # Cache mounts live at traversable paths (not /root, which the non-root
    # agent user cannot cross with CAP_DAC_OVERRIDE dropped). The runner
    # image sets pip/npm/cargo cache dirs accordingly via env/config.
    name = await _start_container(tmp_path, ro_mounts={str(cache): "/cache/pip"})
    try:
        r = await ENGINE.exec(name, ["cat", "/cache/pip/MARKER"])
        assert r.exit_code == 0
        assert "cache-warm" in r.stdout
        # read-only: cannot poison the shared cache from inside
        r = await ENGINE.exec(name, ["touch", "/cache/pip/EVIL"])
        assert r.exit_code != 0
    finally:
        await ENGINE.kill(name)


async def test_timeout_kills_container(tmp_path: Path) -> None:
    name = await _start_container(tmp_path)
    start = time.monotonic()
    with pytest.raises(SandboxTimeout):
        await ENGINE.exec(name, ["sleep", "60"], timeout_s=5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 20, f"timeout enforcement too slow: {elapsed:.1f}s"
    await asyncio.sleep(1)  # give the runtime a moment to reap --rm
    assert await ENGINE.exists(name) is False


async def test_missing_worktree_rejected(tmp_path: Path) -> None:
    spec = ContainerSpec(name="girder-test-missing", image=IMAGE, worktree=tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        await ENGINE.start(spec)
