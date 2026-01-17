from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.html import format_html
from django.urls import reverse
from django.utils.safestring import mark_safe
from .models import (
    User, CurrencySettings, Category, Product, Customer, 
    Sale, SaleItem, InventoryLog, DebtPayment, Receipt, AuditLog,
    SaleUSD, SaleSOS, SaleItemUSD, SaleItemSOS, DebtPaymentUSD, DebtPaymentSOS,
    DebtCorrection
)


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'get_full_name', 'phone', 'can_sell', 'can_restock', 'is_active_staff', 'date_created')
    list_filter = ('can_sell', 'can_restock', 'is_active_staff', 'is_staff', 'is_superuser', 'date_created')
    search_fields = ('username', 'first_name', 'last_name', 'email', 'phone')
    ordering = ('-date_created',)
    
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'email', 'phone')}),
        ('Permissions', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        ('Role Permissions', {
            'fields': ('can_sell', 'can_restock', 'is_active_staff'),
        }),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'password1', 'password2', 'first_name', 'last_name', 'email', 'phone'),
        }),
        ('Role Permissions', {
            'fields': ('can_sell', 'can_restock', 'is_active_staff'),
        }),
    )


@admin.register(CurrencySettings)
class CurrencySettingsAdmin(admin.ModelAdmin):
    list_display = ('usd_to_sos_rate', 'sos_to_usd_rate', 'date_updated', 'updated_by')
    readonly_fields = ('sos_to_usd_rate', 'date_updated', 'updated_by')
    fieldsets = (
        ('Exchange Rates', {
            'fields': ('usd_to_sos_rate', 'sos_to_usd_rate'),
            'description': 'USD to SOS rate is editable. SOS to USD rate is automatically calculated.'
        }),
        ('System Info', {
            'fields': ('date_updated', 'updated_by'),
            'classes': ('collapse',)
        }),
    )
    
    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'date_created', 'product_count')
    search_fields = ('name', 'description')
    ordering = ('name',)
    
    def product_count(self, obj):
        return obj.product_set.count()
    product_count.short_description = 'Products'


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'brand', 'category', 'barcode', 'selling_price', 'current_stock', 'low_stock_threshold', 'is_low_stock', 'profit_margin', 'is_active')
    list_filter = ('category', 'brand', 'is_active')
    search_fields = ('name', 'brand', 'category__name', 'barcode')
    ordering = ('name',)
    readonly_fields = ('date_added', 'date_updated', 'profit_margin', 'is_low_stock')
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'brand', 'category', 'barcode', 'is_active')
        }),
        ('Pricing (Superuser Only)', {
            'fields': ('purchase_price', 'selling_price'),
            'classes': ('collapse',)
        }),
        ('Inventory', {
            'fields': ('current_stock', 'low_stock_threshold')
        }),
        ('System Info', {
            'fields': ('date_added', 'date_updated', 'profit_margin', 'is_low_stock'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            # Hide purchase price from non-superusers
            return qs.only('id', 'name', 'brand', 'category_id', 'selling_price', 'current_stock', 'low_stock_threshold', 'date_added', 'date_updated', 'is_active')
        return qs


class DebtCorrectionInline(admin.TabularInline):
    """Inline admin for debt corrections"""
    model = DebtCorrection
    extra = 0
    readonly_fields = ('date_created', 'staff_member', 'adjustment_amount')
    fields = ('currency', 'old_debt_amount', 'new_debt_amount', 'adjustment_amount', 'reason', 'staff_member', 'date_created')
    
    def has_add_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.is_staff
    
    def has_change_permission(self, request, obj=None):
        return False  # Make corrections read-only in admin
    
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'total_debt_usd', 'total_debt_sos', 'debt_usd_equivalent', 'last_purchase_date', 'date_created', 'is_active')
    list_filter = ('is_active', 'date_created', 'last_purchase_date')
    search_fields = ('name', 'phone')
    ordering = ('-date_created',)
    readonly_fields = ('date_created', 'last_purchase_date', 'debt_usd_equivalent')
    inlines = [DebtCorrectionInline]
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'phone', 'is_active')
        }),
        ('Debt Information', {
            'fields': ('total_debt_usd', 'total_debt_sos', 'debt_usd_equivalent'),
            'description': 'Separate USD and SOS debt tracking. Use the debt correction form for manual adjustments.'
        }),
        ('System Info', {
            'fields': ('date_created', 'last_purchase_date'),
            'classes': ('collapse',)
        }),
    )
    
    def debt_usd_equivalent(self, obj):
        """Display total debt in USD equivalent"""
        return f"${obj.get_total_debt_usd_equivalent():.2f}"
    debt_usd_equivalent.short_description = 'Total Debt (USD Eq.)'
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related()
    
    def has_change_permission(self, request, obj=None):
        """Only allow superusers and staff to edit customer debt"""
        return request.user.is_superuser or request.user.is_staff


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    readonly_fields = ('total_price',)
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            # Hide purchase price from non-superusers
            return qs.select_related('product').only(
                'id', 'sale_id', 'product_id', 'quantity', 'unit_price', 'total_price',
                'product__name', 'product__brand'
            )
        return qs


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'customer', 'staff_member', 'currency', 'total_amount', 'total_in_sos', 'debt_amount', 'date_created')
    list_filter = ('currency', 'date_created', 'is_completed')
    search_fields = ('transaction_id', 'customer__name', 'customer__phone', 'staff_member__username')
    ordering = ('-date_created',)
    readonly_fields = ('transaction_id', 'debt_amount', 'date_created', 'total_in_sos', 'paid_in_sos', 'debt_in_sos')
    inlines = [SaleItemInline]
    
    fieldsets = (
        ('Transaction Info', {
            'fields': ('transaction_id', 'customer', 'staff_member', 'currency', 'exchange_rate')
        }),
        ('Amounts (Original Currency)', {
            'fields': ('total_amount', 'amount_paid', 'debt_amount'),
            'description': 'All amounts stored in original currency'
        }),
        ('Converted Amounts (SOS)', {
            'fields': ('total_in_sos', 'paid_in_sos', 'debt_in_sos'),
            'classes': ('collapse',)
        }),
        ('Status', {
            'fields': ('is_completed',)
        }),
        ('System Info', {
            'fields': ('date_created',),
            'classes': ('collapse',)
        }),
    )
    
    def total_in_sos(self, obj):
        """Display total amount in SOS"""
        return f"{obj.get_amount_in_currency('SOS'):.0f} SOS"
    total_in_sos.short_description = 'Total (SOS)'
    
    def paid_in_sos(self, obj):
        """Display paid amount in SOS"""
        return f"{obj.get_paid_amount_in_currency('SOS'):.0f} SOS"
    paid_in_sos.short_description = 'Paid (SOS)'
    
    def debt_in_sos(self, obj):
        """Display debt amount in SOS"""
        return f"{obj.get_debt_amount_in_currency('SOS'):.0f} SOS"
    debt_in_sos.short_description = 'Debt (SOS)'
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('customer', 'staff_member')


@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = ('sale', 'product', 'quantity', 'unit_price', 'total_price')
    list_filter = ('sale__currency', 'sale__date_created')
    search_fields = ('sale__transaction_id', 'product__name', 'product__brand')
    ordering = ('-sale__date_created',)
    readonly_fields = ('total_price',)
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('sale', 'product')


@admin.register(SaleUSD)
class SaleUSDAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'customer', 'total_amount', 'amount_paid', 'debt_amount', 'date_created', 'is_completed')
    list_filter = ('is_completed', 'date_created')
    search_fields = ('customer__name', 'customer__phone', 'transaction_id')
    ordering = ('-date_created',)
    readonly_fields = ('transaction_id', 'date_created')
    fieldsets = (
        ('Transaction Details', {
            'fields': ('transaction_id', 'customer', 'staff_member')
        }),
        ('USD Amounts', {
            'fields': ('total_amount', 'amount_paid', 'debt_amount'),
            'description': 'All amounts in USD'
        }),
        ('System Info', {
            'fields': ('date_created', 'is_completed'),
            'classes': ('collapse',)
        }),
    )


@admin.register(SaleSOS)
class SaleSOSAdmin(admin.ModelAdmin):
    list_display = ('transaction_id', 'customer', 'total_amount', 'amount_paid', 'debt_amount', 'date_created', 'is_completed')
    list_filter = ('is_completed', 'date_created')
    search_fields = ('customer__name', 'customer__phone', 'transaction_id')
    ordering = ('-date_created',)
    readonly_fields = ('transaction_id', 'date_created')
    fieldsets = (
        ('Transaction Details', {
            'fields': ('transaction_id', 'customer', 'staff_member')
        }),
        ('SOS Amounts', {
            'fields': ('total_amount', 'amount_paid', 'debt_amount'),
            'description': 'All amounts in SOS'
        }),
        ('System Info', {
            'fields': ('date_created', 'is_completed'),
            'classes': ('collapse',)
        }),
    )


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ('product', 'action', 'quantity_change', 'old_quantity', 'new_quantity', 'staff_member', 'date_created')
    list_filter = ('action', 'date_created', 'product__category')
    search_fields = ('product__name', 'product__brand', 'staff_member__username', 'notes')
    ordering = ('-date_created',)
    readonly_fields = ('date_created',)
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('product', 'staff_member', 'related_sale')


@admin.register(DebtPaymentUSD)
class DebtPaymentUSDAdmin(admin.ModelAdmin):
    list_display = ('customer', 'amount', 'staff_member', 'date_created')
    list_filter = ('date_created', 'staff_member')
    search_fields = ('customer__name', 'customer__phone', 'staff_member__username', 'notes')
    ordering = ('-date_created',)
    readonly_fields = ('date_created',)
    fieldsets = (
        ('Payment Information', {
            'fields': ('customer', 'amount', 'staff_member', 'notes')
        }),
        ('System Info', {
            'fields': ('date_created',),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('customer', 'staff_member')


@admin.register(DebtPaymentSOS)
class DebtPaymentSOSAdmin(admin.ModelAdmin):
    list_display = ('customer', 'amount', 'staff_member', 'date_created')
    list_filter = ('date_created', 'staff_member')
    search_fields = ('customer__name', 'customer__phone', 'staff_member__username', 'notes')
    ordering = ('-date_created',)
    readonly_fields = ('date_created',)
    fieldsets = (
        ('Payment Information', {
            'fields': ('customer', 'amount', 'staff_member', 'notes')
        }),
        ('System Info', {
            'fields': ('date_created',),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('customer', 'staff_member')


# Legacy DebtPayment admin for backward compatibility
@admin.register(DebtPayment)
class DebtPaymentAdmin(admin.ModelAdmin):
    list_display = ('customer', 'amount', 'original_currency', 'original_amount', 'amount_in_sos', 'staff_member', 'date_created')
    list_filter = ('original_currency', 'date_created', 'staff_member')
    search_fields = ('customer__name', 'customer__phone', 'staff_member__username', 'notes')
    ordering = ('-date_created',)
    readonly_fields = ('date_created', 'amount_in_sos')
    fieldsets = (
        ('Payment Information', {
            'fields': ('customer', 'amount', 'original_currency', 'original_amount', 'staff_member', 'notes')
        }),
        ('Converted Amounts', {
            'fields': ('amount_in_sos',),
            'description': 'Payment amount in SOS (converted from original currency)'
        }),
        ('System Info', {
            'fields': ('date_created',),
            'classes': ('collapse',)
        }),
    )
    
    def amount_in_sos(self, obj):
        """Display payment amount in SOS"""
        return f"{obj.get_amount_in_currency('SOS'):.0f} SOS"
    amount_in_sos.short_description = 'Amount (SOS)'
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('customer', 'staff_member')


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ('receipt_number', 'sale', 'date_created')
    list_filter = ('date_created',)
    search_fields = ('receipt_number', 'sale__transaction_id', 'sale__customer__name')
    ordering = ('-date_created',)
    readonly_fields = ('receipt_number', 'date_created')
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('sale', 'sale__customer')


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'action', 'object_type', 'object_id', 'date_created', 'ip_address')
    list_filter = ('action', 'object_type', 'date_created')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'details')
    ordering = ('-date_created',)
    readonly_fields = ('user', 'action', 'object_type', 'object_id', 'details', 'date_created', 'ip_address')
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('user')


@admin.register(DebtCorrection)
class DebtCorrectionAdmin(admin.ModelAdmin):
    list_display = ('customer', 'currency', 'old_debt_amount', 'new_debt_amount', 'adjustment_amount', 'staff_member', 'date_created')
    list_filter = ('currency', 'date_created', 'staff_member')
    search_fields = ('customer__name', 'customer__phone', 'reason', 'staff_member__username')
    ordering = ('-date_created',)
    readonly_fields = ('date_created', 'ip_address', 'adjustment_amount')
    fieldsets = (
        ('Correction Details', {
            'fields': ('customer', 'currency', 'old_debt_amount', 'new_debt_amount', 'adjustment_amount')
        }),
        ('Reason & Staff', {
            'fields': ('reason', 'staff_member')
        }),
        ('System Info', {
            'fields': ('date_created', 'ip_address'),
            'classes': ('collapse',)
        }),
    )
    
    def has_add_permission(self, request):
        return False  # Prevent adding corrections through admin
    
    def has_change_permission(self, request, obj=None):
        return False  # Make corrections read-only
    
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('customer', 'staff_member')
