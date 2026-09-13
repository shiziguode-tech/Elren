"""Child-local workaround for the bundled Windows engraver's stale GC roots.

LilyPond 2.26.0 / Guile 3.0.11 can scan an unmapped xmllite.dll range after
MIDI generation. Retain one loader reference until that short-lived child
exits. Do not change GC policy, the parent process, system DLLs or user scores.
"""
from __future__ import annotations

import sys

WINDOWS_XML_READY = "ELREN_WINDOWS_XML_READY"

# Fixed trusted startup code, never interpolated with a filename or score text.
# 2048 = LOAD_LIBRARY_SEARCH_SYSTEM32: no current-directory/PATH DLL search.
# Deliberately no FreeLibrary/finalizer: the OS reclaims this one reference at
# child exit, after the last GC scan, including on timeout/forced termination.
_WINDOWS_XML_LEASE = r'''(begin
  (use-modules (system foreign))
  (define elren-xml-kernel (dynamic-link "kernel32.dll"))
  (define elren-xml-load
    (pointer->procedure '* (dynamic-func "LoadLibraryExA" elren-xml-kernel)
                          (list '* '* unsigned-int)))
  (define elren-xml-lease
    (elren-xml-load (string->pointer "xmllite.dll") (make-pointer 0) 2048))
  (if (null-pointer? elren-xml-lease)
      (error "Elren: system XML library reference unavailable"))
  (display "ELREN_WINDOWS_XML_READY\n"))'''


def windows_engraver_arguments() -> list[str]:
    """Return trusted startup arguments; other platforms keep their native path."""
    return ["-e", _WINDOWS_XML_LEASE] if sys.platform == "win32" else []
