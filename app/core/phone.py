import re


def get_restaurant_phone_error(value: str) -> str:
    phone = value.strip()
    if not phone:
        return "Restaurant phone number is required."
    digits = "".join(re.findall(r"[0-9]", phone))
    if (
        not 7 <= len(phone) <= 20
        or not 7 <= len(digits) <= 15
        or not re.fullmatch(r"\+?(?:[0-9]|\([0-9]+\))(?:[ -]?(?:[0-9]|\([0-9]+\)))*", phone)
    ):
        return "Enter 7–15 digits. A leading +, spaces, hyphens, and parentheses are allowed (20 characters max)."
    if phone.startswith("+") and digits.startswith("91") and not re.fullmatch(r"91[1-9][0-9]{9}", digits):
        return "Enter a valid +91 phone number with exactly 10 digits after the country code, starting with 1–9."
    if re.fullmatch(r"0+", digits):
        return "Enter a valid phone number; it cannot contain only zeros."
    return ""
