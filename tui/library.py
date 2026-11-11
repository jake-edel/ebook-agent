"""What the TUI reads from disk: past searches and their results.

Plain Python, no Textual, so it's easy to test.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from results import SearchResults, load_results

log = logging.getLogger(__name__)

# How the agent names results files: "20261108-143005 dune.txt", plus
# " (1)" etc. for a repeat in the same second
_NAME_RE = re.compile(r"^(?P<stamp>\d{8}-\d{6}) (?P<query>.+?)(?: \(\d+\))?$")


@dataclass(frozen=True)
class PastSearch:
    path: Path
    query: str
    """The search term: from the file's header, else from its name."""
    when: datetime
    results: SearchResults


def list_searches(directory: Path) -> list[PastSearch]:
    """Every results file in `directory`, newest first. Missing directory: none."""
    searches = []
    for path in directory.glob("*.txt"):
        try:
            results = load_results(path)
        except OSError as e:
            log.warning("Can't read %s: %s", path, e)
            continue
        searches.append(_past_search(path, results))
    return sorted(searches, key=lambda s: s.when, reverse=True)


def _past_search(path: Path, results: SearchResults) -> PastSearch:
    m = _NAME_RE.match(path.stem)
    if m:
        when = datetime.strptime(m.group("stamp"), "%Y%m%d-%H%M%S")
        name_query = m.group("query")
    else:
        # Not named by the agent (copied in by hand?): fall back to the file itself
        when = datetime.fromtimestamp(path.stat().st_mtime)
        name_query = path.stem
    # The header has the query exactly as sent; the name has "/" replaced
    return PastSearch(path=path, query=results.query or name_query, when=when, results=results)
