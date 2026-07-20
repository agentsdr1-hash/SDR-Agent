"""
OBJ-002 Integration layer.

Existing-customer matching has to check a real system of record, not a
hardcoded list. This defines one interface (CustomerProvider) with two
implementations:

  - LocalDBCustomerProvider: reads the `customers` table in apex_pilot.db.
    This is what runs today, and it's what the seed data in db.py feeds.

  - CRMCustomerProvider: stub for the real integration (Salesforce, HubSpot,
    Dynamics...). Swap ACTIVE_PROVIDER below once you have API credentials --
    prospect_validation.py doesn't change at all when you do.
"""
from abc import ABC, abstractmethod

from app.db import get_conn


class CustomerProvider(ABC):
    @abstractmethod
    def get_customer_emails(self) -> set[str]:
        """Return the full set of existing-customer emails, lowercased."""
        ...


class LocalDBCustomerProvider(CustomerProvider):
    def get_customer_emails(self) -> set[str]:
        with get_conn() as conn:
            rows = conn.execute("SELECT email FROM customers").fetchall()
        return {r["email"].lower() for r in rows}


class CRMCustomerProvider(CustomerProvider):
    """
    Real integration target. Fill in when CRM credentials/API access exist.

    Example for Salesforce (pseudocode, uncomment and adapt):
        resp = requests.get(
            f"{SF_INSTANCE_URL}/services/data/v59.0/query",
            params={"q": "SELECT Email FROM Contact"},
            headers={"Authorization": f"Bearer {SF_ACCESS_TOKEN}"},
        )
        return {r["Email"].lower() for r in resp.json()["records"] if r.get("Email")}
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    def get_customer_emails(self) -> set[str]:
        raise NotImplementedError(
            "Wire this up to your CRM's contact/account export or query API. "
            "See docstring above for a Salesforce REST example."
        )


# Swap this line to change matching source for the whole app.
ACTIVE_PROVIDER: CustomerProvider = LocalDBCustomerProvider()
