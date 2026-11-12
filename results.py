"""Parse SearchBot result files (the .txt that comes back from `@search <query>`).

A results file is a short header followed by one line per match:

    Search results from SearchBot v3.00.13 by Ook, ...
    Searched 14 lists for "warhammer" , found 1000 matches. Enjoy!
    ...

    !Bsk Dan Abnett - Warhammer 40,000 - Penitent (Retail).epub ::INFO:: 2.19MB
    !artemis_serv f73222c7950c | Warhammer 40K_ The Purging of Kadillus - Gav Thorpe.mobi ::INFO:: 5.09MB

Each result line starts with `!<bot>`, and the request to send back to the
channel is that line without the details the bot adds after the file. The
bots that serve results don't agree on a format; these are the ones seen:

    !Bsk Title.epub ::INFO:: 2.19MB                       SearchBot's own
    !Firebound %037DFABE5B8F% Title.epub  ::INFO:: 676.97 KB
    !Ook Title (pdf).rar  ::INFO:: 2MB ::HASH:: 24b4ef36dc21f3ed
    !Horla Title.epub                                     no details at all
    !Ashurbanipal <id> - Author - Title [eng]  (EPUB) 3.0 MB - [tags]

so every `!<bot> <something>` line is a result: `::INFO::` and what follows
it, or a trailing `(EXT) size - [tags]`, are details, and the rest is the
request. Lines after the header that don't start with `!<bot>` are kept
verbatim in `SearchResults.unparsed`, and the original file stays on disk.
"""

import re
from dataclasses import dataclass
from pathlib import Path

# Searched 14 lists for "warhammer" , found 1000 matches. Enjoy!
_SUMMARY_RE = re.compile(
    r'Searched (?P<lists>\d+) lists? for "(?P<query>.*)"\s*,\s*found (?P<matches>\d+) match',
    re.IGNORECASE,
)

# "!<bot> <the rest>"
_RESULT_RE = re.compile(r"^!(?P<bot>\S+) +(?P<rest>\S.*?)\s*$")

# "<name> ::INFO:: <size> ::HASH:: <hash>": size and hash optional, any spacing
_INFO_RE = re.compile(r"\s*::INFO::\s*(?P<info>.*)$")

# Ashurbanipal's details: "<name>  (EPUB) 3.0 MB - [Fantasy, Gaming]", tags optional
_TAIL_RE = re.compile(
    r"\s+\((?P<ext>[A-Za-z0-9]{2,5})\)\s+(?P<size>\d+(?:\.\d+)?\s*[KMGT]?i?B)(?:\s+-\s+\[.*\])?$",
    re.IGNORECASE,
)

# A size like "2.19MB", "680.86KB", "44.7 MB", "1 GiB"
_SIZE_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]i?B|B)", re.IGNORECASE)
_UNITS = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

# File extension at the end of a filename: "Penitent (Retail).epub" -> "epub"
_EXT_RE = re.compile(r"\.(?P<ext>[A-Za-z0-9]{2,5})$")

# Ids some bots put before the name; kept in the request, not the title:
#   "f73222c7950c | Real Title.mobi"   artemis_serv
#   "%037DFABE5B8F% Real Title.epub"   Firebound
_HASH_PREFIX_RE = re.compile(r"^(?:\S+ \||%[0-9A-Fa-f]+%) +(?P<title>.+)$")
# "ckH79Mt/Nwe2A0uyM0FCew - Author - Title": Ashurbanipal's (base64, 22 chars)
_ID_PREFIX_RE = re.compile(r"^[A-Za-z0-9+/]{22} - (?P<title>.+)$")


@dataclass(frozen=True)
class ResultItem:
    bot: str
    """The bot that serves the file, without the "!" (e.g. "Bsk")."""
    request: str
    """The exact line to send to the channel to request this file."""
    title: str
    """Human-readable name: no bot, hash prefix, size or extension."""
    ext: str | None
    """Lower-case file type ("epub", "mobi", "azw3"...), if the name has one."""
    size_bytes: int | None
    """Approximate size, if the line states one (KB/MB treated as 1024-based)."""
    raw: str
    """The original line, unmodified."""
    line_no: int
    """1-based line number in the source file."""


@dataclass(frozen=True)
class SearchResults:
    query: str | None
    """The search term, from the summary line."""
    lists_searched: int | None
    match_count: int | None
    """Matches the bot reported. May differ from len(items) (capped, or unparsed lines)."""
    header: tuple[str, ...]
    """Non-empty lines before the first result."""
    items: tuple[ResultItem, ...]
    unparsed: tuple[str, ...]
    """Non-empty lines after the header that aren't results, kept verbatim."""


def parse_size(text: str) -> int | None:
    """Parse a human size like "2.19MB" or "44.7 MB" into bytes."""
    m = _SIZE_RE.fullmatch(text.strip())
    if not m:
        return None
    # "MB" / "MIB" / "B" -> look up by first letter
    return round(float(m.group("num")) * _UNITS[m.group("unit")[0].upper()])


def parse_line(line: str, line_no: int = 0) -> ResultItem | None:
    """Parse one result line ("!<bot> ..."). Returns None for anything else."""
    m = _RESULT_RE.match(line)
    if not m:
        return None
    bot, name = m.group("bot"), m.group("rest")

    # Split off the details, if any
    size = ext = None
    title_re = _HASH_PREFIX_RE
    if info := _INFO_RE.search(name):
        name = name[: info.start()]
        size = info.group("info").split("::")[0]  # not the ::HASH::
    elif tail := _TAIL_RE.search(name):
        name = name[: tail.start()]
        size, ext = tail.group("size"), tail.group("ext").lower()
        title_re = _ID_PREFIX_RE
    if not name:
        return None  # e.g. "!Bsk ::INFO:: 1MB": nothing to request

    # Drop an id before the name from the display title (the request keeps it)
    title = (h.group("title") if (h := title_re.match(name)) else name)
    if e := _EXT_RE.search(title):
        title, ext = title[: e.start()], e.group("ext").lower()

    return ResultItem(
        bot=bot,
        request=f"!{bot} {name}",
        title=title.strip(),
        ext=ext,
        size_bytes=parse_size(size or ""),
        raw=line.rstrip("\r\n"),
        line_no=line_no,
    )


def parse_results(text: str) -> SearchResults:
    header: list[str] = []
    items: list[ResultItem] = []
    unparsed: list[str] = []
    in_header = True

    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        # Header = everything before the first "!bot" line, parseable or not
        if line.startswith("!"):
            in_header = False
        if in_header:
            header.append(line.strip())
        elif item := parse_line(line, line_no):
            items.append(item)
        else:
            unparsed.append(line.strip())

    query = lists = matches = None
    for line in header:
        if m := _SUMMARY_RE.search(line):
            query = m.group("query")
            lists = int(m.group("lists"))
            matches = int(m.group("matches"))
            break

    return SearchResults(
        query=query,
        lists_searched=lists,
        match_count=matches,
        header=tuple(header),
        items=tuple(items),
        unparsed=tuple(unparsed),
    )


def load_results(path: Path) -> SearchResults:
    """Read and parse a results file. Tolerates non-UTF-8 bytes."""
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Older IRC tooling often emits cp1252/latin-1; latin-1 never fails
        text = data.decode("latin-1")
    return parse_results(text)
