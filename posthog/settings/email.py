import os

from posthog.settings.access import SECRET_KEY
from posthog.settings.utils import get_list

MESSAGING_HASH_SALT: str = os.getenv("MESSAGING_HASH_SALT") or SECRET_KEY
MESSAGING_HASH_SALT_FALLBACKS: list[str] = [
    salt for salt in get_list(os.getenv("MESSAGING_HASH_SALT_FALLBACKS", "")) if salt
]

# Customer.io is the transactional email service `posthog.email` reaches for first. There is
# no key on a self-hosted install, and an empty one is exactly what tells
# `is_http_email_service_available` to fall back to SMTP. Reading an attribute that is not
# defined at all is what made every signup return 500.
CUSTOMER_IO_API_KEY: str = os.getenv("CUSTOMER_IO_API_KEY", "")
CUSTOMER_IO_API_URL: str = os.getenv("CUSTOMER_IO_API_URL", "https://api.customer.io")
