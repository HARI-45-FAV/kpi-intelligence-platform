"""What the platform will and will not accept as a password.

The floor is a length of six. It used to be ten, and the sign-in form asked for
twelve, which meant the form rejected passwords the API would have taken -- two
numbers for one rule is one number too many. Both are six now, and the number
lives in exactly one place per side: ``RegisterRequest.password`` here, and the
``minLength`` on the sign-in form.

The character-class floor is deliberately kept. It is not a length rule and was
not part of the change: a password still has to draw on two of lowercase,
uppercase, digits and symbols, so ``secret`` is refused while ``Secret1`` is
taken. Anything above the floor is accepted on its own terms -- a long
passphrase is not asked for a symbol, and there is no upper bound short of the
200 characters the column holds.

Login is *not* held to the policy. It only checks the password it is given
against the stored hash, because a tightened policy must not lock out an account
created under the old one; the wrong-password answer is the same either way.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API, register

SHORT = "Ab3"
SIX = "Ab3xy9"


def _register(client: TestClient, email: str, password: str):
    return client.post(
        f"{API}/auth/register",
        json={"email": email, "password": password, "full_name": "Pat Person"},
    )


# ---------------------------------------------------------------------------
# The length floor
# ---------------------------------------------------------------------------
def test_six_characters_is_accepted(client: TestClient):
    response = _register(client, "six@floor.example.com", SIX)
    assert response.status_code == 201, response.text
    assert response.json()["access_token"]


@pytest.mark.parametrize("password", ["", "A1", "Ab3", "Ab3x", "Ab3xy"])
def test_fewer_than_six_characters_is_refused(client: TestClient, password: str):
    response = _register(client, "short@floor.example.com", password)
    assert response.status_code == 422, response.text


def test_a_long_passphrase_needs_no_symbol(client: TestClient):
    response = _register(client, "phrase@floor.example.com", "Correct horse battery staple")
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# The character-class floor, unchanged by the length change
# ---------------------------------------------------------------------------
def test_a_single_character_class_is_refused_at_any_length(client: TestClient):
    response = _register(client, "onecase@floor.example.com", "abcdefghij")
    assert response.status_code == 422, response.text
    assert "two of" in response.text


def test_surrounding_whitespace_is_refused(client: TestClient):
    response = _register(client, "space@floor.example.com", " Ab3xy9 ")
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Login is not held to the registration policy
# ---------------------------------------------------------------------------
def test_login_accepts_a_six_character_password_it_issued(client: TestClient):
    email = "roundtrip@floor.example.com"
    register(client, email, SIX, "Pat Person")

    response = client.post(f"{API}/auth/login", json={"email": email, "password": SIX})
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


def test_login_does_not_apply_the_policy_to_a_wrong_password(client: TestClient):
    """A short guess is answered as a wrong password, not as a policy violation.

    Returning 422 here would tell an attacker their guess failed validation
    rather than authentication, and would block a legitimate account whose
    password predates the current rule.
    """
    email = "guess@floor.example.com"
    register(client, email, SIX, "Pat Person")

    response = client.post(f"{API}/auth/login", json={"email": email, "password": SHORT})
    assert response.status_code == 401, response.text
