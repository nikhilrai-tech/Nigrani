SECRET_KEY = "lab-only"
DEBUG = False
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = ["django_prometheus"]
MIDDLEWARE = ["django_prometheus.middleware.PrometheusBeforeMiddleware", "django_prometheus.middleware.PrometheusAfterMiddleware"]
ROOT_URLCONF = "lab.urls"
DATABASES = {"default": {"ENGINE": "django_prometheus.db.backends.sqlite3", "NAME": "/tmp/lab.sqlite3"}}
USE_TZ = True
