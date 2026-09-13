# Public release prerequisites

The repository is public. The owner has adopted the custom Elren Source Available
License 1.0 in LICENSE for eligible original material. This file now tracks the
remaining review items, not a pending top-level license choice. Adoption is not
a legal compliance certification and does not complete third-party obligations.

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
- Review the custom LICENSE with qualified counsel; no lawyer approval is claimed.

The historical drafts and their AI review remain for provenance, not as operative
licenses. Existing binary releases have not been rebuilt or relicensed by this
source-documentation update.
