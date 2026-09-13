# Repository repair verification — 2026-09-13

Scope: source repository only. No running installation, personal/public release
folder, release ZIP or Mac installation was replaced. Changes are not yet pushed.

## Implemented

- Include the formal LICENSE, acknowledgements and maintenance/security guidance
  in the shared release allowlist; copy the four top-level legal notices into
  the native macOS app resources.
- Provision the pinned official Audiveris arm64 DMG in the manual Mac workflow,
  check its SHA-256 and run the existing offline validator before building.
- Add explicit build-time OCR model cache provisioning with fixed upstream
  hashes, HTTPS-only redirects, bounded downloads and offline verified install.
  No application-startup download or chat/model configuration changes.
- Add credential-free Windows/Linux core CI, frontend syntax checking, critical
  first-party Python lint, Python distribution builds and Windows shell compile.
- Remove the Mac packaging test's dependency on an untracked local runtime;
  assert that the builder consumes the committed manifests instead.
- Add SECURITY.md and CONTRIBUTING.md; correct the README's workflow description.
- Restore Microsoft's exact WebView2 SDK LICENSE and NOTICE after matching all
  three repository DLL hashes to the official 1.0.4129.50 NuGet package. SDK and
  browser Runtime obligations remain explicitly distinguished.

## Local results

- Follow-up regression: 368 passed and 4 skipped across 20 affected test modules,
  including model routing, live-control contracts, lifecycle, frontend and packaging.
  The four skips are packaged-browser tests: the clean source checkout does not
  contain the packaged browser runtime. They are not recorded as passed.
- Full first-party Python lint passed. Both core and Mac CI now use the full
  configured gate. Vendored upstream source is preserved and excluded from lint;
  `.ci-venv` is excluded to avoid scanning installed third-party build dependencies.
- JavaScript syntax check passed.
- Changed/new Python modules and packaging test passed full Ruff checks.
- Real macOS source ZIP export: 562 files; CRC passed; formal LICENSE and SDK
  notice present. This is source export, not a native Mac application build.
- Real Python sdist and wheel build passed; LICENSE verified in both outputs.
- Windows launcher compiled to an isolated verification directory. Two existing
  CS0649 warnings concern JSON-deserialized ServiceIdentity fields.
- Workflow YAML parsing and git diff whitespace checks passed.
- Real OCR model download completed on Windows; all three pinned SHA-256 checks
  passed. Install/redirect/failure behavior is also covered by isolated tests.
- Corrected Gradle notice from 9.6.1 to the committed distribution version 8.9.
- Follow-up source/notation suite: 67 passed, 87 skipped. This overlaps the
  repository-delivery tests above; do not add the two totals as unique tests.
  Source-level compiler/provenance tests now use committed, hash-verified vendor
  files in isolated fixtures rather than an untracked personal runtime. Two
  artifact-export tests explicitly skip when a complete engraver/notation
  runtime is absent, consistent with the other native integration fixtures.
  Native output rendering/MIDI tests remain unverified in this source checkout.

## Remaining gates — not represented as passed

- Cloud Windows/Linux CI and native macOS workflow have not been run for these
  changes. Audiveris DMG mounting/signing and the real model-cache download have
  not been exercised on macOS in this repair session.
- The full test suite, real desktop input, API calls, microphones, and native
  Mac acceptance were not exercised; the above counts are regression counts, not counts
  of successful end-to-end consumer tasks.
- Exact final-payload Node.js notices, WebView2 browser Runtime terms, Android
  resolved-dependency notices and applicable corresponding-source obligations
  still require release-specific verification. No blanket license-compliance
  or legal-enforceability conclusion is made.

Runner reference: GitHub currently documents macos-15 as an arm64 standard runner:
https://docs.github.com/en/actions/reference/runners/github-hosted-runners
