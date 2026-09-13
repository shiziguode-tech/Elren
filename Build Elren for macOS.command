#!/bin/zsh
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$ROOT/macos/build-macos-app.sh"
"$ROOT/macos/build-macos-app.sh"
print
print "Build finished. The macOS ZIP is in the dist folder."
read "?Press Return to close…"
