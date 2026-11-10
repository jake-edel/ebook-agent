"""Parsing and validating DCC SEND offers."""

import pytest

from config import DownloadsConfig
from dcc import DownloadError, FileTooLargeError, InvalidOfferError
from dcc import parse_dcc_offer as _parse

MAX_DOWNLOAD_BYTES = DownloadsConfig().max_size_bytes


def parse_dcc_offer(text):
    return _parse(text, MAX_DOWNLOAD_BYTES)

from conftest import PUBLIC_IP, ip_int

PUB = ip_int(PUBLIC_IP)


def test_quoted_ctcp_offer():
    offer = parse_dcc_offer(f'\x01DCC SEND "My Book.epub" {PUB} 5000 1234\x01')
    assert (offer.filename, offer.ip, offer.port, offer.filesize) == ("My Book.epub", PUBLIC_IP, 5000, 1234)


def test_bare_offer_without_ctcp_wrapper():
    # Some bots omit the \x01 delimiters — still accepted
    offer = parse_dcc_offer(f"DCC SEND book.epub {PUB} 5000 1234")
    assert offer.filename == "book.epub"


@pytest.mark.parametrize("text", [
    f"hey lol DCC SEND book.epub {PUB} 5000 1234",  # must be at the start
    "just chatting",
    "DCC SEND missing fields",
])
def test_non_offers_return_none(text):
    assert parse_dcc_offer(text) is None


@pytest.mark.parametrize("ip", [
    "127.0.0.1",        # loopback
    "192.168.1.10",     # private LAN
    "10.0.0.1",
    "172.16.0.1",
    "169.254.169.254",  # link-local / cloud metadata
    "100.64.0.1",       # CGNAT
    "224.0.0.1",        # multicast — Python's is_global says True for these!
    "239.255.255.250",
    "240.0.0.1",        # reserved
    "255.255.255.255",  # broadcast
    "0.0.0.0",
])
def test_non_public_addresses_rejected(ip):
    with pytest.raises(InvalidOfferError):
        parse_dcc_offer(f"DCC SEND x {ip_int(ip)} 5000 1")


def test_ip_integer_overflow_rejected():
    with pytest.raises(InvalidOfferError):
        parse_dcc_offer(f"DCC SEND x {2**32} 5000 1")


@pytest.mark.parametrize("port", [0, 22, 1023, 65536, 70000])
def test_bad_ports_rejected(port):
    with pytest.raises(InvalidOfferError):
        parse_dcc_offer(f"DCC SEND x {PUB} {port} 1")


def test_zero_size_rejected():
    with pytest.raises(InvalidOfferError):
        parse_dcc_offer(f"DCC SEND x {PUB} 5000 0")


def test_size_at_limit_accepted():
    assert parse_dcc_offer(f"DCC SEND a.epub {PUB} 5000 {MAX_DOWNLOAD_BYTES}") is not None


@pytest.mark.parametrize("size", [MAX_DOWNLOAD_BYTES + 1, 2**32 + 5, 10**15])
def test_oversized_offer_rejected(size):
    with pytest.raises(FileTooLargeError):
        parse_dcc_offer(f"DCC SEND a.epub {PUB} 5000 {size}")


def test_all_refusals_share_download_error_base():
    # The CLI and socket catch DownloadError — every refusal must be one
    assert issubclass(InvalidOfferError, DownloadError)
    assert issubclass(FileTooLargeError, DownloadError)
