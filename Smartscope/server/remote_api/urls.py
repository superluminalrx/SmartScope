from django.urls import path

from . import views

urlpatterns = [
    path('', views.devices, name='remoteDevices'),
    path('count/', views.device_count, name='remoteDeviceCount'),
    path('pair/', views.pair, name='remoteDevicePair'),
    path('<int:pk>/remove/', views.remove_device, name='remoteDeviceRemove'),
]
