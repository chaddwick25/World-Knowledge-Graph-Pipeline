"""Public country-request capture — the tally behind the picker's
"request processing" view.

`POST /api/country-requests/` is the app's first public write endpoint:
it is intentionally absent from the middleware write gate (the request
is a vote, not a trigger). Abuse control is a math CAPTCHA (stateless,
HMAC-signed — no cache dependency) plus one vote per
(country, requester_hash).

GET  /api/country-requests/captcha/   → {captcha_id, question}
POST /api/country-requests/           → {created, total_requests}
GET  /api/country-requests/tally/     → ranked request counts
"""

import hashlib
import hmac
import logging
import secrets
import time

from django.conf import settings
from django.db.models import Count, Max
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import CountryRequest

logger = logging.getLogger(__name__)

CAPTCHA_TTL_SECONDS = 300


def _sign(payload: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:24]


def _requester_hash(request) -> str:
    """Salted sha256 of the request's IP+UA — dedupes votes without
    storing a raw IP."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = (xff.split(',')[0].strip() if xff else '') \
        or request.META.get('REMOTE_ADDR', '')
    ua = request.META.get('HTTP_USER_AGENT', '')
    raw = f"{ip}|{ua}|{settings.SECRET_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()


class CountryRequestCaptchaView(APIView):
    """Issue a stateless math challenge the submit endpoint verifies."""

    def get(self, request):
        import random
        a, b = random.randint(1, 9), random.randint(1, 9)
        nonce = secrets.token_hex(4)
        exp = int(time.time()) + CAPTCHA_TTL_SECONDS
        payload = f"{a}.{b}.{nonce}.{exp}"
        return Response({
            'captcha_id': f"{payload}.{_sign(payload)}",
            'question': f"What is {a} + {b}?",
        })


class CountryRequestView(APIView):
    """Record a country-processing request (CAPTCHA-gated)."""

    def post(self, request):
        data = request.data or {}
        country_name = (data.get('country_name') or '').strip()
        if not country_name:
            return Response({'error': 'country_name is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        err = self._verify_captcha(data)
        if err:
            return Response(err[0], status=err[1])

        country_key = (
            (data.get('iso_code') or '').strip().lower()
            or country_name.lower().replace(' ', '-')
        )
        req, created = CountryRequest.objects.get_or_create(
            country_key=country_key,
            requester_hash=_requester_hash(request),
            defaults={
                'country_name': country_name,
                'iso_code': (data.get('iso_code') or '').strip() or None,
                'continent': (data.get('continent') or '').strip() or None,
                'surface': (data.get('surface') or '').strip()[:40],
                'requester_name': (data.get('name') or '').strip()[:255],
            },
        )
        total = CountryRequest.objects.filter(country_key=country_key).count()
        return Response({
            'created': created,
            'country_name': req.country_name,
            'total_requests': total,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @staticmethod
    def _verify_captcha(data):
        """Returns (error_dict, http_status) or None when valid."""
        captcha_id = data.get('captcha_id') or ''
        answer = (data.get('captcha_answer') or '').strip()
        try:
            a, b, _nonce, exp, sig = captcha_id.split('.')
            payload = f"{a}.{b}.{_nonce}.{exp}"
            if not hmac.compare_digest(sig, _sign(payload)):
                raise ValueError
            if int(exp) < int(time.time()):
                return ({'error': 'Captcha expired — try again.'}, 400)
            if int(answer) != int(a) + int(b):
                return ({'error': 'Wrong captcha answer.'}, 400)
        except (ValueError, AttributeError):
            return ({'error': 'Invalid captcha.'}, 400)
        return None


class CountryRequestTallyView(APIView):
    """Ranked request counts — the 'which countries next' table."""

    def get(self, request):
        top = int(request.GET.get('top', 50))
        rows = (
            CountryRequest.objects
            .values('country_key', 'country_name', 'iso_code', 'continent')
            .annotate(requests=Count('id'), latest=Max('created_at'))
            .order_by('-requests')[:top]
        )
        return Response({'results': [
            {
                'country_key': r['country_key'],
                'country_name': r['country_name'],
                'iso_code': r['iso_code'],
                'continent': r['continent'],
                'requests': r['requests'],
                'latest': r['latest'].isoformat() if r['latest'] else None,
            } for r in rows
        ]})
