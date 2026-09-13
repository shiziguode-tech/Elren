# Public release prerequisites

This repository has no top-level project license yet. Keep it private until the
owner has selected a suitable license for the original Elren code and reviewed
the rights and distribution obligations of the third-party components.

- Review vendored and linked components, their exact versions, modifications,
  license texts, copyright notices and corresponding-source requirements.
- Preserve THIRD_PARTY_NOTICES.md and component-specific licenses. In particular,
  review Audiveris, LilyPond, jianpu-ly, WebView2 SDK binaries, OCR models, fonts,
  icons, other media, and the runtimes distributed separately in release ZIPs.
- The Windows WebView2 DLLs are retained as required build inputs; verify their
  SDK redistribution terms and add the applicable notice before public release.
- Do not substitute a blanket license for third-party rights. Public visibility
  alone is not an open-source license.
- Review source and new Git history for secrets and personal data before pushing.
  The preparation scan used known local credentials and selected patterns; it is
  not a complete or independent security audit.
- Verify fresh-environment builds on Windows and macOS before making broad claims.
- Add LICENSE and update README once the owner approves the license decision.

No license decision or public upload was made during source preparation.
