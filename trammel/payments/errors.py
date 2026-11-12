"""What a payment provider's client raises."""


class ProviderError(Exception):
    """The provider refused or couldn't be reached; the message says what to do."""

    status: int | None = None


class BeingPaid(ProviderError):
    """A page can't be closed: the customer has paid, or is paying, through it
    and Trammel hasn't recorded it yet. It stays, and no other is made."""
