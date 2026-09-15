# Source synchronization: v272-sync-20260914

## September 14 synchronization

- Scope artifact requirements to substantive user requests, not quoted tool or
  model context. Short continuation messages preserve the actual request.
- Permit proportionate read-only mobile outcome verification without repeating
  submissions. Add combined mobile screenshot/semantic observation and focused
  routing for short phone tasks.
- Add Android auxiliary input with temporary input-method switching, editor
  validation, and authentication-screen checks.
- Remove the Windows live_computer_use start keyword/profile gate. Desktop
  leases, approvals, visual checks, cancellation and emergency stop remain.
- Display final reports consistently and add Windows completion notifications.

The Apple Silicon application was rebuilt on an M4 Mac (macOS 26.0), with 35
related regression tests passing, resource/model checks, and ad-hoc signature
verification. The resulting ZIP passed CRC and transfer SHA-256 verification.
Native GUI acceptance for this rebuilt application has not been performed; it
is not Developer ID signed or Apple-notarized. The delivered Mac build retained
its existing platform-specific live_computer_use implementation.

This repository synchronization contains source and tests only. It does not
publish private archives, credentials, chat databases, or the binary ZIP assets.

## Previous source preparation

This source snapshot includes the Windows live_computer_use upgrade and the
agent prompt/recovery, tool-selection and context-compaction fixes. The public
Windows and Apple Silicon distribution archives are separate release assets;
owner-private archives must never be published.

Source-only preparation checks are not complete fresh-machine build validation.
The paired prompt-only API pilot tied at 10/10 for both variants and does not
establish a reasoning-quality improvement or full agent end-to-end success.

The existing Mac workspace required explicit, one-time protected-file baseline
acceptance after the reviewed policy expansion. An ordinary restart afterward
succeeded; this does not certify every native GUI or hardware scenario.

## Public-distribution gates still open

- The owner has adopted the top-level source-available LICENSE. Third-party
  obligations and notice gaps remain under review; adoption is not legal approval.
- Fresh-environment builds and GitHub Actions runs remain unverified for this
  source repository. Local tests do not substitute for final archive inspection.
- These artifacts are not represented as commercially signed, notarized, legally
  approved, or universally compatible. Follow each platform's documented trust
  and permission flow rather than disabling operating-system security.
- An independent source/history secret review remains outstanding despite the
  repository now being public; the preparation scan was limited in scope.

See LICENSE_PENDING.md and THIRD_PARTY_NOTICES.md before publication.
