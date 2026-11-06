from django.urls import path, include

from routes.views import api_root

urlpatterns = [
    path('', api_root, name='api_root'),
    path('api/', include('routes.urls')),
]
