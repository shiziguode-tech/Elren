# Third-party component inventory and notice locations

This file is a factual inventory of notable third-party material present in the
current Elren v1.0 release workspace. It is not a substitute for the license
text shipped with a component, it is not an exhaustive legal opinion, and it
does not grant a license to Elren. The top-level `LICENSE` applies only to
eligible original Elren material; third-party materials retain their own terms.
Adopting that license does not complete the redistribution gates below.

## Audiveris 5.11.0

The Windows release includes an unmodified Audiveris 5.11.0 command-line
runtime and its `basic-classifier.zip` optical-music-recognition model inside
`work/tool-runtime/native/audiveris`. Elren invokes it as a separate local,
headless process and does not send score images to a cloud service.

- Project: https://github.com/Audiveris/audiveris
- Reviewed commit: `9e1e55cd2746037d059345881c53e6a6754bffbd`
- License: GNU Affero General Public License v3.0 only (AGPL-3.0-only)
- Bundled license: `work/tool-runtime/licenses/audiveris-AGPL-3.0.txt`
- Integrity inventory: `work/tool-runtime/native/audiveris/elren-integrity.json`

Audiveris is a separate third-party program. No Elren API key or credential is
placed in its runtime or child-process environment. Corresponding source is
available from the project URL and reviewed commit above.

The separate Elren child-JVM bootstrap is shipped with its source in
`deepdesk/java`. It redirects only the task-local headless JVM's default data
folder before invoking the unchanged Audiveris entry point.

## Tesseract tessdata 4.1.0

Legacy-compatible English, Simplified Chinese and Traditional Chinese text
recognition data are bundled at `work/tool-runtime/native/audiveris/tessdata`.
The exact three models, sizes and hashes are recorded in
`deepdesk/audiveris_tessdata.lock.json` and the copy beside the models.

- Project: https://github.com/tesseract-ocr/tessdata
- Reviewed commit: `4767ea922bcc460e70b87b1d303ebdfed0897da8`
- License: Apache License 2.0; full text beside the models as `LICENSE`

The macOS source package includes these same platform-neutral data and notices;
this does not mean a native macOS application has been built or verified.

## jianpu-ly 1.889

Elren vendors the unmodified `jianpu_ly` Python module and mirrors it under
`work/tool-runtime/native/jianpu-ly` in Docker Lite. It translates validated
numbered notation into LilyPond input, including `WithStaff` output for
numbered-notation-to-Western-staff conversion. It replaces Elren's earlier
bespoke browser notation layout.

The vendored source file stays unchanged. Elren's separately shipped
`deepdesk/jianpu_compiler_compat.py` applies a hash-pinned, in-memory AST adapter
when compiling to preserve transpose scopes inside repeats. The adapter source
is included with the application; it is not presented as an upstream release.


- Project and corresponding source: https://github.com/ssb22/jianpu-ly
- Distribution: `jianpu_ly-1.889.tar.gz`
- Distribution SHA-256: `06bae9443df40a1d0bea43143d2124ad1781e6469fa6521bd4183d566a77afd0`
- License: Apache License 2.0
- Bundled license and provenance: `work/tool-runtime/native/jianpu-ly/LICENSE`
  and `work/tool-runtime/native/jianpu-ly/UPSTREAM.json` (source mirror:
  `jianpu_ly.py`)

## GNU LilyPond 2.26.0

The Windows releases include the unmodified official GNU LilyPond 2.26.0
x86_64 package under `work/tool-runtime/native/lilypond`. Elren invokes it as a
separate offline process to typeset jianpu-ly output as SVG and PDF. The macOS
build script downloads the fixed official arm64 archive and rejects it unless
its SHA-256 matches the recorded value.

- Project and corresponding source: https://gitlab.com/lilypond/lilypond/-/releases/v2.26.0
- License: GNU General Public License v3.0 or later (GPL-3.0-or-later)
- Bundled license and provenance: `work/tool-runtime/native/lilypond/COPYING`
  and `work/tool-runtime/native/lilypond/UPSTREAM.json`

## Notices already retained in the portable payload

| Component | Workspace version/evidence | License or notice location in the package |
| --- | --- | --- |
| CPython portable runtime | 3.14.6 in `work/runtime-bundle.json` | `work/python-runtime/LICENSE.txt` |
| Python dependencies | Installed under `.venv/Lib/site-packages` from the ranges in `pyproject.toml` | Package metadata under the corresponding `*.dist-info` directories; license files supplied there by each package are retained, and Playwright's package/driver notices remain in this environment |
| OpenClaw | 2026.7.1-2; package metadata declares MIT | `work/openclaw-runtime/node_modules/openclaw/LICENSE` and `THIRD_PARTY_NOTICES.md` in that directory |
| npm CLI | 11.16.0; package metadata declares Artistic-2.0 | `work/node-runtime/node_modules/npm/LICENSE` plus notices retained in its dependency tree |
| Chromium Headless Shell | Playwright revision 1234, Chrome 151.0.7922.34 | `work/browser-runtime/chromium_headless_shell-1234/chrome-headless-shell-win64/LICENSE.headless_shell` |
| Playwright browser FFmpeg payload | Playwright revision 1011 | `work/browser-runtime/ffmpeg-1011/COPYING.LGPLv2.1` |
| Lucide and Feather-derived icons | Files under `deepdesk/static/icons` | `deepdesk/static/icons/LICENSE-lucide.txt` (ISC and MIT notices) |
| KaTeX | 0.17.0; offline mathematical typesetting runtime and WOFF2 fonts | `deepdesk/static/vendor/katex/LICENSE.txt` (MIT and SIL Open Font License notices) |
| Air Datepicker | 3.6.0; local date/time picker | `deepdesk/static/vendor/air-datepicker/LICENSE.md` (MIT) and `UPSTREAM.json` |
| jianpu-ly | 1.889; mature bidirectional numbered/staff notation bridge | `work/tool-runtime/native/jianpu-ly/LICENSE` and `UPSTREAM.json` |
| GNU LilyPond | 2.26.0; offline SVG/PDF music typesetting | `work/tool-runtime/native/lilypond/COPYING` and `UPSTREAM.json` |
| OpenClaw-bundled skill creator | Bundled skill source | `skills/openclaw-bundled/skill-creator/license.txt` (Apache License 2.0 text) |

The default Windows tool runtime is pinned by hashes and includes these
component notices under `work/tool-runtime/licenses`:

| Component | Version | License recorded by the lock/package metadata | Packaged notice |
| --- | --- | --- | --- |
| jq | 1.8.2 | MIT | `jq-LICENSE` |
| ripgrep | 15.2.0 | Unlicense OR MIT | `ripgrep-COPYING`, `ripgrep-LICENSE-MIT` |
| GitHub CLI | 2.97.0 | MIT | `github-cli-LICENSE` |
| FFmpeg shared LGPL build | 8.1.2-34-g9b6c8969e0+btbn-20260812 | LGPL-2.1-or-later | `ffmpeg-LICENSE.txt` |
| mcporter | 0.13.4 | MIT | `node-mcporter-LICENSE` |
| xurl | 1.3.1 | MIT | `xurl-LICENSE` |

The exact sources, checksums, profiles, and extended-profile components are
recorded in `launcher/tool-runtime.lock.json`. Components listed only in the
extended profile are not represented here as part of the default payload.

## macOS portable Git (v241 QA and later builds)

The macOS builder packages the arm64 `desktop/dugite-native` v2.53.0-4
distribution, not the operating system's developer-tool Git. The pinned download
hashes and source references are in `macos/bundle_git.py` and the installed
`work/tool-runtime/native/git/ELREN-UPSTREAM.json`.

- Git 2.53.0: GPL version 2; preserved `licenses/git-COPYING`.
- Git LFS 3.7.1: preserved `licenses/git-lfs-LICENSE`.
- Git Credential Manager 2.9.0: preserved
  `licenses/git-credential-manager-LICENSE` and upstream `libexec/git-core/NOTICE`
  for its private runtime/dependencies.

Elren supplies a package-relative shell launcher; upstream native files are
re-signed for the application. Public redistribution requires reviewing the exact
payload and fulfilling applicable corresponding-source obligations. A list of
source URLs is not represented as completion of that release gate.

## Other third-party material in release inputs

- The Windows shell uses Microsoft WebView2 assemblies and loader version
  1.0.4129.50 at the package root and under `launcher/webview2`.
  The SDK's official `LICENSE.txt` and `NOTICE.txt` are retained in
  `launcher/webview2`, with verified DLL hashes in `UPSTREAM.md`. These do not
  replace the terms for the separately installed WebView2 browser Runtime.
- The portable Node executable is version 24.18.1 according to
  `work/runtime-bundle.json`.
- The Android source tree includes the Gradle wrapper configured for Gradle
  8.9 and declares Android Gradle Plugin 8.7.3 and Kotlin Android 2.1.0.
  Dependencies fetched by Gradle must be inventoried from the resolved release
  build, not inferred only from these build declarations.

## Notice-completion gates

Before public redistribution, the project owner should complete and retain a
license/SBOM review of the exact final payload. The source repository is not a
complete installed runtime; the following final-artifact checks remain open:

- the separate WebView2 browser Runtime's applicable redistribution terms;
- preservation of the Node.js runtime license at `work/node-runtime/LICENSE`
  in each Windows release (Node itself is not committed to this source tree); and
- a consolidated Gradle-wrapper/Android resolved-dependency notice in the
  Android source tree.

Those checks are recorded as release gates, not filled with guessed license
terms. Preserve every existing nested license/notice file when producing the
final package and resolve the applicable upstream redistribution terms before
release.
