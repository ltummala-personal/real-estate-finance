from django.contrib import admin

from .models import Account, Category, ExtractedEntry, Project, SourceDocument, Transaction


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "location", "budget")
    search_fields = ("code", "name", "location")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "institution", "number_last4", "opening_balance")
    search_fields = ("name", "institution")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "direction", "amount", "description", "counterparty", "project", "account", "category", "source")
    list_filter = ("direction", "source", "project", "account", "category", "date")
    search_fields = ("description", "counterparty", "reference")
    date_hierarchy = "date"
    autocomplete_fields = ("project", "account", "category")
    readonly_fields = ("document", "created_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


class ExtractedEntryInline(admin.TabularInline):
    model = ExtractedEntry
    extra = 0
    fields = ("status", "date", "direction", "amount", "description", "counterparty", "reference", "possible_duplicate")
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(SourceDocument)
class SourceDocumentAdmin(admin.ModelAdmin):
    list_display = ("uploaded_at", "original_name", "kind", "account", "project", "status")
    list_filter = ("kind", "status")
    search_fields = ("original_name",)
    readonly_fields = ("file", "original_name", "sha256", "status", "error", "raw_text", "uploaded_by", "uploaded_at")
    inlines = [ExtractedEntryInline]

    def has_add_permission(self, request):
        return False


@admin.register(ExtractedEntry)
class ExtractedEntryAdmin(admin.ModelAdmin):
    list_display = ("document", "status", "date", "direction", "amount", "description")
    list_filter = ("status",)
    readonly_fields = ("document", "transaction")
