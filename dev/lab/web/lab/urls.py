import time
from django.http import HttpResponse
from django.urls import include, path

def ok(request): return HttpResponse("ok")
def slow(request):
    time.sleep(0.4); return HttpResponse("slow")
def boom(request): raise RuntimeError("boom")

urlpatterns = [path("", include("django_prometheus.urls")), path("ok/", ok), path("slow/", slow), path("boom/", boom)]

from django.db import connection
def db(request):
    with connection.cursor() as c:
        c.execute("select 1")
    return HttpResponse("db")
urlpatterns.append(path("db/", db))
