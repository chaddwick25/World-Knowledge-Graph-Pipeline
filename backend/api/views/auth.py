"""Session-auth endpoints for the self-hosted SPA (login guard).

Login sets the Django session cookie (HttpOnly, same-origin). The SPA
fetches the CSRF token from GET /api/auth/csrf/ first and echoes it back
via the X-CSRFToken header on unsafe requests. Registration is gated by
the SIGNUP_INVITE_CODE env var so friends can self-register with a code
the owner shares.
"""

import json

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST


def _user_payload(user):
    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'is_staff': user.is_staff,
    }


def _json_body(request):
    """Return the request body as a dict, from JSON or form-encoded POST."""
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body or b'{}')
        except json.JSONDecodeError:
            return {}
    return request.POST


@require_GET
@ensure_csrf_cookie
def csrf(request):
    """Set the csrftoken cookie so the SPA can echo it via X-CSRFToken."""
    return JsonResponse({'ok': True})


@require_POST
def login_view(request):
    body = _json_body(request)
    user = authenticate(request, username=body.get('username', ''), password=body.get('password', ''))
    if user is None:
        return JsonResponse({'detail': 'Invalid username or password.'}, status=401)
    login(request, user)
    return JsonResponse({'user': _user_payload(user)})


@require_POST
def logout_view(request):
    logout(request)
    return JsonResponse({'ok': True})


@require_POST
def register_view(request):
    invite_code = getattr(settings, 'SIGNUP_INVITE_CODE', '')
    if not invite_code:
        return JsonResponse({'detail': 'Registration is disabled.'}, status=403)
    body = _json_body(request)
    if body.get('invite_code', '') != invite_code:
        return JsonResponse({'detail': 'Invalid invite code.'}, status=403)

    username = (body.get('username') or '').strip()
    password = body.get('password') or ''
    email = (body.get('email') or '').strip()

    if len(username) < 3:
        return JsonResponse({'detail': 'Username must be at least 3 characters.'}, status=400)
    if len(password) < 8:
        return JsonResponse({'detail': 'Password must be at least 8 characters.'}, status=400)
    if User.objects.filter(username=username).exists():
        return JsonResponse({'detail': 'Username already taken.'}, status=400)

    user = User.objects.create_user(username=username, password=password, email=email or None)
    login(request, user)
    return JsonResponse({'user': _user_payload(user)})


@require_GET
def me(request):
    if not request.user.is_authenticated:
        return JsonResponse({'detail': 'Not authenticated.'}, status=401)
    return JsonResponse({'user': _user_payload(request.user)})
