"""API failures remain diagnosable without serializing private exception data."""

import pytest
from simulator import api_error

import safety


def test_sdk_error_exposes_only_numeric_metadata():
    error = api_error(-2015, "api_key=synthetic-secret signature=synthetic-signature")
    text = safety.safe_error(error)
    assert "API code -2015" in text
    assert "HTTP 400" in text
    assert "synthetic-secret" not in text
    assert "synthetic-signature" not in text


@pytest.mark.parametrize("value", ["token=synthetic-secret", True, None])
def test_non_numeric_error_attributes_are_not_disclosed(value):
    error = RuntimeError("private request body: synthetic-secret")
    error.code = value
    error.status_code = value
    assert safety.safe_error(error) == ("RuntimeError: operation failed; reconcile exchange state")


def test_no_string_conversion_of_exception():
    class PrivateError(Exception):
        code = -1021
        status_code = 400

        def __str__(self):
            raise AssertionError("Exception text must never be accessed")

    assert safety.safe_error(PrivateError()) == (
        "PrivateError (API code -1021, HTTP 400): operation failed; reconcile exchange state"
    )
