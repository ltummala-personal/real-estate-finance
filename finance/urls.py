from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("transactions/", views.transaction_list, name="transaction_list"),
    path("transactions/new/", views.transaction_create, name="transaction_create"),
    path("documents/", views.document_list, name="document_list"),
    path("documents/upload/", views.document_upload, name="document_upload"),
    path("documents/<int:pk>/", views.document_review, name="document_review"),
    path("documents/<int:pk>/download/", views.document_download, name="document_download"),
    path("reports/", views.reports, name="reports"),
]
