from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Sum, Count, Q, F, Case, When, Value, DecimalField
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.utils import timezone
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import json

from .models import *
from .forms import *
from django.db import IntegrityError

def is_superuser(user):
    return user.is_superuser

def has_sell_permission(user):
    return user.is_superuser or user.can_sell

def has_restock_permission(user):
    return user.is_superuser or user.can_restock

def log_audit_action(user, action, object_type, object_id, details, ip_address=None):
    AuditLog.objects.create(
        user=user,
        action=action,
        object_type=object_type,
        object_id=object_id,
        details=details,
        ip_address=ip_address
    )

@login_required
def home(request):
    """Home view that redirects all staff to dashboard"""
    # All staff members land on dashboard as their primary page
    return redirect('core:dashboard')

@login_required
def dashboard_view(request):
    # Get today's date
    today = timezone.now().date()
    
    # Today's sales using new separate models
    today_usd_sales = SaleUSD.objects.filter(date_created__date=today)
    today_sos_sales = SaleSOS.objects.filter(date_created__date=today)
    today_etb_sales = SaleETB.objects.filter(date_created__date=today)
    
    # Get currency settings
    currency_settings = CurrencySettings.objects.first()
    exchange_rate = currency_settings.usd_to_sos_rate if currency_settings else Decimal('8000.00')
    
    # --- TODAY'S REVENUE CALCULATION ---
    # USD revenue
    today_revenue_usd = today_usd_sales.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    
    # SOS revenue
    today_revenue_sos = today_sos_sales.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

    # ETB revenue
    today_revenue_etb = today_etb_sales.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    
    today_revenue_sos_in_usd = Decimal('0.00')
    if currency_settings:
        if today_revenue_sos > 0:
            today_revenue_sos_in_usd = currency_settings.convert_sos_to_usd(today_revenue_sos)
            
    # Convert ETB revenue to USD for combined display (using stored exchange rate from each sale)
    today_revenue_etb_in_usd = Decimal('0.00')
    if today_revenue_etb > 0:
        for sale in today_etb_sales:
            if sale.total_amount > 0:
                # Use stored rate if available, otherwise fallback to current settings
                rate = sale.exchange_rate_at_sale if sale.exchange_rate_at_sale > 0 else (currency_settings.usd_to_etb_rate if currency_settings else Decimal('100.00'))
                if rate > 0:
                    today_revenue_etb_in_usd += sale.total_amount / rate

    # Combined revenue for dashboard display
    today_revenue = today_revenue_usd + today_revenue_sos_in_usd + today_revenue_etb_in_usd
    # --- END REVENUE CALCULATION ---

    # Today's transactions (visible to all staff)
    today_transactions = today_usd_sales.count() + today_sos_sales.count() + today_etb_sales.count()
    
    # --- TODAY'S PROFIT CALCULATION (ONLY for superusers) ---
    today_profit_total = Decimal('0.00')      # Total profit in USD from USD and SOS sales
    today_profit_usd_component = Decimal('0.00') # Profit component from USD sales only
    today_profit_sos_raw = Decimal('0.00')    # Raw profit component from SOS sales (in SOS currency)
    today_profit_sos_in_usd = Decimal('0.00') # Profit component from SOS sales converted to USD
    today_profit_etb_in_usd = Decimal('0.00') # Profit component from ETB sales converted to USD
    today_base_profit = Decimal('0.00')       # Base profit (minimum price - purchase price)
    today_premium_profit = Decimal('0.00')    # Premium profit (actual price - minimum price)

    if request.user.is_superuser:
        print(f"DEBUG: Calculating profit for {today}")
        
        # Calculate profit from USD sales
        print("DEBUG: Processing USD Sales for profit...")
        for sale in today_usd_sales:
            print(f"  - Processing USD Sale ID {sale.id}")
            for item in sale.items.select_related('product'): # items is the related_name for SaleItemUSD
                try:
                    item_profit_usd = item.get_profit_usd() # Returns Decimal profit in USD
                    item_base_profit = item.get_base_profit_usd()
                    item_premium_profit = item.get_premium_profit_usd()
                    
                    print(f"    - USD Item: {item.product.name}, Total Profit: {item_profit_usd}, Base: {item_base_profit}, Premium: {item_premium_profit}")
                    today_profit_usd_component += item_profit_usd
                    today_base_profit += item_base_profit
                    today_premium_profit += item_premium_profit
                except Exception as e:
                    print(f"ERROR calculating USD profit for {item.product.name} (SaleItemUSD ID {item.id}): {e}")
        
        # Add USD component to total
        today_profit_total += today_profit_usd_component
        print(f"DEBUG: Profit after USD sales: {today_profit_total}")

        # Calculate profit from SOS sales
        print("DEBUG: Processing SOS Sales for profit...")
        for sale in today_sos_sales:
            print(f"  - Processing SOS Sale ID {sale.id}")
            for item in sale.items.select_related('product'): # items is the related_name for SaleItemSOS
                try:
                    # get_profit_usd for SaleItemSOS already converts SOS profit to USD
                    item_profit_usd_converted = item.get_profit_usd() # Returns Decimal profit in USD
                    item_base_profit = item.get_base_profit_usd()
                    item_premium_profit = item.get_premium_profit_usd()
                    
                    print(f"    - SOS Item: {item.product.name}, Total Profit: {item_profit_usd_converted}, Base: {item_base_profit}, Premium: {item_premium_profit}")
                    today_profit_sos_in_usd += item_profit_usd_converted # Accumulate USD-converted SOS profit
                    today_base_profit += item_base_profit
                    today_premium_profit += item_premium_profit
                    # e.g., item_profit_sos_raw = item.unit_price * item.quantity - (item.product.purchase_price * currency_settings.usd_to_sos_rate) * item.quantity
                    # today_profit_sos_raw += item_profit_sos_raw 
                except Exception as e:
                    print(f"ERROR calculating SOS profit for {item.product.name} (SaleItemSOS ID {item.id}): {e}")
        
        # Add SOS component (converted to USD) to total
        today_profit_total += today_profit_sos_in_usd

        # Calculate profit from ETB sales
        print("DEBUG: Processing ETB Sales for profit...")
        for sale in today_etb_sales:
            print(f"  - Processing ETB Sale ID {sale.id}")
            for item in sale.items.select_related('product'): # items is the related_name for SaleItemETB
                try:
                    # get_profit_usd for SaleItemETB already converts ETB profit to USD
                    item_profit_usd_converted = item.get_profit_usd() # Returns Decimal profit in USD
                    item_base_profit = item.get_base_profit_usd()
                    item_premium_profit = item.get_premium_profit_usd()
                    
                    print(f"    - ETB Item: {item.product.name}, Total Profit: {item_profit_usd_converted}, Base: {item_base_profit}, Premium: {item_premium_profit}")
                    today_profit_etb_in_usd += item_profit_usd_converted # Accumulate USD-converted ETB profit
                    today_base_profit += item_base_profit
                    today_premium_profit += item_premium_profit
                except Exception as e:
                    print(f"ERROR calculating ETB profit for {item.product.name} (SaleItemETB ID {item.id}): {e}")
        
        # Add ETB component (converted to USD) to total
        today_profit_total += today_profit_etb_in_usd
        print(f"DEBUG: Final calculated today_profit_total (in USD): {today_profit_total}")
        print(f"DEBUG:   - Profit from USD sales (USD component): {today_profit_usd_component}")
        print(f"DEBUG:   - Profit from SOS sales (converted to USD): {today_profit_sos_in_usd}")
        print(f"DEBUG:   - Profit from ETB sales (converted to USD): {today_profit_etb_in_usd}")

    # --- END PROFIT CALCULATION ---

    # --- COMPREHENSIVE DEBT CALCULATION (Customer Model - Includes All Sources) ---
    currency_settings_for_debt = currency_settings # Reuse fetched settings
    exchange_rate_for_calc = Decimal(str(currency_settings_for_debt.usd_to_sos_rate)) if currency_settings_for_debt else Decimal('8000.00')

    # 1. Get total debt from Customer model (includes all sources)
    total_debt_usd = Customer.get_total_debt_usd()
    total_debt_sos = Customer.get_total_debt_sos()
    total_debt_etb = Customer.get_total_debt_etb()

    # 2. Convert SOS/ETB debt to USD for combined display
    total_debt_sos_in_usd = Decimal('0.00')
    if total_debt_sos > 0 and currency_settings_for_debt:
        total_debt_sos_in_usd = currency_settings_for_debt.convert_sos_to_usd(total_debt_sos)
        
    total_debt_etb_in_usd = Decimal('0.00')
    if total_debt_etb > 0 and currency_settings_for_debt:
        total_debt_etb_in_usd = currency_settings_for_debt.convert_etb_to_usd(total_debt_etb)

    # 3. Combined total debt in USD for dashboard display
    total_debt_usd_combined = total_debt_usd + total_debt_sos_in_usd + total_debt_etb_in_usd

    # 4. Count customers with debt
    customers_with_debt_count = Customer.get_customers_with_debt().count()
    # --- End of Comprehensive Debt Calculation ---

    # Legacy total_debt for backward compatibility (if needed elsewhere)
    # total_debt = total_debt_usd_combined # Or total_debt_usd, depending on context

    # Weekly sales data (last 7 days) - for chart
    weekly_labels = []
    weekly_data = []
    for i in range(6, -1, -1):  # Last 7 days including today
        date = today - timedelta(days=i)
        # Note: This part still uses the legacy Sale model for the chart data.
        # You might want to update this to use SaleUSD/SaleSOS if the chart should reflect new data.
        daily_sales_legacy = Sale.objects.filter(date_created__date=date) 
        daily_revenue_sos_legacy = daily_sales_legacy.aggregate(total=Sum('total_amount'))['total'] or 0
        if currency_settings and daily_revenue_sos_legacy > 0:
            daily_revenue_usd = float(currency_settings.convert_sos_to_usd(Decimal(str(daily_revenue_sos_legacy))))
        else:
            daily_revenue_usd = 0
        weekly_labels.append(date.strftime('%a'))
        weekly_data.append(daily_revenue_usd)

    # Top selling items (this week) - includes USD, SOS, and legacy sales
    week_start = today - timedelta(days=7)
    
    # Get top selling items from all three models
    from django.db.models import Q, F
    
    # Create a combined query for top selling items
    top_selling_items = Product.objects.filter(
        Q(saleitem__sale__date_created__date__gte=week_start) |  # Legacy sales
        Q(saleitemusd__sale__date_created__date__gte=week_start) |  # USD sales
        Q(saleitemsos__sale__date_created__date__gte=week_start)  # SOS sales
    ).annotate(
        # Legacy sales quantities and revenue
        legacy_quantity=Sum('saleitem__quantity'),
        legacy_revenue_usd=Sum(
            Case(
                When(saleitem__sale__currency='USD', then='saleitem__total_price'),
                default=Value(0),
                output_field=DecimalField()
            )
        ),
        legacy_revenue_sos=Sum(
            Case(
                When(saleitem__sale__currency='SOS', then='saleitem__total_price'),
                default=Value(0),
                output_field=DecimalField()
            )
        ),
        # USD sales quantities and revenue
        usd_quantity=Sum('saleitemusd__quantity'),
        usd_revenue=Sum('saleitemusd__total_price'),
        # SOS sales quantities and revenue
        sos_quantity=Sum('saleitemsos__quantity'),
        sos_revenue=Sum('saleitemsos__total_price')
    ).annotate(
        # Combined quantities and revenue
        total_quantity=Coalesce('legacy_quantity', 0, output_field=DecimalField()) + Coalesce('usd_quantity', 0, output_field=DecimalField()) + Coalesce('sos_quantity', 0, output_field=DecimalField()),
        total_revenue_usd=Coalesce('legacy_revenue_usd', 0, output_field=DecimalField()) + Coalesce('usd_revenue', 0, output_field=DecimalField()),
        total_revenue_sos=Coalesce('legacy_revenue_sos', 0, output_field=DecimalField()) + Coalesce('sos_revenue', 0, output_field=DecimalField())
    ).filter(
        total_quantity__gt=0  # Only show items that were actually sold
    ).order_by('-total_quantity')[:5]
    
    for item in top_selling_items:
        if item.total_revenue_sos and item.total_revenue_sos > 0 and currency_settings:
            item.total_revenue_sos_in_usd = currency_settings.convert_sos_to_usd(item.total_revenue_sos)
        else:
            item.total_revenue_sos_in_usd = Decimal('0.00')
        item.total_revenue_usd_combined = (item.total_revenue_usd or Decimal('0.00')) + item.total_revenue_sos_in_usd

    # Low stock products
    low_stock_products = Product.objects.filter(
        current_stock__lte=F('low_stock_threshold'),
        is_active=True
    ).order_by('current_stock')

    # Inventory summary counts
    total_products = Product.objects.filter(is_active=True).count()
    low_stock_count = low_stock_products.count()
    out_of_stock_count = Product.objects.filter(
        current_stock=0,
        is_active=True
    ).count()

    # Top debtors - from Customer model
    top_debtors = Customer.get_customers_with_debt()[:5]

    # Recent activity (last 10 sales) - includes USD, SOS, and legacy sales
    # Get recent sales from all three models and combine them
    recent_usd_sales = SaleUSD.objects.select_related('customer', 'staff_member').order_by('-date_created')[:10]
    recent_sos_sales = SaleSOS.objects.select_related('customer', 'staff_member').order_by('-date_created')[:10]
    recent_etb_sales = SaleETB.objects.select_related('customer', 'staff_member').order_by('-date_created')[:10]
    recent_legacy_sales = Sale.objects.select_related('customer', 'staff_member').order_by('-date_created')[:10]
    
    # Combine all recent sales and sort by date
    recent_activity = []
    for sale in recent_usd_sales:
        recent_activity.append({
            'id': sale.id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'total_amount': sale.total_amount,
            'total_amount_usd': sale.total_amount,  # Already in USD
            'currency': 'USD',
            'date_created': sale.date_created,
            'type': 'USD Sale'
        })
    
    for sale in recent_sos_sales:
        # Convert SOS to USD for display
        sos_in_usd = Decimal('0.00')
        if currency_settings and sale.total_amount > 0:
            sos_in_usd = currency_settings.convert_sos_to_usd(sale.total_amount)
        recent_activity.append({
            'id': sale.id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'total_amount': sale.total_amount,
            'total_amount_usd': sos_in_usd,
            'currency': 'SOS',
            'date_created': sale.date_created,
            'type': 'SOS Sale'
        })
    
    for sale in recent_etb_sales:
        # Convert ETB to USD for display
        etb_in_usd = Decimal('0.00')
        if currency_settings and sale.total_amount > 0:
            etb_in_usd = currency_settings.convert_etb_to_usd(sale.total_amount)
        recent_activity.append({
            'id': sale.id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'total_amount': sale.total_amount,
            'total_amount_usd': etb_in_usd,
            'currency': 'ETB',
            'date_created': sale.date_created,
            'type': 'ETB Sale'
        })
    
    for sale in recent_legacy_sales:
        # Convert legacy sale to USD based on currency
        legacy_in_usd = Decimal('0.00')
        if sale.currency == 'USD':
            legacy_in_usd = sale.total_amount
        elif sale.currency == 'SOS' and currency_settings:
            legacy_in_usd = currency_settings.convert_sos_to_usd(sale.total_amount)
        elif sale.currency == 'ETB' and currency_settings:
            legacy_in_usd = currency_settings.convert_etb_to_usd(sale.total_amount)
        recent_activity.append({
            'id': sale.id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'total_amount': sale.total_amount,
            'total_amount_usd': legacy_in_usd,
            'currency': sale.currency,
            'date_created': sale.date_created,
            'type': 'Legacy Sale'
        })
    
    # Sort by date and take the most recent 10
    recent_activity.sort(key=lambda x: x['date_created'], reverse=True)
    recent_activity = recent_activity[:10]

    # Categories for product creation (admin only)
    categories = Category.objects.all().order_by('name')

    # --- CONTEXT PREPARATION ---
    context = {
        'today_revenue': today_revenue,
        'today_revenue_usd': today_revenue_usd,
        'today_revenue_sos': today_revenue_sos,
        'today_revenue_etb': today_revenue_etb,
        'today_transactions': today_transactions,
        
        # --- Corrected Profit Context Variables ---
        # 'today_profit' will hold the total profit in USD
        # 'today_profit_usd' will hold the USD component for display (as float for template)
        # 'today_profit_sos' will hold the SOS component converted to USD for display (as float for template)
        'total_debt': total_debt_usd_combined,
        'total_debt_usd': total_debt_usd_combined,
        'total_debt_sos': total_debt_sos,
        'total_debt_etb': total_debt_etb,
        'total_debt_usd_only': total_debt_usd,
        'exchange_rate': exchange_rate_for_calc,
        'customers_with_debt': customers_with_debt_count,
        
        'weekly_labels': json.dumps(weekly_labels),
        'weekly_data': json.dumps(weekly_data),
        'top_selling_items': top_selling_items,
        'low_stock_products': low_stock_products,
        'top_debtors': top_debtors,
        'recent_activity': recent_activity,
        'total_products': total_products,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'categories': categories,
    }

    # Add profit to context (set to 0 for non-superusers)
    if request.user.is_superuser:
        # Convert total profit to ETB for display
        today_profit_in_etb = Decimal('0.00')
        if currency_settings and today_profit_total > 0:
            today_profit_in_etb = currency_settings.convert_usd_to_etb(Decimal(str(today_profit_total)))
        
        # Pass profit values as floats to the template context for easier handling with floatformat
        context['today_profit'] = float(today_profit_total) # Total profit in USD
        context['today_profit_in_etb'] = float(today_profit_in_etb) # Total profit converted to ETB
        context['today_profit_usd'] = float(today_profit_usd_component) # Profit from USD sales in USD
        context['today_profit_sos'] = float(today_profit_sos_in_usd) # Profit from SOS sales converted to USD
        context['today_profit_etb'] = float(today_profit_etb_in_usd) # Profit from ETB sales converted to USD
        context['today_base_profit'] = float(today_base_profit) # Base profit (minimum price - purchase price)
        context['today_premium_profit'] = float(today_premium_profit) # Premium profit (actual price - minimum price)
    else:
        # Set profit values to 0 for non-superusers (so template doesn't break)
        context['today_profit'] = 0.0
        context['today_profit_usd'] = 0.0
        context['today_profit_sos'] = 0.0
        context['today_profit_etb'] = 0.0
        context['today_base_profit'] = 0.0
        context['today_premium_profit'] = 0.0 

    print(f"DEBUG: Final context profit values sent to template:")
    print(f"  - today_profit (total USD): {context.get('today_profit')}")
    print(f"  - today_profit_usd (USD component): {context.get('today_profit_usd')}")
    print(f"  - today_profit_sos (SOS converted to USD): {context.get('today_profit_sos')}")
    print(f"  - today_profit_etb (ETB converted to USD): {context.get('today_profit_etb')}")
    
    return render(request, 'core/dashboard.html', context)

@login_required
def sales_list(request):
    if not has_sell_permission(request.user):
        messages.error(request, "Access denied. Sales permission required.")
        return redirect('core:inventory_list')
    
    # Get sales from all three models
    usd_sales = SaleUSD.objects.select_related('customer', 'staff_member').order_by('-date_created')
    sos_sales = SaleSOS.objects.select_related('customer', 'staff_member').order_by('-date_created')
    etb_sales = SaleETB.objects.select_related('customer', 'staff_member').order_by('-date_created')
    legacy_sales = Sale.objects.select_related('customer', 'staff_member').order_by('-date_created')
    
    # Search functionality
    search = request.GET.get('search', '')
    if search:
        usd_sales = usd_sales.filter(
            Q(customer__name__icontains=search) |
            Q(customer__phone__icontains=search) |
            Q(transaction_id__icontains=search)
        )
        sos_sales = sos_sales.filter(
            Q(customer__name__icontains=search) |
            Q(customer__phone__icontains=search) |
            Q(customer__phone__icontains=search) |
            Q(transaction_id__icontains=search)
        )
        etb_sales = etb_sales.filter(
            Q(customer__name__icontains=search) |
            Q(customer__phone__icontains=search) |
            Q(transaction_id__icontains=search)
        )
        legacy_sales = legacy_sales.filter(
            Q(customer__name__icontains=search) |
            Q(customer__phone__icontains=search) |
            Q(transaction_id__icontains=search)
        )
    
    # Currency filter
    currency = request.GET.get('currency', '')
    if currency == 'USD':
        sos_sales = sos_sales.none()  # Exclude SOS sales
        etb_sales = etb_sales.none()  # Exclude ETB sales
        legacy_sales = legacy_sales.filter(currency='USD')
    elif currency == 'SOS':
        usd_sales = usd_sales.none()  # Exclude USD sales
        etb_sales = etb_sales.none()  # Exclude ETB sales
        legacy_sales = legacy_sales.filter(currency='SOS')
    elif currency == 'ETB':
        usd_sales = usd_sales.none()  # Exclude USD sales
        sos_sales = sos_sales.none()  # Exclude SOS sales
        legacy_sales = legacy_sales.filter(currency='ETB')
    
    # Combine all sales into a unified list
    all_sales = []
    
    # Add USD sales
    for sale in usd_sales:
        all_sales.append({
            'id': sale.id,
            'transaction_id': sale.transaction_id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'currency': 'USD',
            'total_amount': sale.total_amount,
            'amount_paid': sale.amount_paid,
            'debt_amount': sale.debt_amount,
            'date_created': sale.date_created,
            'type': 'USD Sale'
        })
    
    # Add SOS sales
    for sale in sos_sales:
        all_sales.append({
            'id': sale.id,
            'transaction_id': sale.transaction_id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'currency': 'SOS',
            'total_amount': sale.total_amount,
            'amount_paid': sale.amount_paid,
            'debt_amount': sale.debt_amount,
            'date_created': sale.date_created,
            'type': 'SOS Sale'
        })
    
    # Add ETB sales
    for sale in etb_sales:
        all_sales.append({
            'id': sale.id,
            'transaction_id': sale.transaction_id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'currency': 'ETB',
            'total_amount': sale.total_amount,
            'amount_paid': sale.amount_paid,
            'debt_amount': sale.debt_amount,
            'date_created': sale.date_created,
            'type': 'ETB Sale'
        })
    
    # Add legacy sales
    for sale in legacy_sales:
        all_sales.append({
            'id': sale.id,
            'transaction_id': sale.transaction_id,
            'customer': sale.customer,
            'staff_member': sale.staff_member,
            'currency': sale.currency,
            'total_amount': sale.total_amount,
            'amount_paid': sale.amount_paid,
            'debt_amount': sale.debt_amount,
            'date_created': sale.date_created,
            'type': 'Legacy Sale'
        })
    
    # Sort by date (most recent first)
    all_sales.sort(key=lambda x: x['date_created'], reverse=True)
    
    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(all_sales, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'page_obj': page_obj,
        'search': search,
        'currency': currency,
    }
    
    return render(request, 'core/sales_list.html', context)

@login_required
def create_sale(request):
    if not has_sell_permission(request.user):
        messages.error(request, "Access denied. Sales permission required.")
        return redirect('core:inventory_list')
    
    if request.method == 'POST':
        try:
            print("=== STARTING SALE CREATION ===")
            # Handle both AJAX and regular form submissions
            is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            print(f"Is AJAX request: {is_ajax}")
            
            # Parse the form data
            customer_id = request.POST.get('customer')
            currency = request.POST.get('currency', 'USD')
            amount_paid_str = request.POST.get('amount_paid', '0.00')
            
            print(f"Customer ID: {customer_id}")
            print(f"Currency: {currency}")
            print(f"Amount paid: {amount_paid_str}")
            
            if not customer_id:
                raise ValueError("Customer is required")
            
            # Convert amount_paid safely
            try:
                amount_paid = Decimal(amount_paid_str)
            except (ValueError, InvalidOperation):
                amount_paid = Decimal('0.00')
            
            # Get currency settings
            currency_settings = CurrencySettings.objects.first()
            exchange_rate = currency_settings.usd_to_sos_rate if currency_settings else Decimal('8000.00')
            etb_exchange_rate = currency_settings.usd_to_etb_rate if currency_settings else Decimal('100.00')
            
            # Get customer
            customer = Customer.objects.get(id=customer_id)
            print(f"Customer found: {customer.name}")
            
            # Create sale using appropriate model based on currency
            if currency == 'USD':
                sale = SaleUSD.objects.create(
                    customer=customer,
                    staff_member=request.user,
                    amount_paid=amount_paid,
                    total_amount=Decimal('0.00'),  # Will be calculated
                    debt_amount=Decimal('0.00')    # Will be calculated
                )
            elif currency == 'SOS':  # SOS currency
                sale = SaleSOS.objects.create(
                    customer=customer,
                    staff_member=request.user,
                    amount_paid=amount_paid,
                    total_amount=Decimal('0.00'),  # Will be calculated
                    debt_amount=Decimal('0.00')    # Will be calculated
                )
            else:  # ETB currency
                sale = SaleETB.objects.create(
                    customer=customer,
                    staff_member=request.user,
                    amount_paid=amount_paid,
                    total_amount=Decimal('0.00'),  # Will be calculated
                    debt_amount=Decimal('0.00'),   # Will be calculated
                    exchange_rate_at_sale=etb_exchange_rate  # Store rate at time of sale
                )
            print(f"Sale created with ID: {sale.id}")
            
            # Process products from form data
            total_amount = Decimal('0.00')
            products_processed = []
            
            # Handle the products data from JavaScript
            product_index = 0
            while True:
                product_id_key = f'products[{product_index}][id]'
                quantity_key = f'products[{product_index}][quantity]'
                
                if product_id_key not in request.POST:
                    break
                
                product_id = request.POST[product_id_key]
                quantity_str = request.POST[quantity_key]
                
                print(f"Processing product {product_index}: ID={product_id}, Quantity={quantity_str}")
                
                if product_id and quantity_str:
                    try:
                        product = Product.objects.get(id=product_id)
                        quantity = int(quantity_str)
                        
                        if quantity > 0:
                            # CRITICAL: Check stock availability before processing
                            print(f"Product {product.name}: stock={product.current_stock}, requested={quantity}")
                            if product.current_stock < quantity:
                                raise ValueError(f"Not enough stock for {product.name}. Available: {product.current_stock}, Requested: {quantity}")
                            
                            # Determine unit price with custom pricing support
                            unit_price_key = f'products[{product_index}][unit_price]'
                            custom_unit_price = None
                            
                            # Try to get custom unit price from frontend (for both USD and SOS)
                            if unit_price_key in request.POST:
                                try:
                                    custom_unit_price = Decimal(request.POST[unit_price_key])
                                    print(f"Custom unit price provided: {custom_unit_price}")
                                except (ValueError, InvalidOperation):
                                    print(f"Invalid custom unit price, will use default")
                                    custom_unit_price = None
                            
                            # Set unit price based on currency and custom price availability
                            if currency == 'SOS':
                                if custom_unit_price is not None:
                                    # Use custom SOS price from frontend
                                    unit_price = custom_unit_price
                                else:
                                    # Fallback to USD price converted to SOS
                                    unit_price = product.selling_price * exchange_rate
                            elif currency == 'ETB':
                                if custom_unit_price is not None:
                                    # Use custom ETB price from frontend
                                    unit_price = custom_unit_price
                                else:
                                    # Fallback to USD price converted to ETB
                                    unit_price = product.selling_price * etb_exchange_rate
                            else:  # USD currency
                                if custom_unit_price is not None:
                                    # Use custom USD price from frontend
                                    unit_price = custom_unit_price
                                else:
                                    # Fallback to original selling price
                                    unit_price = product.selling_price
                            
                            # Validate unit price against purchase price (prevent selling at loss)
                            if currency == 'SOS':
                                # For SOS sales, convert purchase price to SOS for comparison
                                min_price_sos = product.purchase_price * exchange_rate
                                if unit_price < min_price_sos:
                                    raise ValueError(f"Cannot sell {product.name} at {unit_price:.0f} SOS (below purchase price of {min_price_sos:.0f} SOS). Minimum allowed price is {min_price_sos:.0f} SOS.")
                            elif currency == 'ETB':
                                # For ETB sales, convert purchase price to ETB for comparison
                                min_price_etb = product.purchase_price * etb_exchange_rate
                                if unit_price < min_price_etb:
                                    raise ValueError(f"Cannot sell {product.name} at {unit_price:.2f} ETB (below purchase price of {min_price_etb:.2f} ETB). Minimum allowed price is {min_price_etb:.2f} ETB.")
                            else:
                                # For USD sales, compare directly
                                if unit_price < product.purchase_price:
                                    raise ValueError(f"Cannot sell {product.name} at ${unit_price:.2f} USD (below purchase price of ${product.purchase_price:.2f} USD). Minimum allowed price is ${product.purchase_price:.2f} USD.")
                            
                            print(f"Final unit price for {product.name}: {unit_price} {currency}")
                            
                            total_price = unit_price * quantity
                            
                            # Create sale item using appropriate model based on currency
                            if currency == 'USD':
                                sale_item = SaleItemUSD.objects.create(
                                    sale=sale,
                                    product=product,
                                    quantity=quantity,
                                    unit_price=unit_price,
                                    total_price=total_price
                                )

                            elif currency == 'SOS':  # SOS currency
                                sale_item = SaleItemSOS.objects.create(
                                    sale=sale,
                                    product=product,
                                    quantity=quantity,
                                    unit_price=unit_price,
                                    total_price=total_price
                                )
                            else:  # ETB currency
                                sale_item = SaleItemETB.objects.create(
                                    sale=sale,
                                    product=product,
                                    quantity=quantity,
                                    unit_price=unit_price,
                                    total_price=total_price
                                )
                            print(f"SaleItem created: {sale_item.id}")
                            
                            total_amount += total_price
                            products_processed.append({
                                'product': product.name,
                                'quantity': quantity,
                                'total_price': float(total_price)
                            })
                        else:
                            print(f"Invalid quantity: {quantity}")
                    except Product.DoesNotExist:
                        print(f"Product not found: {product_id}")
                        raise ValueError(f"Product not found")
                    except ValueError as ve:
                        print(f"Value error: {ve}")
                        raise
                else:
                    print(f"Missing product data for index {product_index}")
                
                product_index += 1
            
            print(f"Total products processed: {len(products_processed)}")
            print(f"Total amount: {total_amount}")
            
            # Update sale with calculated total
            sale.total_amount = total_amount
            # debt_amount will be automatically recalculated in save() method
            sale.save()
            print(f"Sale updated with totals: total={sale.total_amount}, debt={sale.debt_amount}")
            
            # Sale amounts are now stored in original currency - no conversion needed
            print(f"Sale amounts stored in original currency: {currency}")
            
            # FIXED: Update customer debt after sale is saved
            if sale.debt_amount > 0:
                print(f"Updating customer debt: {sale.debt_amount} {currency}")
                if currency == 'USD':
                    old_debt = customer.total_debt_usd
                    customer.total_debt_usd += sale.debt_amount
                    print(f"Customer USD debt updated: {old_debt} -> {customer.total_debt_usd}")
                elif currency == 'SOS':
                    old_debt = customer.total_debt_sos
                    customer.total_debt_sos += sale.debt_amount
                    print(f"Customer SOS debt updated: {old_debt} -> {customer.total_debt_sos}")
                elif currency == 'ETB':
                    old_debt = customer.total_debt_etb
                    customer.total_debt_etb += sale.debt_amount
                    print(f"Customer ETB debt updated: {old_debt} -> {customer.total_debt_etb}")
                customer.save()
                
                # Log debt update
                log_audit_action(
                    request.user, 'DEBT_ADDED', 'Customer', customer.id,
                    f'Added debt of {sale.debt_amount} {currency} for sale #{sale.transaction_id}',
                    request.META.get('REMOTE_ADDR')
                )
            
            # FIXED: Update inventory after sale is saved
            for item in sale.items.all():
                product = item.product
                old_stock = product.current_stock
                print(f"Updating inventory for {product.name}: old stock = {old_stock}, sold = {item.quantity}")
                
                # Update product stock
                product.current_stock -= item.quantity
                product.save()
                print(f"Product {product.name}: new stock = {product.current_stock}")
                
                # Log inventory change
                log_data = {
                    'product': product,
                    'action': 'SALE',
                    'quantity_change': -item.quantity,
                    'old_quantity': old_stock,
                    'new_quantity': product.current_stock,
                    'staff_member': request.user,
                    'notes': f'Sold in Sale #{sale.transaction_id}'
                }
                
                # Set the appropriate related sale field based on currency
                if currency == 'USD':
                    log_data['related_sale_usd'] = sale
                elif currency == 'SOS':  # SOS
                    log_data['related_sale_sos'] = sale
                elif currency == 'ETB':  # ETB
                    log_data['related_sale_etb'] = sale
                
                InventoryLog.objects.create(**log_data)
                print(f"Inventory log created for {product.name}")
            
            # Calculate and update the sale total amount
            sale.calculate_total()
            print(f"Sale total calculated: ${sale.total_amount}")
            
            # Log audit action
            log_audit_action(
                request.user, 'SALE_CREATED', 'Sale', sale.id,
                f'Created sale #{sale.transaction_id} for ${sale.total_amount} with {len(products_processed)} items, Debt: ${sale.debt_amount}',
                request.META.get('REMOTE_ADDR')
            )
            
            # Return appropriate response
            success_message = f'Sale completed successfully! Transaction ID: {sale.transaction_id}'
            if is_ajax:
                print("Returning AJAX response")
                return JsonResponse({
                    'success': True,
                    'sale_id': sale.id,
                    'transaction_id': str(sale.transaction_id),
                    'message': success_message
                })
            else:
                messages.success(request, success_message)
                print("Redirecting to dashboard")
                return redirect('core:dashboard')
                
        except Exception as e:
            error_message = str(e)
            print(f"=== SALE CREATION ERROR ===")
            print(f"Error: {error_message}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            
            if is_ajax or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'error': error_message
                })
            else:
                messages.error(request, f'Error creating sale: {error_message}')
                return redirect('core:create_sale')
    
    # GET request - show the form
    print("=== LOADING SALE FORM ===")
    currency_settings = CurrencySettings.objects.first()
    
    context = {
        'currency_settings': currency_settings,
    }
    
    return render(request, 'core/create_sale.html', context)
    
@login_required
def sale_detail(request, sale_id, currency=None):
    if not has_sell_permission(request.user):
        messages.error(request, "Access denied. Sales permission required.")
        return redirect('core:inventory_list')
    
    # Try to find the sale in all three models
    sale = None
    sale_type = None
    
    # Check USD sales first
    if currency == 'USD' or currency is None:
        try:
            sale = SaleUSD.objects.select_related('customer', 'staff_member').prefetch_related('items__product').get(id=sale_id)
            sale_type = 'USD'
        except SaleUSD.DoesNotExist:
            pass
    
    # Check SOS sales if not found in USD
    if sale is None and (currency == 'SOS' or currency is None):
        try:
            sale = SaleSOS.objects.select_related('customer', 'staff_member').prefetch_related('items__product').get(id=sale_id)
            sale_type = 'SOS'
        except SaleSOS.DoesNotExist:
            pass
    
    # Check ETB sales if not found in USD or SOS
    if sale is None and (currency == 'ETB' or currency is None):
        try:
            sale = SaleETB.objects.select_related('customer', 'staff_member').prefetch_related('items__product').get(id=sale_id)
            sale_type = 'ETB'
        except SaleETB.DoesNotExist:
            pass
            
    # Check legacy sales if not found in USD, SOS, or ETB
    if sale is None and (currency == 'Legacy' or currency is None):
        try:
            sale = Sale.objects.select_related('customer', 'staff_member').prefetch_related('items__product').get(id=sale_id)
            sale_type = 'Legacy'
        except Sale.DoesNotExist:
            pass
    
    # If still not found, return 404
    if sale is None:
        from django.http import Http404
        raise Http404("Sale not found")
    
    context = {
        'sale': sale,
        'sale_type': sale_type,
        'currency': sale_type,
    }
    
    return render(request, 'core/sale_detail.html', context)

@login_required
def inventory_list(request):
    if not has_restock_permission(request.user):
        messages.error(request, "Access denied. Inventory permission required.")
        return redirect('core:sales_list')
    
    products = Product.objects.select_related('category').order_by('name')
    
    # Search functionality
    search = request.GET.get('search', '')
    if search:
        products = products.filter(
            Q(name__icontains=search) |
            Q(brand__icontains=search) |
            Q(category__name__icontains=search)
        )
    
    # Category filter
    category = request.GET.get('category', '')
    if category:
        products = products.filter(category_id=category)
    
    # Low stock filter
    low_stock = request.GET.get('low_stock', '')
    if low_stock == 'true':
        products = products.filter(current_stock__lte=F('low_stock_threshold'))
    
    # Pagination
    paginator = Paginator(products, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Categories for filter
    categories = Category.objects.all()
    
    # Get low stock products for summary
    low_stock_products = Product.objects.filter(
        current_stock__lte=F('low_stock_threshold'),
        is_active=True
    )
    
    # Get out of stock count
    out_of_stock_count = Product.objects.filter(
        current_stock=0,
        is_active=True
    ).count()
    
    context = {
        'page_obj': page_obj,
        'search': search,
        'category': category,
        'low_stock': low_stock,
        'categories': categories,
        'low_stock_products': low_stock_products,
        'out_of_stock_count': out_of_stock_count,
    }
    
    return render(request, 'core/inventory_list.html', context)

@login_required
def add_sale_item(request, sale_id):
    """
    Add an item to an existing sale
    """
    if not has_sell_permission(request.user):
        messages.error(request, "Access denied. Sales permission required.")
        return redirect('core:sales_list')
    
    sale = get_object_or_404(Sale, id=sale_id)
    
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        quantity = request.POST.get('quantity')
        
        try:
            product = get_object_or_404(Product, id=product_id)
            quantity = int(quantity)
            
            if quantity <= 0:
                messages.error(request, "Quantity must be greater than zero.")
                return redirect('core:sale_detail', sale_id=sale.id)
            
            if product.current_stock < quantity:
                messages.error(request, f"Not enough stock. Available: {product.current_stock}")
                return redirect('core:sale_detail', sale_id=sale.id)
            
            # Check if this product is already in the sale
            sale_item, created = SaleItem.objects.get_or_create(
                sale=sale,
                product=product,
                defaults={
                    'quantity': quantity,
                    'unit_price': product.selling_price,
                    'total_price': product.selling_price * quantity
                }
            )
            
            if not created:
                # If item already exists, update quantity
                sale_item.quantity += quantity
                sale_item.total_price = sale_item.unit_price * sale_item.quantity
                sale_item.save()
            
            # Update inventory
            product.current_stock -= quantity
            product.save()
            
            # Update sale total
            sale.calculate_total()
            
            # Log inventory change
            InventoryLog.objects.create(
                product=product,
                action='sale_item_added',
                quantity_change=-quantity,
                old_quantity=product.current_stock + quantity,
                new_quantity=product.current_stock,
                staff_member=request.user,
                notes=f'Added to Sale #{sale.transaction_id}'
            )
            
            # Log audit action
            log_audit_action(
                request.user, 'add_sale_item', 'SaleItem', sale_item.id,
                f'Added {quantity} x {product.name} to sale #{sale.transaction_id}',
                request.META.get('REMOTE_ADDR')
            )
            
            messages.success(request, f'Added {quantity} x {product.name} to sale successfully!')
            
        except (ValueError, Product.DoesNotExist):
            messages.error(request, "Invalid product or quantity.")
        
        return redirect('core:sale_detail', sale_id=sale.id)
    
    # For GET requests, redirect back to sale detail
    return redirect('core:sale_detail', sale_id=sale.id)

@login_required
def restock_inventory(request):
    if not has_restock_permission(request.user):
        messages.error(request, "Access denied. Inventory permission required.")
        return redirect('core:sales_list')
    
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        quantity = request.POST.get('quantity')
        notes = request.POST.get('notes', '')
        
        try:
            product = Product.objects.get(id=product_id)
            quantity = int(quantity)
            
            if quantity <= 0:
                return JsonResponse({'success': False, 'error': 'Quantity must be positive'})
            
            old_stock = product.current_stock
            product.current_stock += quantity
            product.save()
            
            # Log inventory change
            InventoryLog.objects.create(
                product=product,
                action='restock',
                quantity_change=quantity,
                old_quantity=old_stock,
                new_quantity=product.current_stock,
                staff_member=request.user,
                notes=notes
            )
            
            # Log audit action
            log_audit_action(
                request.user, 'restock', 'Product', product.id,
                f'Restocked {product.name} with {quantity} units',
                request.META.get('REMOTE_ADDR')
            )
            
            return JsonResponse({'success': True})
            
        except (Product.DoesNotExist, ValueError):
            return JsonResponse({'success': False, 'error': 'Invalid product or quantity'})
    
    # Get low stock products for quick access
    low_stock_products = Product.objects.filter(
        current_stock__lte=F('low_stock_threshold')
    ).order_by('current_stock')
    
    context = {
        'low_stock_products': low_stock_products,
    }
    
    return render(request, 'core/restock_inventory.html', context)

@login_required
def customers_list(request):
    customers = Customer.objects.all().order_by('-date_created')
    
    # Search functionality
    search = request.GET.get('search', '')
    if search:
        customers = customers.filter(
            Q(name__icontains=search) |
            Q(phone__icontains=search)
        )
    
    # Debt filter
    debt_filter = request.GET.get('debt_filter', '')
    if debt_filter == 'has_debt':
        customers = customers.filter(total_debt__gt=0)
    elif debt_filter == 'no_debt':
        customers = customers.filter(total_debt=0)
    
    # Pagination
    paginator = Paginator(customers, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Summary statistics
    total_customers = Customer.objects.count()
    customers_with_debt = Customer.get_customers_with_debt().count()
    total_debt = Customer.get_total_debt_sos()
    
    context = {
        'customers': page_obj,
        'search': search,
        'debt_filter': debt_filter,
        'total_customers': total_customers,
        'customers_with_debt': customers_with_debt,
        'total_debt': total_debt,
    }
    
    return render(request, 'core/customers_list.html', context)

@login_required
def create_customer(request):
    if request.method == 'POST':
        form = CustomerForm(request.POST)
        if form.is_valid():
            customer = form.save()
            
            # Log audit action
            log_audit_action(
                request.user, 'create_customer', 'Customer', customer.id,
                f'Created customer: {customer.name}',
                request.META.get('REMOTE_ADDR')
            )
            
            messages.success(request, f'Customer "{customer.name}" created successfully!')
            return redirect('core:customers_list')
    else:
        form = CustomerForm()
    
    context = {
        'form': form,
    }
    
    return render(request, 'core/create_customer.html', context)


@login_required
def edit_customer(request, customer_id):
    """Edit customer information - admin/staff only"""
    # Check permissions - only superusers and staff can edit customers
    if not (request.user.is_superuser or request.user.is_staff):
        messages.error(request, "Access denied. Admin or staff privileges required.")
        return redirect('core:customer_detail', customer_id=customer_id)
    
    customer = get_object_or_404(Customer, id=customer_id)
    
    if request.method == 'POST':
        form = CustomerEditForm(request.POST, instance=customer)
        if form.is_valid():
            # Store old values for audit log
            old_name = customer.name
            old_phone = customer.phone
            old_active = customer.is_active
            
            # Save the updated customer
            customer = form.save()
            
            # Log audit action with detailed changes
            changes = []
            if old_name != customer.name:
                changes.append(f"name: '{old_name}' → '{customer.name}'")
            if old_phone != customer.phone:
                changes.append(f"phone: '{old_phone}' → '{customer.phone}'")
            if old_active != customer.is_active:
                changes.append(f"active: {old_active} → {customer.is_active}")
            
            if changes:
                log_audit_action(
                    request.user, 'CUSTOMER_UPDATED', 'Customer', customer.id,
                    f'Updated customer: {", ".join(changes)}',
                    request.META.get('REMOTE_ADDR')
                )
            
            messages.success(request, f'Customer "{customer.name}" updated successfully!')
            return redirect('core:customer_detail', customer_id=customer.id)
    else:
        form = CustomerEditForm(instance=customer)
    
    context = {
        'customer': customer,
        'form': form,
    }
    
    return render(request, 'core/edit_customer.html', context)

@login_required
def debug_customer(request, customer_id):
    customer = get_object_or_404(Customer, id=customer_id)
    transactions = customer.transaction_set.all().order_by('-date')
    
    context = {
        'customer': customer,
        'transactions': transactions,
    }
    return render(request, 'core/debug_customer.html', context)

def offline_view(request):
    """
    View to render the offline fallback page.
    This page is served by the Service Worker when the user is offline
    and tries to navigate to a page that isn't cached.
    """
    return render(request, 'core/offline.html')

@login_required
def customer_detail(request, customer_id):
    try:
        print(f"Customer detail view called for customer_id: {customer_id}")
        
        customer = get_object_or_404(Customer, id=customer_id)
        print(f"Customer found: {customer.name}")
        
        # Get currency settings for conversion
        currency_settings = CurrencySettings.objects.first()
        if not currency_settings:
            currency_settings = CurrencySettings.objects.create()
        
        # Get sales from all models
        usd_sales = SaleUSD.objects.filter(customer=customer).select_related('staff_member')
        sos_sales = SaleSOS.objects.filter(customer=customer).select_related('staff_member')
        etb_sales = SaleETB.objects.filter(customer=customer).select_related('staff_member')
        legacy_sales = Sale.objects.filter(customer=customer).select_related('staff_member')
        
        # Combine sales
        all_sales_list = []
        for s in usd_sales:
            s.currency = 'USD'
            s.total_amount_usd = s.total_amount
            s.amount_paid_usd = s.amount_paid
            s.debt_amount_usd = s.debt_amount
            all_sales_list.append(s)
        for s in sos_sales:
            s.currency = 'SOS'
            s.total_amount_usd = currency_settings.convert_sos_to_usd(s.total_amount)
            s.amount_paid_usd = currency_settings.convert_sos_to_usd(s.amount_paid)
            s.debt_amount_usd = currency_settings.convert_sos_to_usd(s.debt_amount)
            all_sales_list.append(s)
        for s in etb_sales:
            s.currency = 'ETB'
            s.total_amount_usd = currency_settings.convert_etb_to_usd(s.total_amount)
            s.amount_paid_usd = currency_settings.convert_etb_to_usd(s.amount_paid)
            s.debt_amount_usd = currency_settings.convert_etb_to_usd(s.debt_amount)
            all_sales_list.append(s)
        for s in legacy_sales:
            if s.currency == 'SOS':
                s.total_amount_usd = currency_settings.convert_sos_to_usd(s.total_amount)
                s.amount_paid_usd = currency_settings.convert_sos_to_usd(s.amount_paid)
                s.debt_amount_usd = currency_settings.convert_sos_to_usd(s.debt_amount)
            elif s.currency == 'ETB':
                s.total_amount_usd = currency_settings.convert_etb_to_usd(s.total_amount)
                s.amount_paid_usd = currency_settings.convert_etb_to_usd(s.amount_paid)
                s.debt_amount_usd = currency_settings.convert_etb_to_usd(s.debt_amount)
            else:
                s.total_amount_usd = s.total_amount
                s.amount_paid_usd = s.amount_paid
                s.debt_amount_usd = s.debt_amount
            all_sales_list.append(s)
            
        # Sort sales by date
        all_sales_list.sort(key=lambda x: x.date_created, reverse=True)
        sales = all_sales_list

        # Get payments from all models (Legacy only for now? No, need to check if DebtPayment is split too)
        # Assuming DebtPayment is legacy and DebtPaymentUSD/SOS/ETB exist?
        # Checked models earlier: DebtPaymentUSD/SOS/ETB exist.
        
        usd_payments = DebtPaymentUSD.objects.filter(customer=customer)
        sos_payments = DebtPaymentSOS.objects.filter(customer=customer)
        etb_payments = DebtPaymentETB.objects.filter(customer=customer)
        legacy_payments = DebtPayment.objects.filter(customer=customer)
        
        all_payments_list = []
        for p in usd_payments:
            p.original_currency = 'USD'
            p.original_amount = p.amount
            all_payments_list.append(p)
        for p in sos_payments:
            p.original_currency = 'SOS'
            p.original_amount = p.amount
            all_payments_list.append(p)
        for p in etb_payments:
            p.original_currency = 'ETB'
            p.original_amount = p.amount
            all_payments_list.append(p)
        for p in legacy_payments:
            # Legacy payments assumed to have original_currency set or inferred
            if not hasattr(p, 'original_currency'):
                p.original_currency = 'USD' 
            p.original_amount = p.amount
            all_payments_list.append(p)
            
        all_payments_list.sort(key=lambda x: x.date_created, reverse=True)
        payments = all_payments_list
        
        print(f"Sales count: {len(sales)}")
        print(f"Payments count: {len(payments)}")
        
        # Basic calculations with error handling - CONVERT ALL TO USD
        total_spent_usd = Decimal('0.00')
        total_products_bought = 0
        total_debt_paid_usd = Decimal('0.00')
        current_debt_usd = Decimal('0.00')
        
        try:
            if sales:
                # Calculate total spent by converting each sale to USD
                for sale in sales:
                    total_spent_usd += sale.total_amount_usd
                print(f"Total spent calculated: ${total_spent_usd}")
        except Exception as e:
            print(f"Error calculating total_spent: {e}")
        
        try:
            if sales:
                for sale in sales:
                    # Aggregate items quantity manually since we can't use .aggregate on list
                    # This might be N+1 query issue but manageable for now
                    if hasattr(sale, 'items'):
                         # Use items.all() which will hit DB for each sale. Ideally prefetch.
                         # We can prefetch in the initial query.
                         total_products_bought += sum(item.quantity for item in sale.items.all())
                print(f"Total products calculated: {total_products_bought}")
        except Exception as e:
            print(f"Error calculating total_products_bought: {e}")
        
        try:
            if payments:
                # Calculate total debt paid by converting each payment to USD
                for payment in payments:
                    if payment.original_currency == 'USD':
                        total_debt_paid_usd += payment.amount
                    elif payment.original_currency == 'SOS':
                        total_debt_paid_usd += currency_settings.convert_sos_to_usd(payment.amount)
                    elif payment.original_currency == 'ETB':
                        total_debt_paid_usd += currency_settings.convert_etb_to_usd(payment.amount)
                print(f"Total debt paid calculated: ${total_debt_paid_usd}")
        except Exception as e:
            print(f"Error calculating total_debt_paid: {e}")
        
        # Calculate current debt in USD
        try:
            # Get debt from all currency fields and convert to USD
            current_debt_usd += customer.total_debt_usd
            current_debt_usd += currency_settings.convert_sos_to_usd(customer.total_debt_sos)
            current_debt_usd += currency_settings.convert_etb_to_usd(customer.total_debt_etb)
            print(f"Current debt calculated: ${current_debt_usd}")
        except Exception as e:
            print(f"Error calculating current_debt: {e}")
        
        # Simple payment frequency
        payment_frequency = "Never"
        if len(payments) > 0:
            payment_frequency = f"{len(payments)} payment(s)"
        
        # Calculate lifetime value in USD
        lifetime_value_usd = total_spent_usd + current_debt_usd
        
        # Calculate debt in both currencies for display (keeping for backward compatibility)
        current_debt_sos = customer.get_debt_in_currency('SOS')
        current_debt_usd_display = customer.get_debt_in_currency('USD')
        
        context = {
            'customer': customer,
            'sales': sales,
            'payments': payments,
            'total_spent': total_spent_usd,  # Now in USD
            'total_products_bought': total_products_bought,
            'total_debt_paid': total_debt_paid_usd,  # Now in USD
            'current_debt': current_debt_usd,  # Now in USD
            'current_debt_sos': current_debt_sos,
            'current_debt_etb': customer.total_debt_etb,
            'current_debt_usd': current_debt_usd_display,
            'payment_frequency': payment_frequency,
            'lifetime_value': lifetime_value_usd,  # Now in USD
            'sales_count': len(sales),
            'payments_count': len(payments),
        }
        
        print(f"Context created successfully. Rendering template...")
        return render(request, 'core/customer_detail.html', context)
        
    except Exception as e:
        print(f"Error in customer_detail view: {e}")
        import traceback
        traceback.print_exc()
        messages.error(request, f"Error loading customer details: {str(e)}")
        return redirect('core:customers_list')

@login_required
def record_debt_payment(request, customer_id):
    customer = get_object_or_404(Customer, id=customer_id)
    
    if request.method == 'POST':
        form = DebtPaymentForm(request.POST, customer=customer)
        if form.is_valid():
            payment = form.save(commit=False)
            payment.customer = customer
            payment.staff_member = request.user
            
            # Get currency and amount from form
            currency = form.cleaned_data.get('currency', 'USD')
            original_amount = form.cleaned_data.get('amount')
            
            # Set payment amount in original currency
            payment.amount = original_amount
            payment.original_currency = currency
            payment.original_amount = original_amount
            
            # Validate payment amount against customer debt in same currency
            if currency == 'USD':
                customer_debt = customer.total_debt_usd
            elif currency == 'SOS':
                customer_debt = customer.total_debt_sos
            elif currency == 'ETB':
                customer_debt = customer.total_debt_etb
            else:
                customer_debt = 0 # specific fallback or error
                
            if payment.amount > customer_debt:
                messages.error(request, f'Payment amount ({payment.amount} {currency}) cannot exceed total debt ({customer_debt} {currency})')
                return redirect('core:record_debt_payment', customer_id=customer.id)
            
            # Save the payment first
            payment.save()
            
            # FIXED: Update customer debt after payment is saved
            old_debt = Decimal('0.00')
            if currency == 'USD':
                old_debt = customer.total_debt_usd
                customer.total_debt_usd -= payment.amount
                # Ensure debt doesn't go negative
                if customer.total_debt_usd < 0:
                    customer.total_debt_usd = Decimal('0.00')
                print(f"Customer USD debt updated: {old_debt} -> {customer.total_debt_usd}")
            elif currency == 'SOS':
                old_debt = customer.total_debt_sos
                customer.total_debt_sos -= payment.amount
                # Ensure debt doesn't go negative
                if customer.total_debt_sos < 0:
                    customer.total_debt_sos = Decimal('0.00')
                print(f"Customer SOS debt updated: {old_debt} -> {customer.total_debt_sos}")
            elif currency == 'ETB':
                old_debt = customer.total_debt_etb
                customer.total_debt_etb -= payment.amount
                # Ensure debt doesn't go negative
                if customer.total_debt_etb < 0:
                    customer.total_debt_etb = Decimal('0.00')
                print(f"Customer ETB debt updated: {old_debt} -> {customer.total_debt_etb}")
            customer.save()
            
            # FIXED: Also update sales debt amounts for this customer
            # Apply payment to sales with debt in the same currency (oldest first)
            remaining_payment = payment.amount
            
            # Select appropriate model based on currency
            sales_model = Sale # Default to legacy
            if currency == 'USD':
                sales_model = SaleUSD
            elif currency == 'SOS':
                sales_model = SaleSOS
            elif currency == 'ETB':
                sales_model = SaleETB
                
            # Filter sales with debt (assuming field names are consistent)
            # Note: SaleUSD/SOS/ETB don't store currency field usually as it's implicit, 
            # but legacy Sale does. We need to check field existence or just filter by debt > 0.
            # New models (SaleUSD etc) don't have 'currency' field in filter usually needed if implicit.
            
            if sales_model == Sale:
                 customer_sales_with_debt = sales_model.objects.filter(
                    customer=customer,
                    debt_amount__gt=0,
                    currency=currency
                ).order_by('date_created')
            else:
                 customer_sales_with_debt = sales_model.objects.filter(
                    customer=customer,
                    debt_amount__gt=0
                ).order_by('date_created')
            
            for sale in customer_sales_with_debt:
                if remaining_payment <= 0:
                    break
                    
                if sale.debt_amount <= remaining_payment:
                    # This sale is fully paid - update amount_paid
                    sale.amount_paid += sale.debt_amount
                    remaining_payment -= sale.debt_amount
                    sale.save()  # save() method automatically recalculates debt_amount based on total_amount and amount_paid
                    print(f"Sale {sale.id} fully paid, amount_paid updated to {sale.amount_paid}, debt_amount: {sale.debt_amount}")
                else:
                    # Partial payment for this sale - update amount_paid
                    sale.amount_paid += remaining_payment
                    remaining_payment = Decimal('0.00')
                    sale.save()  # save() method automatically recalculates debt_amount based on total_amount and amount_paid
                    print(f"Sale {sale.id} partially paid, amount_paid updated to {sale.amount_paid}, debt reduced to {sale.debt_amount}")
            
            # Log the debt update with correct currency
            if currency == 'USD':
                print(f"Debt payment recorded: {old_debt} -> {customer.total_debt_usd}")
            elif currency == 'SOS':
                print(f"Debt payment recorded: {old_debt} -> {customer.total_debt_sos}")
            elif currency == 'ETB':
                print(f"Debt payment recorded: {old_debt} -> {customer.total_debt_etb}")
            
            # Get new debt amount for logging
            if currency == 'USD':
                new_debt = customer.total_debt_usd
            elif currency == 'SOS':
                new_debt = customer.total_debt_sos
            elif currency == 'ETB':
                new_debt = customer.total_debt_etb
            else:
                new_debt = Decimal('0.00')
            
            # Log audit action
            log_audit_action(
                request.user, 'DEBT_PAID', 'Customer', customer.id,
                f'Recorded payment of {payment.amount} {currency}. Debt reduced from {old_debt} to {new_debt} {currency}',
                request.META.get('REMOTE_ADDR')
            )
            
            messages.success(request, f'Payment of {payment.amount} {currency} recorded successfully! Debt reduced to {new_debt} {currency}')
            return redirect('core:customer_detail', customer_id=customer.id)
    else:
        form = DebtPaymentForm(customer=customer)
    
    # Calculate debt in both currencies for display
    current_debt_usd = customer.total_debt_usd
    current_debt_sos = customer.total_debt_sos
    
    context = {
        'customer': customer,
        'form': form,
        'current_debt_usd': current_debt_usd,
        'current_debt_sos': current_debt_sos,
        'current_debt_etb': customer.total_debt_etb,
    }
    
    return render(request, 'core/record_debt_payment.html', context)

@login_required
def correct_customer_debt(request, customer_id):
    """View for manually correcting customer debt - admin/staff only"""
    # Check permissions - only superusers and staff can correct debt
    if not (request.user.is_superuser or request.user.is_staff):
        messages.error(request, "Access denied. Admin or staff privileges required.")
        return redirect('core:customer_detail', customer_id=customer_id)
    
    customer = get_object_or_404(Customer, id=customer_id)
    
    if request.method == 'POST':
        form = DebtCorrectionForm(request.POST, customer=customer)
        if form.is_valid():
            # Get form data
            currency = form.cleaned_data['currency']
            new_debt_amount = form.cleaned_data['new_debt_amount']
            reason = form.cleaned_data['reason']
            old_debt_amount = form.cleaned_data['old_debt_amount']
            adjustment_amount = form.cleaned_data['adjustment_amount']
            
            # Create debt correction record
            debt_correction = DebtCorrection.objects.create(
                customer=customer,
                currency=currency,
                old_debt_amount=old_debt_amount,
                new_debt_amount=new_debt_amount,
                adjustment_amount=adjustment_amount,
                reason=reason,
                staff_member=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            
            # Update customer debt
            if currency == 'USD':
                customer.total_debt_usd = new_debt_amount
            elif currency == 'SOS':  # SOS
                customer.total_debt_sos = new_debt_amount
            elif currency == 'ETB':  # ETB
                customer.total_debt_etb = new_debt_amount
            
            customer.save()
            
            # Log audit action
            log_audit_action(
                request.user, 'DEBT_CORRECTED', 'Customer', customer.id,
                f'Manual debt correction: {currency} {old_debt_amount} -> {new_debt_amount} (adjustment: {adjustment_amount:+.2f}). Reason: {reason}',
                request.META.get('REMOTE_ADDR')
            )
            
            messages.success(
                request, 
                f'Debt corrected successfully! {currency} debt changed from {old_debt_amount} to {new_debt_amount} (adjustment: {adjustment_amount:+.2f})'
            )
            return redirect('core:customer_detail', customer_id=customer.id)
    else:
        form = DebtCorrectionForm(customer=customer)
    
    # Get current debt amounts for display
    current_debt_usd = customer.total_debt_usd
    current_debt_sos = customer.total_debt_sos
    
    # Get recent debt corrections for this customer
    recent_corrections = customer.debt_corrections.all()[:5]
    
    context = {
        'customer': customer,
        'form': form,
        'current_debt_usd': current_debt_usd,
        'current_debt_sos': current_debt_sos,
        'current_debt_etb': customer.total_debt_etb,
        'recent_corrections': recent_corrections,
    }
    
    return render(request, 'core/correct_customer_debt.html', context)

@login_required
def staff_management(request):
    if not is_superuser(request.user):
        messages.error(request, "Access denied. Superuser privileges required.")
        return redirect('core:dashboard')
    
    staff_members = User.objects.filter(is_staff=True).order_by('username')
    
    if request.method == 'POST':
        action = request.POST.get('action', 'add')
        
        if action == 'add':
            # Handle adding new staff member
            print(f"POST data: {request.POST}")
            form = CustomUserCreationForm(request.POST)
            print(f"Form is valid: {form.is_valid()}")
            if not form.is_valid():
                print(f"Form errors: {form.errors}")
            
            if form.is_valid():
                try:
                    user = form.save(commit=False)
                    user.set_password(form.cleaned_data['password1'])
                    user.is_staff = True  # Make sure new users are staff members
                    user.is_active = True  # Make sure new users are active
                    
                    # Set permissions from form
                    user.can_sell = form.cleaned_data.get('can_sell', False)
                    user.can_restock = form.cleaned_data.get('can_restock', False)
                    
                    print(f"About to save user: {user.username}")
                    print(f"Permissions before save: can_sell={user.can_sell}, can_restock={user.can_restock}")
                    
                    user.save()
                    
                    print(f"Staff member created successfully: {user.username}")
                    print(f"Permissions after save: can_sell={user.can_sell}, can_restock={user.can_restock}")
                    
                    # Log audit action
                    try:
                        log_audit_action(
                            request.user, 'STAFF_ADDED', 'User', user.id,
                            f'Created staff member: {user.username} with permissions: sell={user.can_sell}, restock={user.can_restock}',
                            request.META.get('REMOTE_ADDR')
                        )
                    except Exception as audit_error:
                        print(f"Audit log error: {audit_error}")
                    
                    messages.success(request, f'Staff member "{user.username}" created successfully!')
                    return redirect('core:staff_management')
                    
                except Exception as e:
                    print(f"Error creating staff member: {e}")
                    import traceback
                    traceback.print_exc()
                    messages.error(request, f'Error creating staff member: {str(e)}')
            else:
                print(f"Form errors: {form.errors}")
                for field, errors in form.errors.items():
                    for error in errors:
                        messages.error(request, f'{field}: {error}')
        
        elif action == 'edit':
            # Handle editing staff member
            try:
                staff_id = request.POST.get('staff_id')
                print(f"Editing staff ID: {staff_id}")
                print(f"Edit POST data: {request.POST}")
                
                staff = get_object_or_404(User, id=staff_id, is_staff=True)
                print(f"Found staff: {staff.username}")
                
                # Update basic information
                staff.username = request.POST.get('username', staff.username)
                staff.email = request.POST.get('email', staff.email)
                staff.first_name = request.POST.get('first_name', staff.first_name)
                staff.last_name = request.POST.get('last_name', staff.last_name)
                staff.phone = request.POST.get('phone', staff.phone)
                
                # Update password if provided
                new_password = request.POST.get('password')
                if new_password:
                    staff.set_password(new_password)
                
                # Update permissions
                staff.can_sell = 'can_sell' in request.POST
                staff.can_restock = 'can_restock' in request.POST
                staff.is_active = 'is_active' in request.POST
                
                print(f"Updated permissions: can_sell={staff.can_sell}, can_restock={staff.can_restock}, is_active={staff.is_active}")
                
                staff.save()
                
                print(f"Staff member updated successfully: {staff.username}")
                
                # Log audit action
                try:
                    log_audit_action(
                        request.user, 'STAFF_UPDATED', 'User', staff.id,
                        f'Updated staff member: {staff.username} with permissions: sell={staff.can_sell}, restock={staff.can_restock}, active={staff.is_active}',
                        request.META.get('REMOTE_ADDR')
                    )
                except Exception as audit_error:
                    print(f"Audit log error: {audit_error}")
                
                messages.success(request, f'Staff member "{staff.username}" updated successfully!')
                return redirect('core:staff_management')
                
            except Exception as e:
                print(f"Error updating staff member: {e}")
                import traceback
                traceback.print_exc()
                messages.error(request, f'Error updating staff member: {str(e)}')
        
        elif action == 'activate':
            # Handle activating staff member
            try:
                staff_id = request.POST.get('staff_id')
                staff = get_object_or_404(User, id=staff_id, is_staff=True)
                staff.is_active = True
                staff.save()
                
                # Log audit action
                log_audit_action(
                    request.user, 'STAFF_ACTIVATED', 'User', staff.id,
                    f'Activated staff member: {staff.username}',
                    request.META.get('REMOTE_ADDR')
                )
                
                messages.success(request, f'Staff member "{staff.username}" activated successfully!')
                return redirect('core:staff_management')
                
            except Exception as e:
                print(f"Error activating staff member: {e}")
                messages.error(request, f'Error activating staff member: {str(e)}')
        
        elif action == 'deactivate':
            # Handle deactivating staff member
            try:
                staff_id = request.POST.get('staff_id')
                staff = get_object_or_404(User, id=staff_id, is_staff=True)
                
                # Don't allow deactivating superusers
                if staff.is_superuser:
                    messages.error(request, "Cannot deactivate superuser accounts.")
                    return redirect('core:staff_management')
                
                staff.is_active = False
                staff.save()
                
                # Log audit action
                log_audit_action(
                    request.user, 'STAFF_DEACTIVATED', 'User', staff.id,
                    f'Deactivated staff member: {staff.username}',
                    request.META.get('REMOTE_ADDR')
                )
                
                messages.success(request, f'Staff member "{staff.username}" deactivated successfully!')
                return redirect('core:staff_management')
                
            except Exception as e:
                print(f"Error deactivating staff member: {e}")
                messages.error(request, f'Error deactivating staff member: {str(e)}')
    
    else:
        form = CustomUserCreationForm()
    
    context = {
        'staff_members': staff_members,
        'form': form,
    }
    
    return render(request, 'core/staff_management.html', context)

@login_required
def currency_settings(request):
    if not is_superuser(request.user):
        messages.error(request, "Access denied. Superuser privileges required.")
        return redirect('core:dashboard')
    
    currency_settings = CurrencySettings.objects.first()
    
    if request.method == 'POST':
        form = CurrencySettingsForm(request.POST, instance=currency_settings)
        if form.is_valid():
            settings = form.save(commit=False)
            settings.updated_by = request.user
            settings.save()
            
            # Log audit action
            log_audit_action(
                request.user, 'update_currency', 'CurrencySettings', settings.id,
                f'Updated exchange rate to {settings.usd_to_sos_rate}',
                request.META.get('REMOTE_ADDR')
            )
            
            messages.success(request, 'Currency settings updated successfully!')
            return redirect('core:currency_settings')
    else:
        form = CurrencySettingsForm(instance=currency_settings)
    
    context = {
        'form': form,
        'currency_settings': currency_settings,
    }
    
    return render(request, 'core/currency_settings.html', context)

# API Endpoints for mobile interface
@login_required
def api_search_products(request):
    query = request.GET.get('q', '').strip()
    
    if len(query) < 2:
        products = Product.objects.all()[:10]
    else:
        products = Product.objects.filter(
            Q(name__icontains=query) |
            Q(brand__icontains=query) |
            Q(category__name__icontains=query)
        )[:10]
    
    data = []
    for product in products:
        data.append({
            'id': product.id,
            'name': product.name,
            'brand': product.brand,
            'category': product.category.name,
            'selling_price': float(product.selling_price),
            'current_stock': product.current_stock,
            'low_stock_threshold': product.low_stock_threshold,
        })
    
    return JsonResponse(data, safe=False)

@login_required
def api_lookup_product_by_barcode(request):
    """Lookup a product by exact barcode string."""
    barcode = request.GET.get('barcode', '').strip()
    if not barcode:
        return JsonResponse({'success': False, 'error': 'Missing barcode'}, status=400)
    try:
        product = Product.objects.get(barcode=barcode)
        return JsonResponse({
            'success': True,
            'product': {
                'id': product.id,
                'name': product.name,
                'brand': product.brand,
                'category': product.category.name,
                'selling_price': float(product.selling_price),
                'current_stock': product.current_stock,
                'low_stock_threshold': product.low_stock_threshold,
            }
        })
    except Product.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Product not found'}, status=404)

@login_required
def api_search_customers(request):
    query = request.GET.get('q', '').strip()
    
    if len(query) < 2:
        customers = Customer.objects.all()[:10]
    else:
        customers = Customer.objects.filter(
            Q(name__icontains=query) |
            Q(phone__icontains=query)
        )[:10]
    
    data = []
    for customer in customers:
        data.append({
            'id': customer.id,
            'name': customer.name,
            'phone': customer.phone,
            'total_debt': float(customer.total_debt_sos),
            'last_purchase_date': customer.last_purchase_date.isoformat() if customer.last_purchase_date else None,
        })
    
    return JsonResponse(data, safe=False)

@login_required
def api_create_customer(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'})
    
    try:
        data = json.loads(request.body)
        name = data.get('name', '').strip()
        phone = data.get('phone', '').strip()
        
        if not name or not phone:
            return JsonResponse({'success': False, 'error': 'Name and phone are required'})
        
        # Check if phone already exists
        if Customer.objects.filter(phone=phone).exists():
            return JsonResponse({'success': False, 'error': 'Phone number already exists'})
        
        customer = Customer.objects.create(
            name=name,
            phone=phone,
        )
        
        # Log audit action
        log_audit_action(
            request.user, 'create_customer', 'Customer', customer.id,
            f'Created customer via API: {customer.name}',
            request.META.get('REMOTE_ADDR')
        )
        
        return JsonResponse({
            'success': True,
            'customer': {
                'id': customer.id,
                'name': customer.name,
                'phone': customer.phone,
            }
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def api_create_product(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'})
    
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'error': 'Permission denied'})
    
    try:
        # Get form data
        name = request.POST.get('name', '').strip()
        brand = request.POST.get('brand', '').strip()
        category_id = request.POST.get('category', '').strip()
        barcode = request.POST.get('barcode', '').strip()
        purchase_price = request.POST.get('purchase_price', '').strip()
        selling_price = request.POST.get('selling_price', '').strip()
        current_stock = request.POST.get('current_stock', '0').strip()
        low_stock_threshold = request.POST.get('low_stock_threshold', '5').strip()
        is_active = request.POST.get('is_active') == 'on'
        
        # Validate required fields
        if not all([name, brand, category_id, purchase_price, selling_price]):
            return JsonResponse({'success': False, 'error': 'All required fields must be filled'})
        
        # Validate numeric fields
        try:
            purchase_price = Decimal(purchase_price)
            selling_price = Decimal(selling_price)
            current_stock = int(current_stock)
            low_stock_threshold = int(low_stock_threshold)
        except (ValueError, InvalidOperation):
            return JsonResponse({'success': False, 'error': 'Invalid numeric values'})
        
        # Get category
        try:
            category = Category.objects.get(id=category_id)
        except Category.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Invalid category'})
        
        # Create product
        # Validate optional barcode uniqueness if provided
        if barcode:
            if Product.objects.filter(barcode=barcode).exists():
                return JsonResponse({'success': False, 'error': 'Barcode already exists for another product'}, status=400)

        # Create product (barcode optional)
        try:
            product = Product.objects.create(
            name=name,
            brand=brand,
            category=category,
            barcode=barcode or None,
            purchase_price=purchase_price,
            selling_price=selling_price,
            current_stock=current_stock,
            low_stock_threshold=low_stock_threshold,
            is_active=is_active
            )
        except IntegrityError:
            # Catch rare race where another product with same barcode was created just now
            return JsonResponse({'success': False, 'error': 'Barcode already exists for another product'}, status=400)
        
        # Log audit action
        log_audit_action(
            request.user, 'create_product', 'Product', product.id,
            f'Created product: {product.name}',
            request.META.get('REMOTE_ADDR')
        )
        
        return JsonResponse({
            'success': True,
            'product': {
                'id': product.id,
                'name': product.name,
                'brand': product.brand,
                'category': product.category.name,
                'barcode': product.barcode,
                'selling_price': float(product.selling_price),
                'current_stock': product.current_stock,
                'low_stock_threshold': product.low_stock_threshold,
            }
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def api_get_product_details(request, product_id):
    try:
        product = Product.objects.get(id=product_id)
        data = {
            'id': product.id,
            'name': product.name,
            'brand': product.brand,
            'category': product.category.name,
            'selling_price': float(product.selling_price),
            'current_stock': product.current_stock,
            'low_stock_threshold': product.low_stock_threshold,
        }
        
        # Only include purchase price for superusers
        if request.user.is_superuser:
            data['purchase_price'] = float(product.purchase_price)
        
        return JsonResponse(data)
        
    except Product.DoesNotExist:
        return JsonResponse({'error': 'Product not found'}, status=404)

@login_required
def debug_user(request):
    """Debug view to check user permissions"""
    user_info = {
        'username': request.user.username,
        'full_name': request.user.get_full_name(),
        'is_superuser': request.user.is_superuser,
        'is_staff': request.user.is_staff,
        'can_sell': request.user.can_sell,
        'can_restock': request.user.can_restock,
        'is_active_staff': request.user.is_active_staff,
        'date_joined': request.user.date_joined.isoformat(),
    }
    return JsonResponse(user_info)
@login_required
def debug_inventory(request):
    """Debug view to check inventory status"""
    if not request.user.is_superuser:
        return JsonResponse({'error': 'Superuser access required'}, status=403)
    
    products = Product.objects.all().order_by('name')
    data = []
    for product in products:
        data.append({
            'id': product.id,
            'name': product.name,
            'brand': product.brand,
            'current_stock': product.current_stock,
            'low_stock_threshold': product.low_stock_threshold,
            'is_low_stock': product.is_low_stock,
            'selling_price': float(product.selling_price),
            'purchase_price': float(product.purchase_price),
        })
    
    return JsonResponse({'products': data})

@login_required
def debug_customer(request, customer_id):
    """Debug view to check customer status and debt"""
    if not request.user.is_superuser:
        return JsonResponse({'error': 'Superuser access required'}, status=403)
    
    try:
        customer = Customer.objects.get(id=customer_id)
        sales = Sale.objects.filter(customer=customer)
        payments = DebtPayment.objects.filter(customer=customer)
        
        data = {
            'customer': {
                'id': customer.id,
                'name': customer.name,
                'phone': customer.phone,
                'total_debt': float(customer.total_debt_sos),
                'date_created': customer.date_created.isoformat(),
                'last_purchase_date': customer.last_purchase_date.isoformat() if customer.last_purchase_date else None,
            },
            'sales': [{
                'id': sale.id,
                'transaction_id': str(sale.transaction_id),
                'total_amount': float(sale.total_amount),
                'amount_paid': float(sale.amount_paid),
                'debt_amount': float(sale.debt_amount),
                'date_created': sale.date_created.isoformat(),
            } for sale in sales],
            'payments': [{
                'id': payment.id,
                'amount': float(payment.amount),
                'date_created': payment.date_created.isoformat(),
                'notes': payment.notes,
            } for payment in payments],
            'total_sales': sales.count(),
            'total_payments': payments.count(),
        }
        
        return JsonResponse(data)
        
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Customer not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required
def edit_sale(request, currency, sale_id):
    if not request.user.is_superuser:
        messages.error(request, "Access denied. Only superusers can edit sales.")
        return redirect('core:sales_list')

    sale = None
    model_class = None

    if currency == 'USD':
        model_class = SaleUSD
    elif currency == 'SOS':
        model_class = SaleSOS
    elif currency == 'ETB':
        model_class = SaleETB
    else:
        messages.error(request, "Invalid currency specified.")
        return redirect('core:sales_list')

    sale = get_object_or_404(model_class, id=sale_id)

    if request.method == 'POST':
        new_customer_id = request.POST.get('customer')
        new_staff_id = request.POST.get('staff_member')
        new_amount_paid = request.POST.get('amount_paid')

        try:
            # Handle Customer Update (and debt transfer)
            if new_customer_id and int(new_customer_id) != sale.customer.id:
                old_customer = sale.customer
                new_customer = Customer.objects.get(id=new_customer_id)
                
                # Remove debt from old customer
                old_customer.update_debt(-sale.debt_amount, currency)
                
                # Add debt to new customer
                new_customer.update_debt(sale.debt_amount, currency)
                
                sale.customer = new_customer
            
            # Update Staff
            if new_staff_id:
                sale.staff_member = User.objects.get(id=new_staff_id)
            
            # Update Amount Paid & Recalculate Debt
            if new_amount_paid:
                old_debt = sale.debt_amount
                sale.amount_paid = Decimal(new_amount_paid)
                sale.save() # save() method handles debt_amount recalculation logic

                # Sync customer total debt if amount changed
                if sale.debt_amount != old_debt:
                    debt_diff = sale.debt_amount - old_debt
                    sale.customer.update_debt(debt_diff, currency)

            sale.save()
            messages.success(request, "Sale updated successfully.")
            return redirect('core:sale_detail', sale_id=sale.id, currency=currency)

        except Exception as e:
            messages.error(request, f"Error updating sale: {e}")
            # Fallthrough to render form with errors
    
    customers = Customer.objects.all().order_by('name')
    staff_members = User.objects.all().order_by('username')

    context = {
        'sale': sale,
        'currency': currency,
        'customers': customers,
        'staff_members': staff_members,
    }
    return render(request, 'core/edit_sale.html', context)