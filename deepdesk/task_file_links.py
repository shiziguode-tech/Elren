"""Resolve explicitly referenced output documents without exposing arbitrary files."""
from pathlib import Path
import re

DOCUMENT_EXTENSIONS = {'.html', '.htm', '.pdf', '.txt', '.md', '.svg', '.png', '.jpg', '.jpeg',
                       '.webp', '.docx', '.pptx', '.xlsx', '.csv', '.json', '.mid', '.midi', '.musicxml'}

def referenced_task_file(task, workspace: Path, value: str) -> Path:
    normalized = value.replace('\\', '/')
    if len(normalized) > 4096 or '\x00' in normalized or normalized.startswith('//'):
        raise PermissionError('Invalid output path')
    parts = normalized.split('/')
    if '..' in parts or 'outputs' not in parts or Path(normalized).suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise PermissionError('Not an output document')
    # Only a complete path in saved task content grants access, not a filename
    # guessed by a caller or a substring inside another path.
    pattern = re.compile(r'(?<![\w/.:])' + re.escape(normalized) + r'(?![\w/.-])')
    def contains(value):
        if isinstance(value, str):
            return bool(pattern.search(value.replace('\\', '/')))
        if isinstance(value, dict):
            return any(contains(v) for v in value.values())
        if isinstance(value, list):
            return any(contains(v) for v in value)
        return False
    if not contains(task.model_dump(mode='json')):
        raise PermissionError('File not referenced by this task')
    path = Path(normalized)
    if not path.is_absolute():
        path = workspace / path
    # Reject links at every level rather than following output links elsewhere.
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (path, *path.parents)):
        raise PermissionError('Linked output path')
    if not path.is_file():
        raise FileNotFoundError('Output file no longer exists')
    return path
