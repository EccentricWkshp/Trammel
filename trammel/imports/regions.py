"""US states by name, for stores that send "Washington" where tax lookups want "WA"."""

from __future__ import annotations

US_STATES = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
    "AMERICAN SAMOA": "AS",
    "GUAM": "GU",
    "NORTHERN MARIANA ISLANDS": "MP",
    "PUERTO RICO": "PR",
    "US VIRGIN ISLANDS": "VI",
    "VIRGIN ISLANDS": "VI",
    "ARMED FORCES AMERICAS": "AA",
    "ARMED FORCES EUROPE": "AE",
    "ARMED FORCES PACIFIC": "AP",
}
_CODES = set(US_STATES.values())


def state_code(value: str | None, country: str = "US") -> str:
    """A US state as its two-letter code. Anything else (a province, an unknown
    spelling) comes back as it was, so the address keeps it."""
    text = (value or "").strip()
    if not text or (country or "US").upper() != "US":
        return text
    if len(text) == 2 and text.upper() in _CODES:
        return text.upper()
    return US_STATES.get(" ".join(text.replace(".", "").upper().split()), text)
