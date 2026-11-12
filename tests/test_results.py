"""Parsing SearchBot result files."""

from pathlib import Path

import pytest

from results import load_results, parse_line, parse_results, parse_size

FIXTURE = Path(__file__).parent / "fixtures" / "search_warhammer.txt"


@pytest.fixture(scope="module")
def results():
    return load_results(FIXTURE)


def by_title(results, fragment):
    matches = [i for i in results.items if fragment in i.title]
    assert len(matches) == 1, f"expected one item matching {fragment!r}, got {len(matches)}"
    return matches[0]


# --- whole file --------------------------------------------------------------

def test_summary_parsed_from_header(results):
    assert results.query == "warhammer"
    assert results.lists_searched == 14
    assert results.match_count == 1000  # what the bot reported, not len(items)


def test_header_and_items_separated(results):
    assert len(results.header) == 5
    assert results.header[0].startswith("Search results from SearchBot")
    assert len(results.items) == 10  # Ashurbanipal's two included
    assert results.unparsed == ()


def test_line_numbers_point_into_source(results):
    lines = FIXTURE.read_text().splitlines()
    for item in results.items:
        assert lines[item.line_no - 1] == item.raw


def test_crlf_and_lf_parse_identically():
    crlf = FIXTURE.read_bytes().decode()
    assert "\r\n" in crlf
    assert parse_results(crlf) == parse_results(crlf.replace("\r\n", "\n"))


# --- standard "::INFO::" lines ----------------------------------------------

def test_info_line(results):
    item = by_title(results, "Penitent")
    assert item.bot == "Bsk"
    assert item.request == "!Bsk Dan Abnett - Warhammer 40,000 - Penitent (Retail).epub"
    assert item.title == "Dan Abnett - Warhammer 40,000 - Penitent (Retail)"
    assert item.ext == "epub"
    assert item.size_bytes == round(2.19 * 1024**2)


def test_info_requests_are_line_before_info_marker(results):
    info_lines = [i for i in results.items if "::INFO::" in i.raw]
    assert len(info_lines) == 8
    for item in info_lines:
        assert item.request == item.raw.split(" ::INFO::")[0]


def test_hash_prefix_kept_in_request_but_not_title(results):
    item = by_title(results, "Purging of Kadillus")
    assert item.bot == "artemis_serv"
    assert item.request == (
        "!artemis_serv f73222c7950c | Warhammer 40K_ The Purging of Kadillus - Gav Thorpe.mobi"
    )
    assert item.title == "Warhammer 40K_ The Purging of Kadillus - Gav Thorpe"
    assert item.ext == "mobi"


def test_title_starting_with_word_dash_kept_intact(results):
    item = next(i for i in results.items if i.ext == "epub" and "Heroes 03" in i.title)
    assert item.title == "Warhammer - [Heroes 03] - Sword of Vengeance - Chris Wraight (v1.0)"


def test_kb_size(results):
    assert by_title(results, "Rose in Darkness").size_bytes == round(680.86 * 1024)


# --- other bots' formats ------------------------------------------------------
#
# Real lines from real searches: each bot's own take on the format

@pytest.mark.parametrize("line, req, title, ext, size", [
    # Two spaces before ::INFO::, and a size with a space in it
    ("!peapod George Orwell - 1984.epub  ::INFO:: 270.54KB",
     "!peapod George Orwell - 1984.epub", "George Orwell - 1984", "epub", round(270.54 * 1024)),
    ("!Wench George Orwell - Animal Farm.pdf  ::INFO:: 233.4KB",
     "!Wench George Orwell - Animal Farm.pdf", "George Orwell - Animal Farm", "pdf", round(233.4 * 1024)),
    # A ::HASH:: after the size
    ("!Ook The Cambridge Companion to George Orwell (2007) - John Rodden (pdf).rar  ::INFO:: 944KB ::HASH:: 4407b386b7712e7d",
     "!Ook The Cambridge Companion to George Orwell (2007) - John Rodden (pdf).rar",
     "The Cambridge Companion to George Orwell (2007) - John Rodden (pdf)", "rar", 944 * 1024),
    ("!Pondering-Ebooks2 George Orwell - 1984 (v5.0) (epub).rar  ::INFO:: 2MB ::HASH:: e2e1656a49f7cbb",
     "!Pondering-Ebooks2 George Orwell - 1984 (v5.0) (epub).rar",
     "George Orwell - 1984 (v5.0) (epub)", "rar", 2 * 1024**2),
    # A %hash% before the name: kept in the request, not the title
    ("!Firebound %037DFABE5B8F% George Orwell - Homage to Catalonia (Retail).epub  ::INFO:: 676.97 KB",
     "!Firebound %037DFABE5B8F% George Orwell - Homage to Catalonia (Retail).epub",
     "George Orwell - Homage to Catalonia (Retail)", "epub", round(676.97 * 1024)),
    # No details at all: the whole line is the request
    ("!Horla George Orwell - 1984 (Penguin) (epub).epub",
     "!Horla George Orwell - 1984 (Penguin) (epub).epub", "George Orwell - 1984 (Penguin) (epub)", "epub", None),
    ("!Dumbledore Mark Z Danielewski - House of Leaves (lrf).epub",
     "!Dumbledore Mark Z Danielewski - House of Leaves (lrf).epub",
     "Mark Z Danielewski - House of Leaves (lrf)", "epub", None),
    # Ashurbanipal: an id before the name, "(EXT) size - [tags]" after it
    ("!Ashurbanipal e+dKfyTbsTh+hhAWPdJoxA - David Ryan - George Orwell on Screen [eng]  (EPUB) 3.0 MB - [Film, Television]",
     "!Ashurbanipal e+dKfyTbsTh+hhAWPdJoxA - David Ryan - George Orwell on Screen [eng]",
     "David Ryan - George Orwell on Screen [eng]", "epub", round(3.0 * 1024**2)),
    ("!Ashurbanipal wLzfYssqwXcipxVs8UQztw - Games-Workshop - Soul Wars [eng]  (PDF) 44.7 MB",
     "!Ashurbanipal wLzfYssqwXcipxVs8UQztw - Games-Workshop - Soul Wars [eng]",
     "Games-Workshop - Soul Wars [eng]", "pdf", round(44.7 * 1024**2)),
    ("!Ashurbanipal A2gYmSoq1wqhdVThRqtCjA - George Orwell - Essays (audiobook)  (M4B) 1.3 GB - [Audiobook]",
     "!Ashurbanipal A2gYmSoq1wqhdVThRqtCjA - George Orwell - Essays (audiobook)",
     "George Orwell - Essays (audiobook)", "m4b", round(1.3 * 1024**3)),
])
def test_other_formats(line, req, title, ext, size):
    item = parse_line(line)
    assert (item.request, item.title, item.ext, item.size_bytes) == (req, title, ext, size)


def test_id_like_words_are_only_stripped_in_ashurbanipals_format():
    # 22 letters then " - " is just a title, without the "(EXT) size" details
    item = parse_line("!Horla Abcdefghijklmnopqrstuv - Some Title.epub")
    assert item.title == "Abcdefghijklmnopqrstuv - Some Title"


@pytest.mark.parametrize("line", [
    "!Bsk ::INFO:: 1MB",  # details, but nothing to request
    "!Bsk  ::INFO::",
])
def test_lines_with_only_details_not_parsed(line):
    assert parse_line(line) is None


def test_malformed_info_marker_is_part_of_the_name():
    item = parse_line("!SomeBot Some Book.epub ::INFO")
    assert item.request == "!SomeBot Some Book.epub ::INFO"


# --- edge cases ----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2.19MB", round(2.19 * 1024**2)),
    ("680.86KB", round(680.86 * 1024)),
    ("44.7 MB", round(44.7 * 1024**2)),
    ("1.5 GB", round(1.5 * 1024**3)),
    ("3 MiB", 3 * 1024**2),
    ("12mb", 12 * 1024**2),
    ("512 B", 512),
    ("big", None),
    ("", None),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_info_marker_without_size():
    item = parse_line("!Bsk Some Book.epub ::INFO::")
    assert item.request == "!Bsk Some Book.epub"
    assert item.size_bytes is None


@pytest.mark.parametrize("line", ["", "   ", "hello", "!", "!Bsk", "!Bsk   ", " !Bsk book.epub"])
def test_non_result_lines(line):
    assert parse_line(line) is None


def test_unrecognised_lines_after_header_kept_not_dropped():
    text = "Searched 1 lists for \"x\" , found 1 matches.\n!Bsk A.epub ::INFO:: 1MB\nsome trailer text\n"
    r = parse_results(text)
    assert len(r.items) == 1
    assert r.unparsed == ("some trailer text",)


def test_empty_file():
    r = parse_results("")
    assert (r.query, r.items, r.header, r.unparsed) == (None, (), (), ())


def test_header_only_no_matches():
    r = parse_results('Searched 14 lists for "zzzz" , found 0 matches.\r\n')
    assert r.query == "zzzz"
    assert r.match_count == 0
    assert r.items == ()


def test_query_with_quotes_inside():
    r = parse_results('Searched 2 lists for "the "best" book" , found 3 matches. Enjoy!')
    assert r.query == 'the "best" book'


def test_non_utf8_file_still_loads(tmp_path):
    path = tmp_path / "latin1.txt"
    path.write_bytes(b"!Bsk Caf\xe9 Society.epub ::INFO:: 1MB\r\n")
    r = load_results(path)
    assert r.items[0].title == "Café Society"


def test_header_ends_at_first_bot_line_even_if_unparseable():
    text = 'Searched 1 lists for "x" , found 2 matches.\n!Bsk ::INFO:: 1MB\ntrailer\n!Bsk A.epub ::INFO:: 1MB\n'
    r = parse_results(text)
    assert r.header == ('Searched 1 lists for "x" , found 2 matches.',)
    assert r.unparsed == ("!Bsk ::INFO:: 1MB", "trailer")
    assert [i.title for i in r.items] == ["A"]
