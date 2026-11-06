from django.urls import path
from .views import health_check, calculate_routes

urlpatterns = [
    path('health/', health_check, name='health_check'),
    path('routes/', calculate_routes, name='calculate_routes'),
]
