from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    # Home - redirects based on user permissions
    path('', views.home, name='home'),
    
    # Dashboard
    #path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    # Sales
    path('sales/', views.sales_list, name='sales_list'),
    path('sales/create/', views.create_sale, name='create_sale'),
    path('sales/<int:sale_id>/', views.sale_detail, name='sale_detail_legacy'), # Legacy fallback
    path('sales/<str:currency>/<int:sale_id>/', views.sale_detail, name='sale_detail'),
    path('sales/<str:currency>/<int:sale_id>/edit/', views.edit_sale, name='edit_sale'),
    path('sales/<str:currency>/<int:sale_id>/add-item/', views.add_sale_item, name='add_sale_item'),
    # Inventory
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('restock-inventory/', views.restock_inventory, name='restock_inventory'),
    
    # Customers
    path('customers/', views.customers_list, name='customers_list'),
    path('customers/create/', views.create_customer, name='create_customer'),
    path('customers/<int:customer_id>/', views.customer_detail, name='customer_detail'),
    path('customers/<int:customer_id>/edit/', views.edit_customer, name='edit_customer'),
    path('customers/<int:customer_id>/payment/', views.record_debt_payment, name='record_debt_payment'),
    path('customers/<int:customer_id>/correct-debt/', views.correct_customer_debt, name='correct_customer_debt'),
    

    
    # Settings
    path('currency-settings/', views.currency_settings, name='currency_settings'),
    
    # API Endpoints for mobile interface
    path('api/search-products/', views.api_search_products, name='api_search_products'),
    path('api/search-customers/', views.api_search_customers, name='api_search_customers'),
    path('api/create-customer/', views.api_create_customer, name='api_create_customer'),
    path('api/create-product/', views.api_create_product, name='api_create_product'),
    path('api/product/<int:product_id>/', views.api_get_product_details, name='api_get_product_details'),
    
    # Debug
    path('debug/user/', views.debug_user, name='debug_user'),
    path('debug/inventory/', views.debug_inventory, name='debug_inventory'),
    path('debug/customer/<int:customer_id>/', views.debug_customer, name='debug_customer'),
    
    # Offline Fallback
    path('offline/', views.offline_view, name='offline'),
]