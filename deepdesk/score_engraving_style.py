"""Presentation-only house style for the pinned LilyPond compiler output."""
from __future__ import annotations

import re

STYLE = r'''
% Elren reading edition: no musical events are changed here.
#(set-global-staff-size 24)
\header { tagline = ##f }
\paper {
  #(set-paper-size "a4")
  top-margin = 14\mm
  bottom-margin = 15\mm
  left-margin = 16\mm
  right-margin = 16\mm
  ragged-bottom = ##t
  ragged-last-bottom = ##t
  system-system-spacing = #'((basic-distance . 10) (minimum-distance . 8) (padding . 3) (stretchability . 12))
  markup-system-spacing = #'((basic-distance . 4) (minimum-distance . 3) (padding . 2))
  top-system-spacing = #'((basic-distance . 8) (minimum-distance . 6) (padding . 3))
}
\layout {
  \context {
    \Score
    \override SpacingSpanner.shortest-duration-space = #2.2
    \override SpacingSpanner.spacing-increment = #1.2
    \override BarNumber.font-size = #-3
  }
}
'''


def apply_reading_style(lilypond_text: str) -> str:
    """Insert fixed trusted layout after compiler globals, before the first score.

    Do not rewrite notes, beams, repeat structure, user lyrics or MIDI blocks.
    Keep upstream automatic collision-aware line/page breaking for dense music.
    """
    if "% Elren reading edition:" in lilypond_text:
        return lilypond_text
    lilypond_text = lilypond_text.replace(
        r"\override Score.BarNumber.break-visibility = #center-visible",
        r"\override Score.BarNumber.break-visibility = #begin-of-line-visible",
    ).replace(r"\override Score.BarNumber.Y-offset = -1", r"\override Score.BarNumber.Y-offset = 2")
    lilypond_text = re.sub(
        r"\\set Score\.barNumberVisibility = #\(every-nth-bar-number-visible \d+\)",
        lambda _: r"\set Score.barNumberVisibility = #all-bar-numbers-visible", lilypond_text,
    )
    first_score = re.search(r"(?m)^\\score\s*\{", lilypond_text)
    if first_score is None:
        raise ValueError("LilyPond compiler output has no score block")
    return lilypond_text[:first_score.start()] + STYLE + "\n" + lilypond_text[first_score.start():]
