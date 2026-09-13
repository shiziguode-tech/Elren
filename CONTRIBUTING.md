# Contributing to Elren

Elren uses the custom [source-available LICENSE](LICENSE), not an OSI open-source
license. Read it before copying, distributing or offering modified versions.
Third-party code retains its own license; acknowledgement is not permission to
relicense it. A contribution does not assign your copyright to the maintainer.

## Propose and submit a change

1. Open an issue for a normal bug or feature. For vulnerabilities, use
   [SECURITY.md](SECURITY.md), not a public issue.
2. Keep the change focused and describe reproduction, expected/actual results,
   platform, relevant model/protocol and exact version. Never include keys.
3. Include regression tests and explain untested platform or API scenarios.
4. In the PR, confirm that you own the contribution or have permission to submit
   it under the project's LICENSE. Identify third-party material, its source,
   modifications and license; preserve notices. No automatic right to commercially
   relicense someone else's contribution is implied.
5. Do not include runtime credentials, personal profiles, conversation databases,
   screenshots containing personal information, logs, build caches or private ZIPs.

## Local checks

Use a disposable development checkout and virtual environment, not your personal
Elren installation. Install `python -m pip install -e ".[dev]" build`, then run:

```text
python -m pip check
python -m pytest -q tests/test_release_archive.py tests/test_repository_delivery.py tests/test_prompt_module_scope.py tests/test_agent_deep_validation.py
python -m ruff check .
node --check deepdesk/static/app.js
python -m build
```

Core CI runs this bounded, offline regression subset on Windows and Linux, plus
Windows launcher compilation. It is not a full desktop, microphone, real-provider
or native macOS acceptance test. Full tests may need platform resources; never
give untrusted PR tests production secrets or access to your desktop.

Vendored upstream code is excluded from the first-party lint gate;
that exclusion is not a claim that the upstream code is free of bugs. The macOS
workflow retains its full-suite/full-lint gate. Local lint success does not
replace a native build or a complete cloud workflow run.

## Build and release boundaries

Android CI runs for relevant pushes/PRs; macOS full packaging is manually invoked
and downloads pinned build inputs. See [macOS build instructions](macos/BUILD-ON-MAC.md).
Do not remove checksum, cancellation, input-interference or permission checks to
make a build/test pass. Release ZIPs require separate dependency/license review.
Contribution acceptance is not approval to ship private or unchecked artifacts.
