import uuid

from django.db import models


class CountryRequest(models.Model):
    """A user's vote to process a country that has no pipeline data.

    Append-only tally rows: one row per (country_key, requester_hash)
    pair, so each visitor counts once per country. Raw IPs are never
    stored — requester_hash is a salted sha256 of ip+user-agent.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ***REMOVED*** Identity of the requested country (slug/iso as sent by the client;
    ***REMOVED*** no FK — the country may not exist in the pipeline tables at all).
    country_key = models.CharField(max_length=255, db_index=True)
    country_name = models.CharField(max_length=255)
    iso_code = models.CharField(max_length=10, null=True, blank=True)
    continent = models.CharField(max_length=100, null=True, blank=True)
    ***REMOVED*** Which surface the request came from ('tools' | 'doppelganger').
    surface = models.CharField(max_length=40, blank=True)
    ***REMOVED*** Optional free-text name the requester chose to share.
    requester_name = models.CharField(max_length=255, blank=True)
    requester_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['country_key', 'requester_hash'],
                name='uniq_country_request_per_requester',
            )
        ]

    def __str__(self):
        return f"{self.country_name} ({self.surface or '?'})"
