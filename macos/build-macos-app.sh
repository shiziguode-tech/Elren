#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${ELREN_BUILD_DIR:-$ROOT/build/macos}"
APP="$BUILD/Elren.app"
CONTENTS="$APP/Contents"
RESOURCES="$CONTENTS/Resources"
ARCH="$(uname -m)"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"
OUTPUT="${ELREN_OUTPUT:-$ROOT/dist/Elren-v1.0-macOS-$ARCH.zip}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"
IDENTITY="${ELREN_CODESIGN_IDENTITY:-${MILO_CODESIGN_IDENTITY:--}}"
PYTHON_LOCK="$ROOT/macos/build-requirements.lock"
RUNTIME_PACKAGE="$ROOT/macos/runtime-package.json"
RUNTIME_LOCK="$ROOT/macos/runtime-package-lock.json"
LILYPOND_VERSION="2.26.0"
LILYPOND_ARCHIVE="$BUILD/lilypond-$LILYPOND_VERSION-darwin-arm64.tar.gz"
LILYPOND_URL="https://gitlab.com/lilypond/lilypond/-/releases/v$LILYPOND_VERSION/downloads/lilypond-$LILYPOND_VERSION-darwin-arm64.tar.gz"
LILYPOND_SHA256="18ffc454fef3753c26a015d95a3c232f89b22f052b897c046e0198740a1221be"
AUDIVERIS_SOURCE="${ELREN_AUDIVERIS_SOURCE:-/Applications/Audiveris.app/Contents}"
AUDIVERIS_HOME="$RESOURCES/bundle/work/tool-runtime/native/audiveris"
NATIVE_ROOT="$RESOURCES/bundle/work/tool-runtime/native"
NODE_ROOT="$NATIVE_ROOT/node"
PORTABLE_PYTHON_ARCHIVE="$BUILD/cpython-3.12.14-macos-arm64.tar.gz"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "This script must run on macOS because Swift, Mach-O code signing, and notarization are Apple tools."
  exit 2
fi
if [[ "$ARCH" != "arm64" ]]; then
  print -u2 "The locked macOS runtime requires an Apple silicon (arm64) build host."
  exit 3
fi
if ! xcrun --find swiftc >/dev/null 2>&1; then
  print -u2 "Install Xcode Command Line Tools first: xcode-select --install"
  exit 4
fi
if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3,12))'; then
  print -u2 "Python 3.12 is required to reproduce the locked bundled backend."
  exit 5
fi
for LOCKED_INPUT in "$PYTHON_LOCK" "$RUNTIME_PACKAGE" "$RUNTIME_LOCK"; do
  if [[ ! -f "$LOCKED_INPUT" ]]; then
    print -u2 "Missing locked build input: $LOCKED_INPUT"
    exit 6
  fi
done

# Full releases must carry Node/OpenClaw; never silently produce a package
# that works only when the end user has installed a developer runtime.
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1 ||
  ! node -e 'const [a,b,c]=process.versions.node.split(".").map(Number); const ok=(a===22&&(b>22||(b===22&&c>=3)))||(a===24&&(b>15||(b===15&&c>=0)))||(a>25||(a===25&&(b>9||(b===9&&c>=0)))); process.exit(ok?0:1)'; then
  print -u2 "A compatible Node.js and npm are required on the build host to bundle OpenClaw (22.22.3+, 24.15.0+, or 25.9.0+)."
  exit 9
fi
NPM_SOURCE="$("$PYTHON_BIN" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parents[1])' "$(command -v npm)")"
NODE_PREFIX="$(cd "$(dirname "$(command -v node)")/.." && pwd)"
if [[ ! -f "$NPM_SOURCE/bin/npm-cli.js" || ! -f "$NODE_PREFIX/LICENSE" ]]; then
  print -u2 "Use an official Node distribution containing npm and its LICENSE."
  exit 10
fi

# A complete music runtime is required by default. Validate the reviewed local
# Audiveris app before creating build output or downloading other dependencies.
"$PYTHON_BIN" "$ROOT/macos/bundle_audiveris.py" "$AUDIVERIS_SOURCE" \
  --check-only --forbid-root "$BUILD"
if [[ -L "$ROOT/build" || -L "$BUILD" || "${BUILD:h}" != "$ROOT/build" || "${BUILD:t}" == "." || "${BUILD:t}" == ".." ]]; then
  print -u2 "Refusing a redirected or unexpected macOS build directory."
  exit 8
fi
if [[ -e "$BUILD" || -e "$OUTPUT" ]]; then
  print -u2 "Refusing to overwrite a previous build or archive. Choose a fresh ELREN_BUILD_DIR directly inside build and a fresh ELREN_OUTPUT."
  exit 11
fi
mkdir -p "$CONTENTS/MacOS" "$RESOURCES/backend" "$NODE_ROOT" "$ROOT/dist"
"$PYTHON_BIN" "$ROOT/macos/bundle_cli.py" "${ELREN_CLI_CACHE:-$BUILD/cli-downloads}" "$NATIVE_ROOT"
"$PYTHON_BIN" "$ROOT/macos/bundle_git.py" "${ELREN_GIT_CACHE:-$BUILD/git-downloads}" "$NATIVE_ROOT"
cp "$ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
mkdir -p "$RESOURCES/bundle"
cp -R "$ROOT/skills" "$ROOT/plugins" "$RESOURCES/bundle/"
cp "$ROOT/AGENTS.md" "$RESOURCES/bundle/AGENTS.md"
for NOTICE in LICENSE THIRD_PARTY_NOTICES.md ACKNOWLEDGEMENTS.md LICENSE_PENDING.md; do
  cp "$ROOT/$NOTICE" "$RESOURCES/bundle/$NOTICE"
done
PYTHON_BIN="$PYTHON_BIN" /bin/zsh "$ROOT/launcher/install-jianpu-runtime-macos.sh" \
  "$AUDIVERIS_SOURCE" "$RESOURCES/bundle"
mkdir -p "$RESOURCES/bundle/work/tool-runtime/native/lilypond"
mkdir -p "$RESOURCES/bundle/work/tool-runtime/native/jianpu-ly"
if [[ -n "${ELREN_LILYPOND_ARCHIVE:-}" ]]; then
  cp "$ELREN_LILYPOND_ARCHIVE" "$LILYPOND_ARCHIVE"
else
  curl --fail --location --retry 3 "$LILYPOND_URL" --output "$LILYPOND_ARCHIVE"
fi
print "$LILYPOND_SHA256  $LILYPOND_ARCHIVE" | shasum -a 256 -c -
tar -xzf "$LILYPOND_ARCHIVE" -C "$RESOURCES/bundle/work/tool-runtime/native/lilypond"
cp "$ROOT/macos/third-party/lilypond/COPYING" "$RESOURCES/bundle/work/tool-runtime/native/lilypond/COPYING"
cp "$ROOT/macos/third-party/lilypond/UPSTREAM.json" "$RESOURCES/bundle/work/tool-runtime/native/lilypond/UPSTREAM.json"
cp "$ROOT/deepdesk/vendor/jianpu_ly/__init__.py" "$RESOURCES/bundle/work/tool-runtime/native/jianpu-ly/jianpu_ly.py"
cp "$ROOT/deepdesk/vendor/jianpu_ly/LICENSE" "$RESOURCES/bundle/work/tool-runtime/native/jianpu-ly/LICENSE"
cp "$ROOT/deepdesk/vendor/jianpu_ly/UPSTREAM.json" "$RESOURCES/bundle/work/tool-runtime/native/jianpu-ly/UPSTREAM.json"
# Generated caches are not product capabilities and may contain builder paths.
find "$RESOURCES/bundle" -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .kotlin -o -name .gradle -o -name .pnpm \) -prune -exec rm -rf {} +
find "$RESOURCES/bundle" -type f \( -name '*.pyc' -o -name local.properties \) -delete

"$PYTHON_BIN" -m venv "$BUILD/build-venv"
source "$BUILD/build-venv/bin/activate"
python -m pip install --disable-pip-version-check \
  "pip==26.2.1" "setuptools==84.0.0" "wheel==0.48.0"
python -m pip install --disable-pip-version-check \
  --build-constraint "$PYTHON_LOCK" --requirement "$PYTHON_LOCK"
python -m pip check
if [[ -n "${ELREN_OCR_MODEL_CACHE:-}" ]]; then
  python "$ROOT/macos/provision_ocr_models.py" --install-from "$ELREN_OCR_MODEL_CACHE"
fi
python "$ROOT/macos/check_ocr_models.py"
if [[ -n "${ELREN_PYTHON_ARCHIVE:-}" ]]; then
  cp "$ELREN_PYTHON_ARCHIVE" "$PORTABLE_PYTHON_ARCHIVE"
else
  PYTHON_URL="$(PYTHONPATH="$ROOT" python -c 'from macos.bundle_python import URL; print(URL)')"
  curl --fail --location --retry 3 "$PYTHON_URL" --output "$PORTABLE_PYTHON_ARCHIVE"
fi
python "$ROOT/macos/bundle_python.py" "$PORTABLE_PYTHON_ARCHIVE" "$NATIVE_ROOT"
# General task scripts need an actual interpreter, not the frozen backend entry.
# Install at build time only; startup never runs pip or requires user tools.
"$NATIVE_ROOT/python/bin/python3" -m pip install --disable-pip-version-check \
  --build-constraint "$PYTHON_LOCK" --requirement "$PYTHON_LOCK"
"$NATIVE_ROOT/python/bin/python3" -m pip check
if [[ -n "${ELREN_OCR_MODEL_CACHE:-}" ]]; then
  "$NATIVE_ROOT/python/bin/python3" "$ROOT/macos/provision_ocr_models.py" --install-from "$ELREN_OCR_MODEL_CACHE"
fi
export PLAYWRIGHT_BROWSERS_PATH="$RESOURCES/bundle/work/browser-runtime"
export PLAYWRIGHT_SKIP_BROWSER_GC=1
python -m playwright install --only-shell chromium
python -m PyInstaller --noconfirm --clean --onedir --name ElrenBackend \
  --paths "$ROOT" \
  --collect-all edge_tts --collect-all rapidocr --collect-all onnxruntime \
  --collect-all lark_oapi --collect-all gradio_client \
  --add-data "$ROOT/deepdesk/static:deepdesk/static" \
  --add-data "$ROOT/deepdesk/audiveris_tessdata.lock.json:deepdesk" \
  --add-data "$ROOT/deepdesk/subagent_catalog.json:deepdesk" \
  --add-data "$ROOT/deepdesk/java:deepdesk/java" \
  --collect-submodules deepdesk.vendor.jianpu_ly \
  --distpath "$BUILD/pyinstaller" --workpath "$BUILD/pyinstaller-work" \
  --specpath "$BUILD" "$ROOT/macos/backend_entry.py"
cp -R "$BUILD/pyinstaller/ElrenBackend/." "$RESOURCES/backend/"
python "$ROOT/macos/check_ocr_models.py" --backend "$RESOURCES/backend"
python "$ROOT/macos/check_frozen_resources.py" "$RESOURCES/backend"
python "$ROOT/macos/workspace_manifest.py" "$RESOURCES/bundle" "$ROOT/macos/workspace-history.json"

xcrun swiftc -swift-version 5 -O -target "${ARCH}-apple-macos${MACOSX_DEPLOYMENT_TARGET}" \
  -framework AppKit -framework WebKit \
  "$ROOT/macos/ElrenMac.swift" -o "$CONTENTS/MacOS/Elren"

ICON_SOURCE="$ROOT/launcher/elren-app-icon.png"
if [[ -f "$ICON_SOURCE" ]]; then
  ICONSET="$BUILD/Elren.iconset"
  mkdir -p "$ICONSET"
  for SIZE in 16 32 128 256 512; do
    sips -z "$SIZE" "$SIZE" "$ICON_SOURCE" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" >/dev/null
    DOUBLE=$((SIZE * 2))
    sips -z "$DOUBLE" "$DOUBLE" "$ICON_SOURCE" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$RESOURCES/Elren.icns"
fi

NODE_COMPATIBLE=false
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  if node -e 'const [a,b,c]=process.versions.node.split(".").map(Number); const ok=(a===22&&(b>22||(b===22&&c>=3)))||(a===24&&(b>15||(b===15&&c>=0)))||(a>25||(a===25&&(b>9||(b===9&&c>=0)))); process.exit(ok?0:1)'; then
    NODE_COMPATIBLE=true
  else
    print -u2 "Ignoring Node.js $(node --version): supported branches are 22.22.3+, 24.15.0+, or 25.9.0+."
  fi
fi
if [[ "$NODE_COMPATIBLE" == true ]]; then
  mkdir -p "$NODE_ROOT/bin" "$NODE_ROOT/lib/node_modules"
  cp "$(command -v node)" "$NODE_ROOT/bin/node"
  # npm root -g follows the user's global prefix, not the selected CLI install.
  NPM_SOURCE="$(python -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parents[1])' "$(command -v npm)")"
  NODE_PREFIX="$(cd "$(dirname "$(command -v node)")/.." && pwd)"
  if [[ ! -f "$NPM_SOURCE/bin/npm-cli.js" || ! -f "$NODE_PREFIX/LICENSE" ]]; then
    print -u2 "Use an official Node distribution containing npm and its LICENSE."
    exit 10
  fi
  cp -R "$NPM_SOURCE" "$NODE_ROOT/lib/node_modules/npm"
  cp "$NODE_PREFIX/LICENSE" "$NODE_ROOT/LICENSE"
  ln -s ../lib/node_modules/npm/bin/npm-cli.js "$NODE_ROOT/bin/npm"
  ln -s ../lib/node_modules/npm/bin/npx-cli.js "$NODE_ROOT/bin/npx"
  cp "$RUNTIME_PACKAGE" "$NODE_ROOT/package.json"
  cp "$RUNTIME_LOCK" "$NODE_ROOT/package-lock.json"
  (cd "$NODE_ROOT" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm audit --omit=dev --audit-level=high)
  mkdir -p "$NODE_ROOT/mcp-servers"
  cp "$ROOT/work/openclaw-runtime/mcp-servers/elren-workspace.mjs" "$NODE_ROOT/mcp-servers/"
  if [[ -n "$(find "$NODE_ROOT/node_modules" -type d -name .pnpm -print -quit)" ]]; then
    print -u2 "Refusing to package a pnpm virtual store in the macOS runtime."
    exit 7
  fi
  cat > "$NODE_ROOT/openclaw" <<'WRAPPER'
#!/bin/zsh
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/bin/node" "$HERE/node_modules/openclaw/openclaw.mjs" "$@"
WRAPPER
  chmod 755 "$NODE_ROOT/openclaw" "$NODE_ROOT/bin/node"
else
  print -u2 "The required build-time Node.js runtime became unavailable; refusing an incomplete release."
  exit 9
fi

SIGN_ARGS=(--force --options runtime --sign "$IDENTITY")
if [[ "$IDENTITY" != "-" ]]; then SIGN_ARGS+=(--timestamp); fi
# Sign Mach-O libraries as well as executables (the private JVM contains both).
# Scripts and jars are resources, not individually signable Mach-O programs.
find "$APP" -type f -print0 | while IFS= read -r -d '' executable; do
  if /usr/bin/file -b "$executable" | /usr/bin/grep -q 'Mach-O'; then
    if [[ "$executable" == "$AUDIVERIS_HOME/runtime/bin/java" ]]; then
      codesign "${SIGN_ARGS[@]}" --entitlements "$ROOT/macos/Audiveris.entitlements" "$executable"
    elif [[ "${executable:t}" == "ElrenBackend" || "${executable:t}" == "python3.12" || "${executable:t}" == "git-credential-manager" ]]; then
      # Frozen Python loads its private framework and extension modules. Ad-hoc
      # signatures have no Team ID, so default library validation rejects them.
      # Git Credential Manager likewise loads its bundled .NET JIT/runtime.
      codesign "${SIGN_ARGS[@]}" --entitlements "$ROOT/macos/Backend.entitlements" "$executable"
    elif [[ "${executable:t}" == "node" ]]; then
      # Both OpenClaw and Playwright ship Node. Hardened re-signing without
      # JIT entitlements makes V8 abort before executing even a trivial script.
      codesign "${SIGN_ARGS[@]}" --entitlements "$ROOT/macos/Elren.entitlements" "$executable"
    elif [[ "${executable:t}" == "chrome-headless-shell" ]]; then
      codesign "${SIGN_ARGS[@]}" --entitlements "$ROOT/macos/Chromium.entitlements" "$executable"
    elif [[ "${executable:t}" == "lilypond" ]]; then
      # LilyPond 2.26 embeds Guile 3, whose JIT also needs executable memory.
      codesign "${SIGN_ARGS[@]}" --entitlements "$ROOT/macos/Elren.entitlements" "$executable"
    else
      codesign "${SIGN_ARGS[@]}" "$executable"
    fi
  fi
done
find "$APP" -depth -type d \( -name '*.framework' -o -name '*.app' -o -name '*.xpc' \) ! -path "$APP" -print0 | while IFS= read -r -d '' nested_bundle; do
  codesign "${SIGN_ARGS[@]}" "$nested_bundle"
done
# Signing modifies native bytes; hash the final signed payload before sealing
# the enclosing .app resources so integrity checks do not start out stale.
"$PYTHON_BIN" "$ROOT/macos/bundle_audiveris.py" "$AUDIVERIS_HOME" \
  --verify-installed --refresh-manifest
"$PYTHON_BIN" "$ROOT/macos/runtime_manifest.py" "$RESOURCES/bundle"
if [[ "$IDENTITY" == "-" ]]; then
  codesign --force --options runtime --entitlements "$ROOT/macos/Desktop.entitlements" --sign - "$APP"
else
  codesign --force --options runtime --timestamp --entitlements "$ROOT/macos/Desktop.entitlements" --sign "$IDENTITY" "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"
"$PYTHON_BIN" "$ROOT/macos/bundle_audiveris.py" "$AUDIVERIS_HOME" --verify-installed
spctl --assess --type execute --verbose=2 "$APP" || [[ "$IDENTITY" == "-" ]]

rm -f "$OUTPUT"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUTPUT"
shasum -a 256 "$OUTPUT" > "$OUTPUT.sha256"

NOTARY_PROFILE="${ELREN_NOTARY_PROFILE:-${MILO_NOTARY_PROFILE:-}}"
if [[ -n "$NOTARY_PROFILE" && "$IDENTITY" != "-" ]]; then
  xcrun notarytool submit "$OUTPUT" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  rm -f "$OUTPUT"
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUTPUT"
  shasum -a 256 "$OUTPUT" > "$OUTPUT.sha256"
fi

print "Built: $OUTPUT"
