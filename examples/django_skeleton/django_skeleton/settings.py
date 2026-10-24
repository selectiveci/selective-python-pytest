SECRET_KEY = "skeleton"
DEBUG = True
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "billing",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
USE_TZ = True
ROOT_URLCONF = "django_skeleton.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
