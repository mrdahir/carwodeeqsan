from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Sum, Count, Q, F, Case, When, Value, DecimalField, ExpressionWrapper
from django.db.models.functions import Coalesce, NullIf
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

import json

from .models import *
from .forms import *
from django.db import IntegrityError

def superuser_required(view_func):
    """Decorator that requires user to be authenticated and superuser"""
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, "Authentication required.")
            return redirect('admin:login')
        if not request.user.is_superuser:
            messages.error(request, "Superuser privileges required.")
            return redirect('core:dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def log_audit_action(user, action, object_type, object_id, details, ip_address=None):
    """Log audit action - user can be None for anonymous operations"""
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
    """Home view that redirects all admins to dashboard"""
    return redirect('core:dashboard')

@login_required
@login_required
def dashboard_view(request):
    # Get today's date
    today = timezone.now().date()
    
    # Get currency settings
    currency_settings = CurrencySettings.objects.first()
    # Default rates if settings missing
    usd_to_sos_rate = currency_settings.usd_to_sos_rate if currency_settings else Decimal('8000.00')
    usd_to_etb_rate = currency_settings.usd_to_etb_rate if currency_settings else Decimal('100.00')

    # --- REVENUE CALCULATION (ETB BASE) ---
    today_revenue_etb_total = Decimal('0.00')
    
    # 1. USD Sales -> ETB
    today_revenue_usd = SaleUSD.objects.filter(date_created__date=today).aggregate(
        total=Coalesce(Sum('total_amount'), Value(Decimal('0.00')))
    )['total']
    
    # 2. SOS Sales -> ETB (SOS -> USD -> ETB)
    today_revenue_sos = SaleSOS.objects.filter(date_created__date=today).aggregate(
        total=Coalesce(Sum('total_amount'), Value(Decimal('0.00')))
    )['total']
    
    # 3. ETB Sales (Already ETB)
    today_revenue_etb = SaleETB.objects.filter(date_created__date=today).aggregate(
        total=Coalesce(Sum('total_amount'), Value(Decimal('0.00')))
    )['total']
    
    # Conversions
    revenue_usd_in_etb = today_revenue_usd * usd_to_etb_rate
    revenue_sos_in_etb = Decimal('0.00')
    if usd_to_sos_rate > 0:
        # Convert SOS to USD first, then TO ETB
        revenue_sos_in_etb = (today_revenue_sos / usd_to_sos_rate) * usd_to_etb_rate
        
    today_revenue_etb_total = revenue_usd_in_etb + revenue_sos_in_etb + today_revenue_etb

    # Transaction Counts
    today_transactions = (
        SaleUSD.objects.filter(date_created__date=today).count() +
        SaleSOS.objects.filter(date_created__date=today).count() +
        SaleETB.objects.filter(date_created__date=today).count()
    )

    # --- PROFIT CALCULATION (Superuser Only) ---
    today_profit_in_etb = Decimal('0.00')
    today_base_profit = Decimal('0.00')
    today_premium_profit = Decimal('0.00')
    
    if request.user.is_superuser:
        # Helper to calculate profit for a queryset
        def calculate_profit(sale_items, currency_type):
            base_p = Decimal('0.00')
            premium_p = Decimal('0.00')
            
            for item in sale_items:
                qty = item.quantity
                product_cost_usd = item.product.purchase_price
                product_sell_usd = item.product.selling_price
                
                # Base Profit = (Selling Price - Cost) * Qty (Always calculated in USD first)
                item_base_profit_usd = (product_sell_usd - product_cost_usd) * qty
                
                # Premium Profit = (Actual Sale Price - Selling Price) * Qty
                # We need actual unit price in USD
                actual_unit_price_usd = Decimal('0.00')
                
                if currency_type == 'USD':
                    actual_unit_price_usd = item.unit_price
                elif currency_type == 'SOS':
                    if usd_to_sos_rate > 0:
                        actual_unit_price_usd = item.unit_price / usd_to_sos_rate
                elif currency_type == 'ETB':
                    # Use stored rate if available, else current
                    rate = item.sale.exchange_rate_at_sale if item.sale.exchange_rate_at_sale else usd_to_etb_rate
                    if rate > 0:
                        actual_unit_price_usd = item.unit_price / rate

                item_premium_profit_usd = (actual_unit_price_usd - product_sell_usd) * qty
                
                base_p += item_base_profit_usd
                premium_p += item_premium_profit_usd
                
            return base_p, premium_p

        # Calculate for all 3 types
        # Note: optimizing with aggregate is harder due to currency logic, iterating today's items is likely fine for performance volume
        
        # USD Items
        items_usd = SaleItemUSD.objects.filter(sale__date_created__date=today).select_related('product', 'sale')
        base_usd, prem_usd = calculate_profit(items_usd, 'USD')
        
        # SOS Items
        items_sos = SaleItemSOS.objects.filter(sale__date_created__date=today).select_related('product', 'sale')
        base_sos, prem_sos = calculate_profit(items_sos, 'SOS')
        
        # ETB Items
        items_etb = SaleItemETB.objects.filter(sale__date_created__date=today).select_related('product', 'sale')
        base_etb, prem_etb = calculate_profit(items_etb, 'ETB')
        
        # Sum USD Profits
        total_base_profit_usd = base_usd + base_sos + base_etb
        total_premium_profit_usd = prem_usd + prem_sos + prem_etb
        
        # Convert to ETB for display
        today_base_profit = total_base_profit_usd * usd_to_etb_rate
        today_premium_profit = total_premium_profit_usd * usd_to_etb_rate
        today_profit_in_etb = today_base_profit + today_premium_profit

    # --- DEBT CALCULATION (ETB Centric) ---
    total_debt_usd = Customer.get_total_debt_usd()
    total_debt_sos = Customer.get_total_debt_sos()
    total_debt_etb = Customer.get_total_debt_etb()
    
    # Convert all to ETB
    debt_usd_in_etb = total_debt_usd * usd_to_etb_rate
    debt_sos_in_etb = Decimal('0.00')
    if usd_to_sos_rate > 0:
        debt_sos_in_etb = (total_debt_sos / usd_to_sos_rate) * usd_to_etb_rate
        
    total_debt_combined_etb = debt_usd_in_etb + debt_sos_in_etb + total_debt_etb
    top_debtors = Customer.get_customers_with_debt()[:5]

    # --- WEEKLY SALES CHART (ETB) ---
    weekly_labels = []
    weekly_data = [] # in ETB
    
    for i in range(6, -1, -1):
        date = today - timedelta(days=i)
        
        # 1. USD -> ETB
        day_usd = SaleUSD.objects.filter(date_created__date=date).aggregate(
            total=Coalesce(Sum('total_amount'), Value(Decimal('0.00')))
        )['total']
        val_usd_in_etb = day_usd * usd_to_etb_rate
        
        # 2. SOS -> USD -> ETB
        day_sos = SaleSOS.objects.filter(date_created__date=date).aggregate(
            total=Coalesce(Sum('total_amount'), Value(Decimal('0.00')))
        )['total']
        val_sos_in_etb = Decimal('0.00')
        if usd_to_sos_rate > 0:
            val_sos_in_etb = (day_sos / usd_to_sos_rate) * usd_to_etb_rate
            
        # 3. ETB (Native)
        day_etb = SaleETB.objects.filter(date_created__date=date).aggregate(
            total=Coalesce(Sum('total_amount'), Value(Decimal('0.00')))
        )['total']
        
        total_day_etb = val_usd_in_etb + val_sos_in_etb + day_etb
        
        weekly_labels.append(date.strftime('%a'))
        weekly_data.append(float(total_day_etb))

    # --- TOP SELLING PRODUCTS & RECENT ACTIVITY ---
    # Top Items by QUANTITY with ACTUAL sale prices (not current product prices)
    week_start = today - timedelta(days=7)
    
    # Calculate revenue from actual sale items, not product prices
    # This ensures accuracy when product prices change
    top_selling_items_data = []
    
    # Get all sale items from the past week
    usd_items = SaleItemUSD.objects.filter(
        sale__date_created__date__gte=week_start
    ).select_related('product', 'sale')
    
    sos_items = SaleItemSOS.objects.filter(
        sale__date_created__date__gte=week_start
    ).select_related('product', 'sale')
    
    etb_items = SaleItemETB.objects.filter(
        sale__date_created__date__gte=week_start
    ).select_related('product', 'sale')
    
    # Aggregate by product
    product_revenue = {}
    
    # Process USD items
    for item in usd_items:
        product_id = item.product.id
        if product_id not in product_revenue:
            product_revenue[product_id] = {
                'product': item.product,
                'total_qty': Decimal('0'),
                'total_revenue_usd': Decimal('0'),
            }
        product_revenue[product_id]['total_qty'] += item.quantity
        # Use actual sale price, not current product price
        product_revenue[product_id]['total_revenue_usd'] += item.total_price
    
    # Process SOS items
    for item in sos_items:
        product_id = item.product.id
        if product_id not in product_revenue:
            product_revenue[product_id] = {
                'product': item.product,
                'total_qty': Decimal('0'),
                'total_revenue_usd': Decimal('0'),
            }
        product_revenue[product_id]['total_qty'] += item.quantity
        # Convert SOS to USD, then to ETB
        if usd_to_sos_rate > 0:
            revenue_usd = item.total_price / usd_to_sos_rate
            product_revenue[product_id]['total_revenue_usd'] += revenue_usd
    
    # Process ETB items
    for item in etb_items:
        product_id = item.product.id
        if product_id not in product_revenue:
            product_revenue[product_id] = {
                'product': item.product,
                'total_qty': Decimal('0'),
                'total_revenue_usd': Decimal('0'),
            }
        product_revenue[product_id]['total_qty'] += item.quantity
        # Convert ETB to USD using stored rate or current rate
        rate = item.sale.exchange_rate_at_sale if item.sale.exchange_rate_at_sale else usd_to_etb_rate
        if rate > 0:
            revenue_usd = item.total_price / rate
            product_revenue[product_id]['total_revenue_usd'] += revenue_usd
    
    # Convert to list and calculate ETB revenue
    for product_id, data in product_revenue.items():
        data['total_revenue_etb'] = data['total_revenue_usd'] * usd_to_etb_rate
        # Add product name for template compatibility
        data['name'] = data['product'].name
        top_selling_items_data.append(data)
    
    # Sort by quantity and take top 5
    top_selling_items_data.sort(key=lambda x: x['total_qty'], reverse=True)
    top_selling_items = top_selling_items_data[:5]

    # Recent Activity (Normalized to ETB)
    recent_activity = []
    
    def add_recent(queryset, currency, conversion_func):
        for sale in queryset:
            val_etb = conversion_func(sale)
            recent_activity.append({
                'id': sale.id,
                'customer': sale.customer if sale.customer else "Walk-in Customer",
                'user': sale.user,
                'amount_etb': val_etb,
                'original_amount': sale.total_amount,
                'currency': currency,
                'date_created': sale.date_created,
                'is_paid': sale.is_completed # Simplify status
            })

    # USD Sales
    add_recent(SaleUSD.objects.select_related('customer', 'user').order_by('-date_created')[:10], 'USD', 
               lambda s: s.total_amount * usd_to_etb_rate)
    # SOS Sales
    add_recent(SaleSOS.objects.select_related('customer', 'user').order_by('-date_created')[:10], 'SOS', 
               lambda s: (s.total_amount / usd_to_sos_rate * usd_to_etb_rate) if usd_to_sos_rate > 0 else 0)
    # ETB Sales
    add_recent(SaleETB.objects.select_related('customer', 'user').order_by('-date_created')[:10], 'ETB', 
               lambda s: s.total_amount) # Already ETB

    recent_activity.sort(key=lambda x: x['date_created'], reverse=True)
    recent_activity = recent_activity[:10]

    # Inventory Counts
    low_stock_products = Product.objects.filter(current_stock__lte=F('low_stock_threshold'), is_active=True).order_by('current_stock')
    total_products = Product.objects.filter(is_active=True).count()
    low_stock_count = low_stock_products.count()
    out_of_stock_count = Product.objects.filter(current_stock=0, is_active=True).count()
    categories = Category.objects.all().order_by('name')

    context = {
        # Revenue
        'today_revenue_etb': today_revenue_etb_total,
        'today_transactions': today_transactions,
        
        # Debt
        'total_debt_etb': total_debt_combined_etb,
        'customers_with_debt': Customer.get_customers_with_debt().count(),
        
        # Charts & Lists
        'weekly_labels': weekly_labels,
        'weekly_data': weekly_data, # Now in ETB
        'top_selling_items': top_selling_items,
        'recent_activity': recent_activity,
        'top_debtors': top_debtors,
        
        # Inventory
        'total_products': total_products,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'low_stock_products': low_stock_products,
        'categories': categories,
        
        # Settings
        'exchange_rate': usd_to_sos_rate,
        'usd_to_etb_rate': usd_to_etb_rate,
    }

    if request.user.is_superuser:
        context.update({
            'today_profit_in_etb': today_profit_in_etb,
            'today_base_profit': today_base_profit,
            'today_premium_profit': today_premium_profit,
        })

    return render(request, 'core/dashboard.html', context)

@superuser_required
def sales_list(request):
    # Permission check removed
    
    # Get sales from all three models
    usd_sales = SaleUSD.objects.select_related('customer', 'user').order_by('-date_created')
    sos_sales = SaleSOS.objects.select_related('customer', 'user').order_by('-date_created')
    etb_sales = SaleETB.objects.select_related('customer', 'user').order_by('-date_created')
    legacy_sales = Sale.objects.select_related('customer', 'user').order_by('-date_created')
    
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
            'user': sale.user,
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
            'user': sale.user,
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
            'user': sale.user,
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
            'user': sale.user,
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

# Allow unauthenticated access for walk-in sales
def create_sale(request):

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
            
            # Convert amount_paid safely
            try:
                amount_paid = Decimal(amount_paid_str)
            except (ValueError, InvalidOperation):
                amount_paid = Decimal('0.00')
            
            # Get currency settings
            currency_settings = CurrencySettings.objects.first()
            exchange_rate = currency_settings.usd_to_sos_rate if currency_settings else Decimal('8000.00')
            etb_exchange_rate = currency_settings.usd_to_etb_rate if currency_settings else Decimal('100.00')
            
            # Get customer (optional - allows anonymous sales)
            customer = None
            if customer_id:
                try:
                    customer = Customer.objects.get(id=customer_id)
                    print(f"Customer found: {customer.name}")
                except Customer.DoesNotExist:
                    print(f"Customer ID {customer_id} not found, creating anonymous sale")
            else:
                print("No customer ID provided, creating anonymous sale")
            
            # Create sale using appropriate model based on currency
            # user is optional - can be None for anonymous/admin operations
            sale_user = request.user if request.user.is_authenticated else None
            
            if currency == 'USD':
                sale = SaleUSD.objects.create(
                    customer=customer,
                    user=sale_user,
                    amount_paid=amount_paid,
                    total_amount=Decimal('0.00'),  # Will be calculated
                    debt_amount=Decimal('0.00')    # Will be calculated
                )
            elif currency == 'SOS':  # SOS currency
                sale = SaleSOS.objects.create(
                    customer=customer,
                    user=sale_user,
                    amount_paid=amount_paid,
                    total_amount=Decimal('0.00'),  # Will be calculated
                    debt_amount=Decimal('0.00')    # Will be calculated
                )
            else:  # ETB currency
                sale = SaleETB.objects.create(
                    customer=customer,
                    user=sale_user,
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
                        quantity = Decimal(quantity_str)
                        
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
                            # Create instance first, then validate before saving
                            if currency == 'USD':
                                sale_item = SaleItemUSD(
                                    sale=sale,
                                    product=product,
                                    quantity=quantity,
                                    unit_price=unit_price,
                                    total_price=total_price
                                )
                            elif currency == 'SOS':  # SOS currency
                                sale_item = SaleItemSOS(
                                    sale=sale,
                                    product=product,
                                    quantity=quantity,
                                    unit_price=unit_price,
                                    total_price=total_price
                                )
                            else:  # ETB currency
                                sale_item = SaleItemETB(
                                    sale=sale,
                                    product=product,
                                    quantity=quantity,
                                    unit_price=unit_price,
                                    total_price=total_price
                                )
                            
                            # Validate sale item (unit validation - PIECE/METER)
                            try:
                                sale_item.full_clean()
                            except ValidationError as e:
                                error_messages = []
                                for field, errors in e.error_dict.items():
                                    error_messages.extend([f"{field}: {error}" for error in errors])
                                error_message = "; ".join(error_messages)
                                raise ValueError(f"Validation error for {product.name}: {error_message}")
                            
                            # Save after validation passes
                            sale_item.save()
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
            
            # FIXED: Update customer debt after sale is saved (only if customer exists)
            if sale.debt_amount > 0 and customer:
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
                audit_user = request.user if request.user.is_authenticated else None
                if audit_user:
                    log_audit_action(
                        audit_user, 'DEBT_ADDED', 'Customer', customer.id,
                        f'Added debt of {sale.debt_amount} {currency} for sale #{sale.transaction_id}',
                        request.META.get('REMOTE_ADDR')
                    )
            elif sale.debt_amount > 0 and not customer:
                print(f"Sale has debt but no customer - anonymous sale with debt: {sale.debt_amount} {currency}")
            
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
                    'user': request.user,
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
            
            # Check debt and customer requirement
            debt_amount = max(Decimal('0.00'), sale.total_amount - sale.amount_paid)
            
            if debt_amount > 0 and not customer:
                # Strictly require customer for credit sales
                error_message = f"Incomplete payment (Debt: {debt_amount}). You must select a customer for credit sales."
                print(f"Validation Error: {error_message}")
                
                # Delete the invalid sale
                sale.delete()
                
                if is_ajax:
                    return JsonResponse({'success': False, 'error': error_message}, status=400)
                messages.error(request, error_message)
                return redirect('core:create_sale')
            
            # Validate sale (standard model validation)
            try:
                sale.full_clean()
            except ValidationError as e:
                error_messages = []
                for field, errors in e.error_dict.items():
                    error_messages.extend([f"{field}: {error}" for error in errors])
                error_message = "; ".join(error_messages)
                print(f"Sale validation error: {error_message}")
                
                # Delete the invalid sale
                sale.delete()
                
                if is_ajax:
                    return JsonResponse({'success': False, 'error': error_message}, status=400)
                messages.error(request, error_message)
                return redirect('core:create_sale')
            
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
@login_required
def sale_detail(request, sale_id, currency=None):
    # Access control: Login required (handled by decorator)

    
    # Try to find the sale in all three models
    sale = None
    sale_type = None
    
    # Check USD sales first
    if currency == 'USD' or currency is None:
        try:
            sale = SaleUSD.objects.select_related('customer', 'user').prefetch_related('items__product').get(id=sale_id)
            sale_type = 'USD'
        except SaleUSD.DoesNotExist:
            pass
    
    # Check SOS sales if not found in USD
    if sale is None and (currency == 'SOS' or currency is None):
        try:
            sale = SaleSOS.objects.select_related('customer', 'user').prefetch_related('items__product').get(id=sale_id)
            sale_type = 'SOS'
        except SaleSOS.DoesNotExist:
            pass
    
    # Check ETB sales if not found in USD or SOS
    if sale is None and (currency == 'ETB' or currency is None):
        try:
            sale = SaleETB.objects.select_related('customer', 'user').prefetch_related('items__product').get(id=sale_id)
            sale_type = 'ETB'
        except SaleETB.DoesNotExist:
            pass
            
    # Check legacy sales if not found in USD, SOS, or ETB
    if sale is None and (currency == 'Legacy' or currency is None):
        try:
            sale = Sale.objects.select_related('customer', 'user').prefetch_related('items__product').get(id=sale_id)
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

@superuser_required
def inventory_list(request):

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

@superuser_required
def add_sale_item(request, currency, sale_id):
    """
    Add an item to an existing sale
    """
    # Permission check removed

    
    sale = None
    model_class = None
    item_model_class = None
    
    if currency == 'USD':
        model_class = SaleUSD
        item_model_class = SaleItemUSD
    elif currency == 'SOS':
        model_class = SaleSOS
        item_model_class = SaleItemSOS
    elif currency == 'ETB':
        model_class = SaleETB
        item_model_class = SaleItemETB
    elif currency == 'Legacy': # Fallback for old system if needed
        model_class = Sale
        item_model_class = SaleItem
    else:
        messages.error(request, "Invalid currency.")
        return redirect('core:sales_list')

    sale = get_object_or_404(model_class, id=sale_id)
    
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        quantity = request.POST.get('quantity')
        
        try:
            product = get_object_or_404(Product, id=product_id)
            quantity = Decimal(quantity)
            
            if quantity <= 0:
                messages.error(request, "Quantity must be greater than zero.")
                return redirect('core:sale_detail', currency=currency, sale_id=sale.id)
            
            if product.current_stock < quantity:
                messages.error(request, f"Not enough stock. Available: {product.current_stock}")
                return redirect('core:sale_detail', currency=currency, sale_id=sale.id)
            
            # Check if this product is already in the sale
            sale_item, created = item_model_class.objects.get_or_create(
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
                user=request.user,
                notes=f'Added to Sale #{sale.transaction_id}'
            )
            
            # Log audit action
            log_audit_action(
                request.user, 'add_sale_item', 'SaleItem', sale_item.id,
                f'Added {quantity} x {product.name} to sale #{sale.transaction_id}',
                request.META.get('REMOTE_ADDR')
            )
            
            messages.success(request, f'Added {quantity} x {product.name} to sale successfully!')
            
        except (ValueError, Product.DoesNotExist, InvalidOperation):
            messages.error(request, "Invalid product or quantity.")
        
        return redirect('core:sale_detail', currency=currency, sale_id=sale.id)
    
    # For GET requests, redirect back to sale detail
    return redirect('core:sale_detail', currency=currency, sale_id=sale.id)

@superuser_required
def restock_inventory(request):
    # Permission check removed as all users are trusted
    
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        quantity = request.POST.get('quantity')
        notes = request.POST.get('notes', '')
        
        try:
            product = Product.objects.get(id=product_id)
            quantity = Decimal(quantity)
            
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
                user=request.user,
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

@superuser_required
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

@superuser_required
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


@superuser_required
def edit_customer(request, customer_id):
    """Edit customer information - all admins can edit"""
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

@superuser_required
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
        usd_sales = SaleUSD.objects.filter(customer=customer).select_related('user')
        sos_sales = SaleSOS.objects.filter(customer=customer).select_related('user')
        etb_sales = SaleETB.objects.filter(customer=customer).select_related('user')
        legacy_sales = Sale.objects.filter(customer=customer).select_related('user')
        
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

@superuser_required
def record_debt_payment(request, customer_id):
    customer = get_object_or_404(Customer, id=customer_id)
    
    if request.method == 'POST':
        form = DebtPaymentForm(request.POST, customer=customer)
        if form.is_valid():
            payment = form.save(commit=False)
            payment.customer = customer
            payment.user = request.user
            
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

@superuser_required
def correct_customer_debt(request, customer_id):
    """View for manually correcting customer debt - all admins can correct"""
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
                user=request.user,
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



@superuser_required
def currency_settings(request):
    
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
            Q(category__name__icontains=query),
            is_active=True
        )[:10]
    
    data = []
    for product in products:
        data.append({
            'id': product.id,
            'name': product.name,
            'brand': product.brand,
            'category': product.category.name,
            'selling_price': float(product.selling_price),
            'current_stock': float(product.current_stock),
            'low_stock_threshold': float(product.low_stock_threshold),
            'selling_unit': product.selling_unit,
            'minimum_sale_length': float(product.minimum_sale_length) if product.minimum_sale_length else None,
        })
    
    return JsonResponse(data, safe=False)

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

@superuser_required
def api_create_product(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'})
    
    # All logged-in admins can create products
    
    try:
        # Get form data
        name = request.POST.get('name', '').strip()
        brand = request.POST.get('brand', '').strip()
        category_id = request.POST.get('category', '').strip()
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
        try:
            product = Product.objects.create(
            name=name,
            brand=brand,
            category=category,
            purchase_price=purchase_price,
            selling_price=selling_price,
            current_stock=current_stock,
            low_stock_threshold=low_stock_threshold,
            is_active=is_active
            )
        except IntegrityError:
            return JsonResponse({'success': False, 'error': 'Product creation failed'}, status=400)
        
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
               
                'selling_price': float(product.selling_price),
                'current_stock': product.current_stock,
                'low_stock_threshold': product.low_stock_threshold,
            }
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@superuser_required
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

@superuser_required
def debug_user(request):
    """Debug view to check user info"""
    user_info = {
        'username': request.user.username,
        'full_name': request.user.get_full_name(),
        'is_superuser': request.user.is_superuser,
        'date_joined': request.user.date_joined.isoformat(),
    }
    return JsonResponse(user_info)
@superuser_required
def debug_inventory(request):
    """Debug view to check inventory status"""
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
        new_customer_id = request.POST.get('customer', '').strip()
        new_amount_paid = request.POST.get('amount_paid')

        try:
            # Store old values before any changes
            old_debt = sale.debt_amount
            old_customer = sale.customer
            
            # First, update amount paid to recalculate debt
            if new_amount_paid:
                sale.amount_paid = Decimal(new_amount_paid)
                sale.save()  # save() method handles debt_amount recalculation logic
            new_debt = sale.debt_amount

            # Customer logic: Required ONLY if debt exists
            if new_debt > Decimal('0.01'):  # Small threshold to avoid floating-point errors
                # Debt exists - customer is required
                if not new_customer_id:
                    messages.error(request, "Customer is required when the sale has outstanding debt. Please select a customer or pay the full amount.")
                    customers = Customer.objects.all().order_by('name')
                    # Reload sale to get correct state (revert any changes)
                    sale.refresh_from_db()
                    # Recalculate values for context (same logic as GET request)
                    currency_settings = CurrencySettings.objects.first()
                    usd_to_etb_rate = currency_settings.usd_to_etb_rate if currency_settings else Decimal('100.00')
                    usd_to_sos_rate = currency_settings.usd_to_sos_rate if currency_settings else Decimal('8000.00')
                    
                    if currency == 'ETB' and hasattr(sale, 'exchange_rate_at_sale') and sale.exchange_rate_at_sale:
                        etb_exchange_rate = sale.exchange_rate_at_sale
                    else:
                        etb_exchange_rate = usd_to_etb_rate
                    
                    if hasattr(sale, 'items'):
                        calculated_total = sale.items.aggregate(total=Sum('total_price'))['total'] or Decimal('0.00')
                        if calculated_total != sale.total_amount:
                            sale.total_amount = calculated_total
                            sale.save()
                    
                    sale.refresh_from_db()
                    calculated_debt = max(Decimal('0.00'), sale.total_amount - sale.amount_paid)
                    
                    if currency == 'USD':
                        total_amount_etb = sale.total_amount * usd_to_etb_rate
                        amount_paid_etb = sale.amount_paid * usd_to_etb_rate
                        debt_amount_etb = calculated_debt * usd_to_etb_rate
                    elif currency == 'SOS':
                        if usd_to_sos_rate > 0:
                            total_amount_usd = sale.total_amount / usd_to_sos_rate
                            amount_paid_usd = sale.amount_paid / usd_to_sos_rate
                            debt_amount_usd = calculated_debt / usd_to_sos_rate
                            total_amount_etb = total_amount_usd * usd_to_etb_rate
                            amount_paid_etb = amount_paid_usd * usd_to_etb_rate
                            debt_amount_etb = debt_amount_usd * usd_to_etb_rate
                        else:
                            total_amount_etb = Decimal('0.00')
                            amount_paid_etb = Decimal('0.00')
                            debt_amount_etb = Decimal('0.00')
                    else:  # ETB
                        total_amount_etb = sale.total_amount
                        amount_paid_etb = sale.amount_paid
                        debt_amount_etb = calculated_debt
                    
                    context = {
                        'sale': sale,
                        'currency': currency,
                        'customers': customers,
                        'total_amount_etb': total_amount_etb,
                        'amount_paid_etb': amount_paid_etb,
                        'debt_amount_etb': debt_amount_etb,
                        'total_amount_original': sale.total_amount,
                        'amount_paid_original': sale.amount_paid,
                        'debt_amount_original': calculated_debt,
                        'usd_to_etb_rate': usd_to_etb_rate,
                        'usd_to_sos_rate': usd_to_sos_rate,
                        'etb_exchange_rate': etb_exchange_rate,
                    }
                    return render(request, 'core/edit_sale.html', context)
                
                # Get the new customer
                new_customer = Customer.objects.get(id=new_customer_id)
                current_customer_id = old_customer.id if old_customer else None
                
                # Handle customer assignment/change (debt transfer)
                if not current_customer_id or int(new_customer_id) != current_customer_id:
                    # Case 1: Sale had no customer, now assigning one
                    if not old_customer:
                        # Add debt to new customer
                        new_customer.update_debt(new_debt, currency)
                        sale.customer = new_customer
                    # Case 2: Sale had a customer, changing to different customer
                    else:
                        # Remove old debt from old customer
                        old_customer.update_debt(-old_debt, currency)
                        # Add new debt to new customer
                        new_customer.update_debt(new_debt, currency)
                        sale.customer = new_customer
                else:
                    # Same customer, but debt amount may have changed
                    if old_customer and new_debt != old_debt:
                        debt_diff = new_debt - old_debt
                        old_customer.update_debt(debt_diff, currency)
            else:
                # Fully paid - clear customer and remove debt from customer if exists
                if old_customer:
                    # Remove all old debt from old customer
                    old_customer.update_debt(-old_debt, currency)
                    sale.customer = None

            sale.save()
            messages.success(request, "Sale updated successfully.")
            return redirect('core:sale_detail', sale_id=sale.id, currency=currency)

        except Exception as e:
            messages.error(request, f"Error updating sale: {e}")
            # Fallthrough to render form with errors
    
    customers = Customer.objects.all().order_by('name')
    
    # Get currency settings for ETB conversion
    currency_settings = CurrencySettings.objects.first()
    usd_to_etb_rate = currency_settings.usd_to_etb_rate if currency_settings else Decimal('100.00')
    usd_to_sos_rate = currency_settings.usd_to_sos_rate if currency_settings else Decimal('8000.00')
    
    # For ETB sales, use stored exchange rate if available
    if currency == 'ETB' and hasattr(sale, 'exchange_rate_at_sale') and sale.exchange_rate_at_sale:
        etb_exchange_rate = sale.exchange_rate_at_sale
    else:
        etb_exchange_rate = usd_to_etb_rate
    
    # Ensure total_amount is calculated from sale items (recalculate if needed)
    # Calculate total from items directly to ensure accuracy
    if hasattr(sale, 'items'):
        calculated_total = sale.items.aggregate(total=Sum('total_price'))['total'] or Decimal('0.00')
        # Only update if different (avoid unnecessary save)
        if calculated_total != sale.total_amount:
            sale.total_amount = calculated_total
            sale.save()
    else:
        # Fallback to calculate_total method
        sale.calculate_total()
    
    sale.refresh_from_db()
    
    # Calculate debt_amount explicitly (total_amount - amount_paid)
    calculated_debt = max(Decimal('0.00'), sale.total_amount - sale.amount_paid)
    
    # Convert all amounts to ETB for display
    if currency == 'USD':
        total_amount_etb = sale.total_amount * usd_to_etb_rate
        amount_paid_etb = sale.amount_paid * usd_to_etb_rate
        debt_amount_etb = calculated_debt * usd_to_etb_rate
    elif currency == 'SOS':
        if usd_to_sos_rate > 0:
            # Convert SOS -> USD -> ETB
            total_amount_usd = sale.total_amount / usd_to_sos_rate
            amount_paid_usd = sale.amount_paid / usd_to_sos_rate
            debt_amount_usd = calculated_debt / usd_to_sos_rate
            total_amount_etb = total_amount_usd * usd_to_etb_rate
            amount_paid_etb = amount_paid_usd * usd_to_etb_rate
            debt_amount_etb = debt_amount_usd * usd_to_etb_rate
        else:
            total_amount_etb = Decimal('0.00')
            amount_paid_etb = Decimal('0.00')
            debt_amount_etb = Decimal('0.00')
    else:  # ETB
        total_amount_etb = sale.total_amount
        amount_paid_etb = sale.amount_paid
        debt_amount_etb = calculated_debt

    context = {
        'sale': sale,
        'currency': currency,
        'customers': customers,
        # Real computed values in ETB
        'total_amount_etb': total_amount_etb,
        'amount_paid_etb': amount_paid_etb,
        'debt_amount_etb': debt_amount_etb,
        # Original currency values for form input
        'total_amount_original': sale.total_amount,
        'amount_paid_original': sale.amount_paid,
        'debt_amount_original': calculated_debt,
        # Exchange rates for JavaScript
        'usd_to_etb_rate': usd_to_etb_rate,
        'usd_to_sos_rate': usd_to_sos_rate,
        'etb_exchange_rate': etb_exchange_rate,
    }
    return render(request, 'core/edit_sale.html', context)


from django.views.decorators.http import require_POST
from django.http import JsonResponse
from decimal import Decimal

@login_required
@require_POST
def api_create_product(request):
    """API endpoint to create a new product"""
    try:
        from django.http import JsonResponse
        from decimal import Decimal
        from .models import Product, Category

        name = request.POST.get('name')
      
        brand = request.POST.get('brand')
        category_id = request.POST.get('category')
        selling_price = request.POST.get('selling_price')
        purchase_price = request.POST.get('purchase_price')
        current_stock = request.POST.get('current_stock', '0')
        low_stock_threshold = request.POST.get('low_stock_threshold', '5')
        selling_unit = request.POST.get('selling_unit', 'UNIT')
        minimum_sale_length = request.POST.get('minimum_sale_length', None)
        is_active = request.POST.get('is_active') == 'on'
        
        # Validate required fields
        if not all([name, brand, category_id, selling_price, purchase_price]):
            return JsonResponse({'success': False, 'error': 'Missing required fields'}, status=400)
        
        # Get category
        try:
            category = Category.objects.get(id=category_id)
        except Category.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Invalid category'}, status=400)
        
        # Create product
        product = Product.objects.create(
            name=name,
          
            brand=brand,
            category=category,
            selling_price=Decimal(selling_price),
            purchase_price=Decimal(purchase_price),
            current_stock=Decimal(current_stock),
            low_stock_threshold=Decimal(low_stock_threshold),
            selling_unit=selling_unit,
            minimum_sale_length=Decimal(minimum_sale_length) if minimum_sale_length else None,
            is_active=is_active
        )
        
        return JsonResponse({
            'success': True,
            'product_id': product.id,
            'message': f'Product "{product.name}" created successfully'
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)