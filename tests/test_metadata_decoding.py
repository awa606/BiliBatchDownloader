"""Offline contracts for bounded metadata decoding; all inputs are synthetic."""
from email.message import Message
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import socket

import pytest


SPEC = importlib.util.spec_from_file_location(
    "metadata_decoding_probe", Path(__file__).parents[1] / "experiments" / "video_download_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
LIMIT = 3 * 1024 * 1024
SECRET = b"SYNTHETIC_PRIVATE_BODY_COOKIE_SIGNED_URL"


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Metadata decoding attempted network or credential access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(probe.common, "load_bili_cookies", forbidden)


def headers(*encodings):
    result = Message()
    for encoding in encodings:
        result["Content-Encoding"] = encoding
    result["Content-Type"] = "text/html; charset=utf-8"
    result["X-Synthetic-Private"] = SECRET.decode()
    return result


def expected_evidence(raw, decoded, status, encoding):
    return {"status": status, "encoding": encoding,
            "raw_bytes": len(raw), "decoded_bytes": len(decoded),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "decoded_sha256": hashlib.sha256(decoded).hexdigest()}


def assert_stops(raw, response_headers, reason):
    before = list(response_headers.items())
    with pytest.raises(probe.ProbeStop) as captured:
        probe.decode_metadata(raw, response_headers)
    assert captured.value.reason == reason
    assert list(response_headers.items()) == before
    # Failures must not expose received body or arbitrary server header text.
    public_error = str(captured.value) + json.dumps(captured.value.evidence)
    assert SECRET.decode() not in public_error
    return captured.value


@pytest.mark.parametrize("encoding,label", [(None, "MISSING"), ("identity", "IDENTITY"),
                                           ("IDENTITY", "IDENTITY"), (" \tIdEnTiTy\t ", "IDENTITY")])
def test_uncompressed_metadata_passes_through_with_exact_private_safe_evidence(encoding, label):
    original = headers() if encoding is None else headers(encoding)
    original["Content-Length"] = "999"
    original["Transfer-Encoding"] = "chunked"
    before = list(original.items())
    decoded, copied, evidence = probe.decode_metadata(SECRET, original)
    assert decoded == SECRET
    assert isinstance(copied, Message) and copied is not original
    assert list(copied.items()) == list(original.items()) == before
    assert evidence == expected_evidence(SECRET, SECRET, "PASS_THROUGH", label)
    assert SECRET.decode() not in json.dumps(evidence)


@pytest.mark.parametrize("encoding", ["gzip", "GZIP", "gZiP", " \tgzip\t "])
def test_single_gzip_decodes_and_replaces_only_transfer_related_headers(encoding):
    raw = gzip.compress(SECRET, mtime=0)
    original = headers(encoding)
    original["Content-Length"] = str(len(raw))
    original["content-length"] = "123456"
    original["Transfer-Encoding"] = "chunked"
    original["transfer-encoding"] = "another-synthetic-value"
    original["X-Keep-Duplicate"] = "first"
    original["X-Keep-Duplicate"] = "second"
    before = list(original.items())
    decoded, copied, evidence = probe.decode_metadata(raw, original)
    assert decoded == SECRET
    assert isinstance(copied, Message) and copied is not original
    assert list(original.items()) == before
    assert copied.get_all("Content-Encoding") is None
    assert copied.get_all("Transfer-Encoding") is None
    assert copied.get_all("Content-Length") == [str(len(SECRET))]
    assert copied.get_all("X-Keep-Duplicate") == ["first", "second"]
    assert copied["Content-Type"] == original["Content-Type"]
    assert copied["X-Synthetic-Private"] == original["X-Synthetic-Private"]
    assert evidence == expected_evidence(raw, SECRET, "DECODED", "GZIP")
    assert SECRET.decode() not in json.dumps(evidence)


def test_empty_gzip_is_a_complete_valid_member():
    raw = gzip.compress(b"", mtime=0)
    decoded, copied, evidence = probe.decode_metadata(raw, headers("gzip"))
    assert decoded == b"" and copied["Content-Length"] == "0"
    assert evidence == expected_evidence(raw, b"", "DECODED", "GZIP")


@pytest.mark.parametrize("encoding", [None, "identity"])
def test_missing_or_identity_declaration_does_not_guess_gzip(encoding):
    raw = gzip.compress(SECRET, mtime=0)
    original = headers() if encoding is None else headers(encoding)
    decoded, copied, evidence = probe.decode_metadata(raw, original)
    assert decoded == raw
    assert list(copied.items()) == list(original.items())
    assert evidence == expected_evidence(raw, raw, "PASS_THROUGH", "MISSING" if encoding is None else "IDENTITY")


@pytest.mark.parametrize("encoding", [None, "identity"])
def test_raw_exact_three_mib_is_accepted(encoding):
    assert probe.MAX_METADATA_BYTES == LIMIT
    raw = b"x" * LIMIT
    original = headers() if encoding is None else headers(encoding)
    decoded, _, evidence = probe.decode_metadata(raw, original)
    assert decoded == raw
    assert evidence["raw_bytes"] == evidence["decoded_bytes"] == LIMIT


@pytest.mark.parametrize("encoding", [None, "identity", "gzip"])
def test_raw_over_three_mib_stops_before_decoding(encoding):
    original = headers() if encoding is None else headers(encoding)
    assert_stops(b"x" * (LIMIT + 1), original, "metadata_size_limit")


def test_gzip_decoded_exact_three_mib_is_accepted():
    expected = b"x" * LIMIT
    raw = gzip.compress(expected, mtime=0)
    decoded, copied, evidence = probe.decode_metadata(raw, headers("gzip"))
    assert decoded == expected and copied["Content-Length"] == str(LIMIT)
    assert evidence == expected_evidence(raw, expected, "DECODED", "GZIP")


def test_gzip_decoded_one_byte_over_three_mib_stops():
    raw = gzip.compress(b"x" * (LIMIT + 1), mtime=0)
    assert len(raw) < LIMIT
    assert_stops(raw, headers("gzip"), "metadata_decoded_size_limit")


@pytest.mark.parametrize("offset", [-8, -4])
def test_gzip_bad_crc_or_isize_is_invalid(offset):
    raw = bytearray(gzip.compress(SECRET, mtime=0))
    raw[offset] ^= 1
    assert_stops(bytes(raw), headers("gzip"), "metadata_gzip_invalid")


@pytest.mark.parametrize("body", [b"plain text declared gzip", b"\x78\x9cnot-a-gzip-stream"])
def test_non_gzip_data_with_gzip_declaration_is_invalid(body):
    assert_stops(body, headers("gzip"), "metadata_gzip_invalid")


@pytest.mark.parametrize("slice_end", [0, 1, 9, -8, -1])
def test_truncated_gzip_cannot_be_completed_by_flush(slice_end):
    raw = gzip.compress(SECRET, mtime=0)[:slice_end]
    assert_stops(raw, headers("gzip"), "metadata_gzip_incomplete")


@pytest.mark.parametrize("tail", [b"\x00", b"\x00\x00", SECRET,
                                  gzip.compress(b"", mtime=0), gzip.compress(SECRET, mtime=0)])
def test_gzip_trailing_bytes_or_additional_members_are_rejected(tail):
    raw = gzip.compress(SECRET, mtime=0) + tail
    assert_stops(raw, headers("gzip"), "metadata_gzip_trailing_data")


@pytest.mark.parametrize("encoding", ["", " ", "br", "deflate", "compress", "gzip, identity",
                                      "identity, gzip", "gzip, gzip", "gzip; q=1",
                                      "\ngzip", "gzip\r", "\vGZIP", "\u00a0gzip", "GZ\u0130P"])
def test_unknown_multiple_or_non_ascii_ows_encoding_is_not_accepted(encoding):
    assert_stops(gzip.compress(SECRET, mtime=0), headers(encoding), "content_encoding_unsupported")


@pytest.mark.parametrize("encodings", [("gzip", "gzip"), ("identity", "identity"),
                                       ("gzip", "identity"), ("", "gzip")])
def test_duplicate_encoding_fields_are_rejected_even_if_values_match(encodings):
    original = headers()
    original["Content-Encoding"] = encodings[0]
    original["cOnTeNt-EnCoDiNg"] = encodings[1]
    assert_stops(gzip.compress(SECRET, mtime=0), original, "content_encoding_unsupported")


def test_private_encoding_value_is_never_in_failure_evidence():
    assert_stops(SECRET, headers(SECRET.decode()), "content_encoding_unsupported")
