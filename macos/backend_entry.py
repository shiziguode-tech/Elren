"""Frozen macOS backend entry, including its isolated metadata OCR worker."""
from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--score-metadata":
        if len(arguments) != 2 or not arguments[1].strip():
            print(json.dumps({"ok": False, "error": "--score-metadata requires exactly one score path"}))
            return 2
        # Do not import the web service in worker mode. In a frozen bundle,
        # sys.executable is this entry point, not a general Python interpreter.
        from deepdesk.score_title_ocr import main as score_metadata_main

        return score_metadata_main([arguments[1]])
    if arguments:
        print(json.dumps({"ok": False, "error": "unsupported backend arguments"}))
        return 2
    from deepdesk.main import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
