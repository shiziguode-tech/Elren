#!/bin/zsh
set -euo pipefail

script_dir="${0:A:h}"
project_root="${script_dir:h}"
source_root="${1:-/Applications/Audiveris.app/Contents}"
workspace_root="${2:-$project_root}"
python_bin="${PYTHON_BIN:-python3}"

# Shared offline validator also supports the normal macOS jpackage layout
# runtime/Contents/Home and normalizes it to runtime/bin/java for the backend.
# No downloads, no replacement of existing installations, no host Java fallback.
exec "$python_bin" "$project_root/macos/bundle_audiveris.py" "$source_root" \
  --workspace "$workspace_root"
