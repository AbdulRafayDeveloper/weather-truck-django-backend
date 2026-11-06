import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()

print('Weather Truck API started.', flush=True)

# Vercel's Python runtime looks for a module-level `app`.
app = application
