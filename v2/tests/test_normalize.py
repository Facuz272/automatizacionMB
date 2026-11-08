import pytest

from lead_engine.normalize import (
    address_problem, best_email, clean_email, extract_domain, is_company_site, phone_key, to_e164,
)


@pytest.mark.parametrize("url,expected", [
    ("https://www.web.com/x", "web.com"),            # v1 lstrip bug gave "eb.com"
    ("http://www.wwwidgets.com", "wwwidgets.com"),    # v1 gave "idgets.com"
    ("wavecondo.com", "wavecondo.com"),
    ("https://WWW.Example.ORG.", "example.org"),
    ("", ""),
    ("not a url", ""),
])
def test_extract_domain(url, expected):
    assert extract_domain(url) == expected


def test_social_is_not_company_site():
    assert not is_company_site("https://www.facebook.com/condo")
    assert not is_company_site("https://linktr.ee/x")
    assert is_company_site("https://oceanvillas.org")


@pytest.mark.parametrize("raw,expected", [
    ("(305) 834-2218", "+13058342218"),
    ("+1 305-834-2218", "+13058342218"),
    ("1.305.834.2218", "+13058342218"),
    ("305-834-2218 ext. 12", "+13058342218"),
    ("305-834-2218 x4", "+13058342218"),
    ("834-2218", None),
    ("(105) 834-2218", None),     # invalid area code
    ("(305) 555-0123", None),     # fictional range
    ("+44 20 7946 0958", None),
    (None, None),
])
def test_to_e164(raw, expected):
    assert to_e164(raw) == expected


def test_phone_key_matches_crm_rule():
    assert phone_key("+13058342218") == "3058342218"


@pytest.mark.parametrize("addr,problem", [
    ("1500 Bay Rd, Miami Beach, FL 33139", None),
    ("1500 Bay Rd, Miami Beach, FL 33139, USA", None),
    ("18555 Collins Ave, Sunny Isles Beach, FL 33160", None),
    ("Miami, FL", "dirección sin número de calle"),
    ("Miami Beach, FL 33139", "dirección sin número de calle"),
    ("info@condo.com", "dirección contiene @"),
    ("PO Box 1234, Miami, FL 33101", "dirección es un P.O. Box"),
    ("123 Main St, Atlanta, GA 30301", "dirección fuera de FL"),
    ("", "sin dirección"),
])
def test_address_problem(addr, problem):
    assert address_problem(addr, "FL") == problem


def test_clean_email():
    assert clean_email("mailto:Manager@OceanVillas.org?subject=hi") == "manager@oceanvillas.org"
    assert clean_email("noreply@x.com") is None
    assert clean_email("logo@2x.png") is None
    assert clean_email("user@example.com") is None


def test_best_email_prefers_person_then_role_on_company_domain():
    emails = {"info@oceanvillas.org", "maria.perez@oceanvillas.org", "someone@gmail.com"}
    assert best_email(emails, "oceanvillas.org", "Maria Perez") == "maria.perez@oceanvillas.org"
    assert best_email({"info@oceanvillas.org", "manager@oceanvillas.org"}, "oceanvillas.org") == "manager@oceanvillas.org"
