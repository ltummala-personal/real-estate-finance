from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.db import connection
from django.http import HttpResponse
from django.urls import include, path


def healthz(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("finance.urls")),
]
