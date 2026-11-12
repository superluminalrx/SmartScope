from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import pairing
from .models import RemoteDevice


def remote_api_enabled(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.REMOTE_API_ENABLED:
            raise Http404('Remote API is disabled')
        return view(request, *args, **kwargs)
    return login_required(wrapper)


def visible_devices(request):
    if request.user.is_staff and request.GET.get('all') == '1':
        return RemoteDevice.objects.select_related('user')
    return RemoteDevice.objects.select_related('user').filter(user=request.user)


@remote_api_enabled
def devices(request):
    context = dict(devices=visible_devices(request), online=pairing.listener_state() is not None,
                   show_all=request.GET.get('all') == '1', pairing_minutes=pairing.PAIRING_SECONDS // 60)
    return render(request, 'remote_api/devices.html', context)


@remote_api_enabled
def device_count(request):
    """Polled by the page while a pairing link is shown, to notice the new device."""
    return JsonResponse({'count': RemoteDevice.objects.filter(user=request.user).count()})


@require_POST
@remote_api_enabled
def pair(request):
    state = pairing.listener_state()
    if state is None:
        return JsonResponse({'error': 'The remote API listener is not running.'}, status=503)
    link = pairing.pairing_link(state['ticket'], pairing.create_pairing(request.user))
    return JsonResponse({'link': link, 'seconds': pairing.PAIRING_SECONDS})


@require_POST
@remote_api_enabled
def remove_device(request, pk):
    device = get_object_or_404(RemoteDevice, pk=pk)
    if device.user != request.user and not request.user.is_staff:
        return HttpResponseForbidden('This device belongs to another user')
    device.delete()
    return redirect('remoteDevices')
