"""Backup safety and actual round-trip, without deleting any source files."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'qa-artifacts/archive_old_releases_v273.py'
if not SCRIPT.is_file():
    pytest.skip('Owner-only maintenance helper is not shipped in application packages', allow_module_level=True)
spec = importlib.util.spec_from_file_location('release_backup', SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_roundtrip_dedup_preserves_local_modifications(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, 'allowed', lambda path: True)
    archive = tmp_path / 'backup'
    archive.mkdir()
    one, two = tmp_path / 'one', tmp_path / 'two'
    for source in (one, two):
        source.mkdir()
        (source / 'runtime.bin').write_bytes(b'same runtime' * 5000)
        (source / 'empty').mkdir()
        (source / 'local.txt').write_text(source.name)
        backup.snapshot(source, archive)
    index = json.loads((archive / 'content-index.json').read_text())
    assert len(index) == 3
    for manifest in archive.glob('*.manifest.json.gz'):
        backup.validate(manifest)
        dest = tmp_path / manifest.stem
        backup.restore(manifest, dest)
        assert (dest / 'runtime.bin').read_bytes() == b'same runtime' * 5000
        assert (dest / 'local.txt').read_text() in ('one', 'two')
        assert (dest / 'empty').is_dir()
    (one / 'local.txt').write_text('new edit')
    token = backup.hashlib.sha256(str(one.absolute()).encode()).hexdigest()[:16]
    with pytest.raises(ValueError, match='changed'):
        backup.validate(archive / (token + '.manifest.json.gz'))


def test_zip_snapshot_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, 'allowed', lambda path: True)
    archive = tmp_path / 'backup'
    archive.mkdir()
    source = tmp_path / 'package.zip'
    with backup.zipfile.ZipFile(source, 'w') as output:
        output.writestr('package/data/chat.db', b'unique old chat')
        output.writestr('package/custom.py', b'local modification')
    backup.snapshot(source, archive)
    manifest = next(archive.glob('*.manifest.json.gz'))
    backup.validate(manifest)
    backup.restore(manifest, tmp_path / 'restored')
    assert (tmp_path / 'restored/package/data/chat.db').read_bytes() == b'unique old chat'
    with pytest.raises(ValueError, match='must not exist'):
        backup.restore(manifest, tmp_path / 'restored')


@pytest.mark.parametrize('path', [
    'E:/', 'D:/', 'E:/Elren-v1.0-Private-RC-v271',
    'E:/Elren-v1.0-Public-RC-v272.zip', 'E:/Unrelated',
    'E:/nested/Elren-v1.0-Public-RC-v270',
])
def test_keep_current_versions_and_unrelated_paths(path):
    assert not backup.allowed(Path(path))


def test_internal_link_materialization_never_follows_external_target(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, 'allowed', lambda path: True)
    source, archive = tmp_path / 'source', tmp_path / 'archive'
    source.mkdir()
    archive.mkdir()
    target = source / 'target'
    target.mkdir()
    (target / 'plugin.txt').write_text('preserve plugin contents')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sentinel.txt').write_text('must stay untouched')
    try:
        (source / 'alias').symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'Symlink creation unavailable: {error}')
    with pytest.raises(backup.UnsupportedLinkError):
        backup.files(source)
    backup.snapshot(source, archive, materialize_internal=True)
    manifest = next(archive.glob('*.manifest.json.gz'))
    with backup.gzip.open(manifest, 'rt', encoding='utf-8') as stream:
        saved = json.load(stream)
    assert saved['source_size'] == len(b'preserve plugin contents')
    assert saved['materialized_links'][0]['target'] == 'target'
    backup.validate(manifest)
    restored = tmp_path / 'restored'
    backup.restore(manifest, restored)
    assert (restored / 'alias/plugin.txt').read_text() == 'preserve plugin contents'
    assert not (restored / 'alias').is_symlink()
    (source / 'external').symlink_to(outside, target_is_directory=True)
    with pytest.raises(backup.UnsupportedLinkError, match='External'):
        backup.files(source, materialize_internal=True)
    assert (outside / 'sentinel.txt').read_text() == 'must stay untouched'
