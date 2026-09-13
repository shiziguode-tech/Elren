from types import SimpleNamespace

import pytest

from deepdesk.task_file_links import referenced_task_file


def task(text):
    return SimpleNamespace(model_dump=lambda **kw: {'result': text})

def test_referenced_output_with_spaces(tmp_path):
    p=tmp_path/'outputs'/'中文 & file.html'
    p.parent.mkdir(); p.write_text('ok')
    assert referenced_task_file(task(f'`{p}`'), tmp_path, str(p)) == p

@pytest.mark.parametrize('value', ['../outputs/x.html','//server/outputs/x.html','outputs/key.vault','outputs/a.exe'])
def test_unsafe_paths(tmp_path,value):
    with pytest.raises(PermissionError):
        referenced_task_file(task(value),tmp_path,value)

def test_other_file_is_not_authorized(tmp_path):
    with pytest.raises(PermissionError):
        referenced_task_file(task('outputs/other.html'),tmp_path,'outputs/a.html')
    with pytest.raises(PermissionError):
        referenced_task_file(task('outputs/a.html.bak'),tmp_path,'outputs/a.html')

def test_missing_document(tmp_path):
    with pytest.raises(FileNotFoundError):
        referenced_task_file(task('outputs/missing.html'),tmp_path,'outputs/missing.html')
