SESSION_IMPERSONATOR_USER_ID_KEY = "impersonator_user_id"

US_TIMEZONE_CHOICES = [
    ("America/New_York", "Eastern Time (US & Canada)"),
    ("America/Chicago", "Central Time (US & Canada)"),
    ("America/Denver", "Mountain Time (US & Canada)"),
    ("America/Phoenix", "Arizona Time"),
    ("America/Los_Angeles", "Pacific Time (US & Canada)"),
    ("America/Anchorage", "Alaska Time"),
    ("Pacific/Honolulu", "Hawaii Time"),
]

INTERNATIONAL_TIMEZONE_CHOICES = [
    ("UTC", "UTC"),
    ("Europe/London", "London (UK Time)"),
    ("Europe/Paris", "Central European Time (Paris, Berlin)"),
    ("Asia/Dubai", "Gulf Time (Dubai)"),
    ("Asia/Kolkata", "India Time"),
    ("Asia/Shanghai", "China Time"),
    ("Asia/Tokyo", "Japan Time"),
    ("Australia/Sydney", "Eastern Australia Time (Sydney)"),
]


def get_timezone_choices():
    """Returns grouped (timezone_name, human_friendly_label) choices.

    A curated list of common timezones rather than every IANA zone — values
    stay valid IANA names so ZoneInfo/timezone activation keeps working.
    Passed to User.time_zone as a *callable* so migrations serialize a
    reference to this function instead of the evaluated list, letting the
    curated list change without AlterField churn.
    """
    return [
        ("US & Canada", US_TIMEZONE_CHOICES),
        ("International", INTERNATIONAL_TIMEZONE_CHOICES),
    ]


COMPANY = {
    "name": "Sarah and Frank",
    "legal_name": "Sarah and Frank",
    "address": {
        "line1": "123 Example St",
        "line2": "",
        "city": "Example City",
        "state": "EX",
        "postal_code": "12345",
        "country": "US",
    },
}

WEDDING_DATE = "2027-09-18"
