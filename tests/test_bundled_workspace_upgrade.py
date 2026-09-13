from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from deepdesk import bundled_workspace as vendor
from deepdesk.control_files import ControlFileGuard, ControlFileStateError
from deepdesk.secret_storage import AesGcmProtector
from macos.workspace_manifest import package_digests, write_manifest


def write(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def package(files, predecessors=()):
    return vendor.VendorPackage(vendor.package_root(next(iter(files))), files,
        {name: 0o644 for name in files}, tuple(predecessors))


def update(*packages):
    digest = hashlib.sha256("".join(p.digest for p in packages).encode()).hexdigest()
    return vendor.VendorUpdate(digest, tuple(packages))


def test_upgrades_pristine_package_without_touching_custom_files_or_rules(tmp_path):
    old = {"skills/openclaw-bundled/demo/SKILL.md": b"old", "skills/openclaw-bundled/demo/old.py": b"old code"}
    for name, data in old.items():
        write(tmp_path, name, data)
    write(tmp_path, "AGENTS.md", b"user instructions")
    write(tmp_path, "plugins/custom.py", b"user code")
    write(tmp_path, "data/conversations.txt", b"user chat")
    guard = ControlFileGuard(tmp_path)
    new = package({"skills/openclaw-bundled/demo/SKILL.md": b"new",
        "skills/openclaw-bundled/demo/new.py": b"new code"}, [vendor.digest_files(old)])
    guard._migrate_vendor_update(update(new))
    assert guard.trusted_bytes("skills/openclaw-bundled/demo/SKILL.md") == b"new"
    assert not (tmp_path / "skills/openclaw-bundled/demo/old.py").exists()
    assert (tmp_path / "skills/openclaw-bundled/demo/new.py").read_bytes() == b"new code"
    assert (tmp_path / "AGENTS.md").read_bytes() == b"user instructions"
    assert (tmp_path / "plugins/custom.py").read_bytes() == b"user code"
    assert (tmp_path / "data/conversations.txt").read_bytes() == b"user chat"
    assert guard.verify_and_restore(reason="qa") == []


@pytest.mark.parametrize("custom", ["modified", "added", "deleted"])
def test_preserves_entire_customized_package(tmp_path, custom):
    old = {"skills/demo/SKILL.md": b"original", "skills/demo/script.py": b"original code"}
    for name, data in old.items():
        write(tmp_path, name, data)
    if custom == "modified":
        write(tmp_path, "skills/demo/SKILL.md", b"user customized")
    elif custom == "added":
        write(tmp_path, "skills/demo/custom.txt", b"user addition")
    else:
        (tmp_path / "skills/demo/script.py").unlink()
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    guard = ControlFileGuard(tmp_path)
    guard._migrate_vendor_update(update(package({"skills/demo/SKILL.md": b"new"}, [vendor.digest_files(old)])))
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_new_package_installs_when_skills_root_already_exists(tmp_path):
    write(tmp_path, "skills/custom/SKILL.md", b"user skill")
    guard = ControlFileGuard(tmp_path)
    guard._migrate_vendor_update(update(package({"skills/new/SKILL.md": b"vendor new"})))
    assert (tmp_path / "skills/new/SKILL.md").read_bytes() == b"vendor new"
    assert (tmp_path / "skills/custom/SKILL.md").read_bytes() == b"user skill"


def test_new_package_preserves_ancestor_file_conflict(tmp_path):
    write(tmp_path, "skills/openclaw-bundled", b"user custom file")
    guard = ControlFileGuard(tmp_path)
    guard._migrate_vendor_update(update(package({"skills/openclaw-bundled/demo/SKILL.md": b"new"})))
    assert (tmp_path / "skills/openclaw-bundled").read_bytes() == b"user custom file"


def test_persistent_upgrade_survives_restart_and_does_not_accept_tampering(tmp_path):
    workspace = tmp_path / "workspace"
    relative = "skills/demo/SKILL.md"
    write(workspace, relative, b"old")
    write(workspace, "AGENTS.md", b"original rules")
    kwargs = {"persistent": True, "state_path": tmp_path / "security/baseline.vault",
        "protector": AesGcmProtector("test-vendor-upgrade", b"k" * 32)}
    first = ControlFileGuard(workspace, **kwargs)
    # Adopt a legacy package matching current vendor bytes into encrypted state.
    first._migrate_vendor_update(update(package({relative: b"old"})))
    first.stop()
    write(workspace, relative, b"tampered")
    write(workspace, "AGENTS.md", b"tampered rules")
    second = ControlFileGuard(workspace, **kwargs)
    assert (workspace / relative).read_bytes() == b"old"
    assert (workspace / "AGENTS.md").read_bytes() == b"original rules"
    # Future vendor version needs no hardcoded predecessor: authenticated
    # package history carries the last installed digest.
    second._migrate_vendor_update(update(package({relative: b"new"})))
    second.stop()
    third = ControlFileGuard(workspace, **kwargs)
    assert (workspace / relative).read_bytes() == b"new"
    assert third._bundled_package_digests["skills/demo"] == vendor.digest_files({relative: b"new"})
    assert third.verify_and_restore(reason="restart") == []
    third.stop()


def test_failed_commit_rolls_back_vendor_files_and_metadata(tmp_path, monkeypatch):
    relative = "skills/demo/SKILL.md"
    write(tmp_path, relative, b"old")
    guard = ControlFileGuard(tmp_path)
    old = package({relative: b"old"})
    guard._migrate_vendor_update(update(old))
    original_restore = guard._restore_locked
    calls = 0
    def fail_after_write(*args, **kwargs):
        nonlocal calls
        original_restore(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("simulated interrupted write")
    monkeypatch.setattr(guard, "_restore_locked", fail_after_write)
    with pytest.raises(OSError, match="interrupted"):
        guard._migrate_vendor_update(update(package({relative: b"new"})))
    assert (tmp_path / relative).read_bytes() == b"old"
    assert guard._bundled_package_digests["skills/demo"] == old.digest
    assert guard.verify_and_restore(reason="rollback") == []


def test_migration_is_not_callable_during_active_model_tool(tmp_path):
    guard = ControlFileGuard(tmp_path)
    guard._active_model_tool_calls = 1
    with pytest.raises(ControlFileStateError, match="cold start"):
        guard._migrate_vendor_update(update(package({"skills/new/SKILL.md": b"new"})))


def make_signed_fixture(tmp_path, monkeypatch):
    executable = write(tmp_path, "Elren.app/Contents/Resources/backend/ElrenBackend", b"frozen")
    bundle = executable.parent.parent / "bundle"
    write(bundle, "skills/demo/SKILL.md", b"trusted vendor")
    history = tmp_path / "history.json"
    history.write_text("{}")
    write_manifest(bundle, history)
    monkeypatch.setattr(vendor.sys, "platform", "darwin")
    monkeypatch.setattr(vendor.sys, "frozen", True, raising=False)
    monkeypatch.setattr(vendor.sys, "executable", str(executable))
    return bundle


def test_packaged_update_requires_signature_and_actual_file_hashes(tmp_path, monkeypatch):
    bundle = make_signed_fixture(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(vendor.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
    result = vendor.packaged_update()
    assert result.packages[0].files == {"skills/demo/SKILL.md": b"trusted vendor"}
    assert calls[0][:4] == ["/usr/bin/codesign", "--verify", "--deep", "--strict"]
    assert vendor.packaged_update(result.manifest_digest) is None
    assert len(calls) == 1
    write(bundle, "skills/demo/SKILL.md", b"changed vendor")
    with pytest.raises(ValueError, match="integrity"):
        vendor.packaged_update()


def test_invalid_signature_cannot_supply_instructions(tmp_path, monkeypatch):
    make_signed_fixture(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "codesign")
    monkeypatch.setattr(vendor.subprocess, "run", fail)
    monkeypatch.setattr(vendor, "_read_verified_bundle", lambda *args: pytest.fail("must not read plugin bytes"))
    with pytest.raises(subprocess.CalledProcessError):
        vendor.packaged_update()


@pytest.mark.parametrize("relative", ["../skills/x", "/skills/x", "AGENTS.md", "data/chat.db",
    "skills/x/../../AGENTS.md", "skills/x:stream", "skills/x./SKILL.md", "skills\\x\\SKILL.md"])
def test_vendor_paths_cannot_update_user_data_or_rules(relative):
    with pytest.raises(ValueError):
        vendor.package_root(relative)


def test_source_runtime_never_accepts_an_environment_update_path(tmp_path, monkeypatch):
    monkeypatch.setattr(vendor.sys, "frozen", False, raising=False)
    monkeypatch.setenv("ELREN_BUNDLED_WORKSPACE", str(tmp_path))
    assert vendor.packaged_update() is None


def test_build_inventory_tracks_packages_and_preserves_case_safety(tmp_path):
    write(tmp_path, "skills/openclaw-bundled/demo/SKILL.md", b"demo")
    write(tmp_path, "skills/openclaw-bundled/demo/scripts/run.py", b"code")
    write(tmp_path, "plugins/custom.py", b"plugin")
    write(tmp_path, "skills/openclaw-bundled/demo/__pycache__/junk.pyc", b"cache")
    assert set(package_digests(tmp_path)) == {"skills/openclaw-bundled/demo", "plugins/custom.py"}


def test_signed_manifest_is_generated_before_outer_signing():
    build = (Path(__file__).parents[1] / "macos/build-macos-app.sh").read_text("utf-8")
    assert build.index("workspace_manifest.py") < build.index("SIGN_ARGS=")


def test_memory_only_guard_does_not_access_user_keychain(tmp_path, monkeypatch):
    def denied():
        pytest.fail("memory-only integrity guard must not access OS credentials")
    monkeypatch.setattr("deepdesk.secret_storage.select_secret_protector", denied)
    guard = ControlFileGuard(tmp_path, persistent=False)
    assert guard.verify_and_restore(reason="offline") == []
    guard.stop()


def test_persistent_guard_still_requires_os_protector(tmp_path, monkeypatch):
    def denied():
        raise RuntimeError("keychain unavailable")
    monkeypatch.setattr("deepdesk.secret_storage.select_secret_protector", denied)
    with pytest.raises(RuntimeError, match="keychain unavailable"):
        ControlFileGuard(tmp_path, persistent=True)


def test_packaged_update_rejects_linked_resource_root(tmp_path, monkeypatch):
    bundle = make_signed_fixture(tmp_path, monkeypatch)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == bundle or original(p))
    monkeypatch.setattr(vendor.subprocess, "run", lambda *a, **kw: pytest.fail("reject before codesign"))
    with pytest.raises(ValueError, match="Linked vendor workspace root"):
        vendor.packaged_update()
