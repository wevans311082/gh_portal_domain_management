from apps.accounts.models import ClientProfile
from apps.domains.models import DomainContact
from apps.domains.resellerclub_client import ResellerClubClient


class DomainContactService:
    def __init__(self, client=None):
        self.client = client

    def build_default_contact(self, user, label="Primary Contact"):
        profile = ClientProfile.objects.filter(user=user).first()
        return {
            "user": user,
            "label": label,
            "name": user.full_name,
            "company": "",
            "email": user.email,
            "phone_country_code": "44",
            "phone": user.phone or "0000000000",
            "address_line1": profile.address_line1 if profile else "",
            "address_line2": profile.address_line2 if profile else "",
            "city": profile.city if profile else "",
            "state": profile.county if profile else "",
            "postcode": profile.postcode if profile else "",
            "country": profile.country if profile else "GB",
        }

    def ensure_default_contact(self, user):
        contact = DomainContact.objects.filter(user=user, is_default=True).first()
        if contact:
            return contact

        defaults = self.build_default_contact(user)
        defaults["is_default"] = True
        return DomainContact.objects.create(**defaults)

    def sync_remote_contact(self, contact, customer_id, tld: str = ""):
        if not customer_id:
            raise ValueError(
                "RESELLERCLUB_CUSTOMER_ID is not set — cannot create registrar contacts."
            )
        payload = contact.as_resellerclub_payload(customer_id=customer_id, tld=tld)
        client = self.client or ResellerClubClient()
        if contact.registrar_contact_id:
            try:
                client.update_contact(contact.registrar_contact_id, payload)
                return contact.registrar_contact_id
            except Exception:
                # Stale remote ID — fall through and create a fresh contact.
                contact.registrar_contact_id = ""
                contact.save(update_fields=["registrar_contact_id", "updated_at"])

        result = client.create_contact(payload)
        if isinstance(result, (int, float)):
            registrar_contact_id = str(int(result))
        elif isinstance(result, str) and result.strip().isdigit():
            registrar_contact_id = result.strip()
        elif isinstance(result, dict):
            registrar_contact_id = str(
                result.get("contact_id")
                or result.get("id")
                or result.get("contactid")
                or result.get("entityid")
                or ""
            ).strip()
        else:
            registrar_contact_id = ""

        if not registrar_contact_id:
            raise ValueError(
                f"ResellerClub contacts/add did not return a contact id (response={result!r}). "
                "Check API credentials, customer-id, and contact type/company fields."
            )
        contact.registrar_contact_id = registrar_contact_id
        contact.save(update_fields=["registrar_contact_id", "updated_at"])
        return registrar_contact_id
