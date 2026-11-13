"""How text looks, by what it means: the one place the TUI chooses colours.

There are two kinds of colour in the TUI. The CSS (app.tcss) styles the
widgets themselves, with the theme's variables ($accent, $panel...), plus
the few of our own from css_variables() below. Text inside them (sidebar
entries, table cells, the book details, the log) is built in code, and gets
its look from here: each role names a theme variable, resolved for the
current theme. So everything follows the theme, and changing what "failed"
looks like is one line.
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


def css_variables(theme: Theme) -> dict[str, str]:
    """Variables of our own for app.tcss, worked out for `theme`.

    $alert and $on-alert: the y/n question's bar, and its text. The theme's
    warning colour, solid, with text that contrasts with it. Not Textual's
    $warning-muted: that's a blend toward the background, and an ANSI theme
    can't blend (a terminal colour has no RGB), so it came out as solid
    yellow under the terminal's light text (1.4:1 in an Omarchy theme).

    The text is `auto`: Textual picks black or white, whichever contrasts
    more with the bar (some themes' warning is a mid-tone: solarized's).
    ANSI themes can't compute that either: they get the terminal's black,
    which in a dark theme is its background colour, so it's dark.
    """
    variables = theme.to_color_system().generate()
    warning = variables["warning"]
    if warning.startswith("ansi_"):
        return {"alert": warning, "on-alert": "ansi_black"}
    return {"alert": warning, "on-alert": "auto 90%"}


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
