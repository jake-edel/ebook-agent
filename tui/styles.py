"""How text looks, by what it means: the one place the TUI chooses colours.

There are two kinds of colour in the TUI. The CSS (app.tcss) styles the
widgets themselves, with the theme's variables ($accent, $panel...). Text
inside them (sidebar entries, table cells, the book details, the log) is
built in code, and gets its look from here: each role names a theme
variable, resolved for the current theme. So everything follows the theme,
and changing what "failed" looks like is one line.
"""

from dataclasses import dataclass, fields

from rich.style import Style as RichStyle
from textual.style import Style
from textual.theme import Theme

# What each role looks like, in Textual's style syntax. $text-error and
# $text-warning, not $error and $warning: those are for backgrounds, these
# are tuned to be read as text. Muted is `dim`, not $text-muted: that's
# relative to the background ("auto 60%"), which text built in code can't know.
ROLES = {
    # Something went wrong: a failed request or hook, an error in the log
    "failed": "$text-error",
    # Worth noticing, but nothing failed: a warning in the log
    "warning": "$text-warning",
    # Secondary: times, sizes, labels, offline bots' rows
    "muted": "dim",
    # The thing itself: a title, a search's query
    "strong": "bold",
    # Section headings: BOOKS, SEARCHES, "from the bot"
    "heading": "bold dim",
}


@dataclass(frozen=True)
class Styles:
    """ROLES, resolved for one theme into styles Rich text can use."""

    failed: RichStyle
    warning: RichStyle
    muted: RichStyle
    strong: RichStyle
    heading: RichStyle

    @classmethod
    def for_theme(cls, theme: Theme) -> "Styles":
        variables = theme.to_color_system().generate()
        return cls(**{f.name: Style.parse(ROLES[f.name], variables).rich_style
                      for f in fields(cls)})
