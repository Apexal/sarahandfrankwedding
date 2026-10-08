import re


def normalize_phone_number(phone_number: str) -> str:
    """Normalize a typed phone number to E.164-ish form.

    Keeps digits only, treats a bare 10-digit number as US, drops an international
    "00" prefix, and caps at the 15 digits E.164 allows. Blank in, blank out.
    """
    raw = (phone_number or "").strip()
    if not raw:
        return ""
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    if not has_plus and len(digits) == 10:
        digits = "1" + digits  # assume US if no country code
    digits = digits[:15]
    return f"+{digits}" if digits else ""
