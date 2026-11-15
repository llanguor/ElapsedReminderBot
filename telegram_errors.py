from settings import settings


def is_gone(error):
    description = str(error).lower()
    return error.error_code == 403 or any(marker in description for marker in settings["gone_markers"])
