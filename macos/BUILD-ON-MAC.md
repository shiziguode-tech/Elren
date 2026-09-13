# Build Elren for macOS

This source package does not contain a renamed Windows executable. It builds a native Mach-O AppKit application on a Mac.

Requirements:

- An Apple silicon Mac running macOS 14 or newer.
- Xcode Command Line Tools (`xcode-select --install`).
- Python 3.12.
- Node.js 22.22.3+, 24.15.0+, or 25.9.0+ on the build host; bundling OpenClaw is mandatory. End users do not need to install Node.js.
  Use the official distribution with npm and LICENSE present.
- A reviewed local **Audiveris 5.11.0 macOS arm64** app with its private Java
  runtime and classifier model. The Windows payload cannot be reused.

Music recognition is required in the default build; missing or mismatched
Audiveris/Java/model/license files stop the build before downloads or cleanup.
The build does not download Audiveris or substitute a system Java installation.
It checks `/Applications/Audiveris.app/Contents` by default. To use a reviewed
local copy elsewhere, set this before running the build:

```zsh
export ELREN_AUDIVERIS_SOURCE="/path/to/reviewed/Audiveris.app/Contents"
```

The source must contain `app/audiveris.jar`, `app/Audiveris.cfg`, macOS arm64
Leptonica/Tesseract dependencies, and Java under `runtime/Contents/Home` or
`runtime`. Version metadata, Java compatibility/architecture, the pinned
classifier SHA-256, and the Audiveris license are validated. Java's legal notices
are preserved. External filesystem links and directory links are rejected;
internal file links are copied as regular files. Existing installations are
never overwritten by the installer.

The completed app contains Audiveris, its private Java and local recognition
model alongside LilyPond and jianpu-ly in
`Contents/Resources/bundle/work/tool-runtime/native`. The launcher passes these
exact in-bundle paths to the backend; a separate Audiveris installation is not
needed on the user's Mac. `elren-integrity.json` inventories the final signed OMR
payload. This local tool runtime is called **Docker Lite** in Elren; it is not a
Docker container. This source archive is not a built or Mac-validated app:
compilation, signing, real recognition and preview must still be tested on a Mac.

General task commands also receive package-first Python, Node, npm, npx and
OpenClaw. CPython 3.12.14 is the pinned Astral python-build-standalone macOS arm64
distribution, verified by SHA-256 before extraction. The locked dependencies are
installed during the build, never at application startup. To reuse a downloaded
archive, set `ELREN_PYTHON_ARCHIVE` to its absolute path. Licenses are retained.
The signed runtime stays inside the app while conversations/settings live in
Application Support. Runtime status checks the app payload, not system tools.
The build also bundles official arm64 jq 1.8.2, ripgrep 15.2.0 and GitHub CLI
2.97.0 with upstream licenses and pinned SHA-256 verification. Set
`ELREN_CLI_CACHE` to a previously downloaded asset directory to reuse verified
downloads. These tools live under `native/cli/bin`, are code-signed with the app,
and do not require Homebrew or an end-user installer. GitHub operations still
require the user's account where applicable.
Git is separately bundled from the pinned arm64 desktop/dugite-native release:
Git 2.53.0, Git LFS 3.7.1 and Git Credential Manager 2.9.0. Set
`ELREN_GIT_CACHE` to reuse its verified archive and license downloads. The
package-relative `native/git/bin/git` launcher selects bundled helpers and
templates, so no Xcode Command Line Tools or Homebrew Git is needed. Signing
includes Git's native helpers and the credential manager's private .NET runtime.
Local isolated-HOME commit/clone tests do not substitute for authenticated remote
workflow tests. Review `THIRD_PARTY_NOTICES.md` and corresponding-source obligations
before public redistribution.
This does not bundle third-party accounts, paid services or external desktop apps.

The app also seals `bundle/workspace-manifest.json`. On a new signed package,
the frozen backend migrates pristine vendor skills/plugins into its authenticated
control-file snapshot before starting tools. `workspace-history.json` contains
package digests from the preserved v235 macOS source release (no user content).
Later installed vendor digests are retained in the encrypted baseline. Packages
with user edits/additions/deletions or conflicting layouts are preserved whole;
AGENTS.md, preferences, credentials and conversations are never vendor-update
targets. Migration does not enable `ELREN_ACCEPT_CONTROL_FILE_CHANGES` and rolls
back on failure. Unchanged authenticated manifests avoid repeat signature scans.

Audiveris text OCR additionally needs **legacy-compatible** Tesseract data.
The source archive carries one platform-neutral copy of `eng`, `chi_sim` and
`chi_tra` under `work/tool-runtime/native/audiveris/tessdata`, including Apache-2.0
LICENSE and a pinned provenance lock. No Windows Java/JAR/native binaries are
included by this exception. The offline bundler verifies and copies these five
files into the app; missing or changed data stops preflight. The reviewed source
is [tesseract-ocr/tessdata 4.1.0](https://github.com/tesseract-ocr/tessdata/tree/4767ea922bcc460e70b87b1d303ebdfed0897da8).
Do not substitute `tessdata_fast` or `tessdata_best` LSTM-only files: Audiveris
requires legacy support. Direct use of `bundle_audiveris.py` also accepts
`--tessdata-source /path/to/verified/tessdata`. Neither installer downloads data.

Score-title/tempo OCR also requires the three local RapidOCR models
`PP-OCRv6_det_small.onnx`, `PP-OCRv6_rec_small.onnx`, and
`ch_ppocr_mobile_v2.0_cls_mobile.onnx` in the locked Python dependency's
`rapidocr/models` directory. The build verifies their fixed reviewed SHA-256
digests from RapidOCR 3.9.2 `default_models.yaml`, before and after PyInstaller.
Absent, empty or corrupted models fail the build without initializing OCR or
its downloader; even identical corrupted source/frozen copies are rejected.
Provision reviewed model files
before building if the dependency wheel omits them; these checks never fetch
models. PDF/image metadata recognition is still best-effort, not a guarantee
that a title or tempo will always be found.

Double-click `Build Elren for macOS.command`, or run:

```zsh
chmod +x "Build Elren for macOS.command" macos/build-macos-app.sh
./macos/build-macos-app.sh
```

The output is `dist/Elren-v1.0-macOS-<architecture>.zip` and a matching SHA-256 file.

An ad-hoc signature is suitable only for local testing. Public distribution requires an Apple Developer ID Application certificate and notarization:

```zsh
export ELREN_CODESIGN_IDENTITY="Developer ID Application: Example Company (TEAMID)"
export ELREN_NOTARY_PROFILE="elren-notary"
./macos/build-macos-app.sh
```

After first launch, grant Elren Accessibility, Screen Recording, and Automation permissions in System Settings → Privacy & Security when desktop control is needed.

The Android APK is independent from this build and is not read, changed, or rebuilt by the macOS script.
