from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import pairing
from .models import RemoteDevice


def staff_with_remote_api(view):
    """Devices open the web app to the outside, so only staff manage them."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.REMOTE_API_ENABLED:
            raise Http404('Remote API is disabled')
        if not request.user.is_staff:
            return HttpResponseForbidden('Only staff can manage remote devices')
        return view(request, *args, **kwargs)
    return login_required(wrapper)


@staff_with_remote_api
def devices(request):
    context = dict(devices=RemoteDevice.objects.select_related('paired_by'),
                   online=pairing.listener_state() is not None,
                   pairing_minutes=pairing.PAIRING_SECONDS // 60)
    return render(request, 'remote_api/devices.html', context)


@staff_with_remote_api
def device_count(request):
    """Polled by the page while a pairing link is shown, to notice the new device."""
    return JsonResponse({'count': RemoteDevice.objects.count()})


@require_POST
@staff_with_remote_api
def pair(request):
    state = pairing.listener_state()
    if state is None:
        return JsonResponse({'error': 'The remote API listener is not running.'}, status=503)
    link = pairing.pairing_link(state['ticket'], pairing.create_pairing(request.user))
    return JsonResponse({'link': link, 'seconds': pairing.PAIRING_SECONDS})


@require_POST
@staff_with_remote_api
def remove_device(request, pk):
    get_object_or_404(RemoteDevice, pk=pk).delete()
    return redirect('remoteDevices')
