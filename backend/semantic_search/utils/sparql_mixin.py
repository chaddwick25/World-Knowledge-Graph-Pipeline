"""
SPARQL Retry Mixin

Provides _sparql_request_with_retry() for Wikidata SPARQL clients.
Both WikidataCandidateService classes inherit this to avoid duplicate retry logic.

Follows the app-specific utils pattern: semantic_search.utils.sparql_mixin
"""

import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Default constants (can be overridden by subclasses)
WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_USER_AGENT = "EDAVectorSearchToolkit/1.0 (WorldKG-IGEA; contact via GitHub)"
SPARQL_TIMEOUT_S = 10
SPARQL_MAX_RETRIES = 1
SPARQL_BACKOFF_BASE_S = 2


class SPARQLRetryMixin:
    """
    Mixin providing exponential-backoff retries for Wikidata SPARQL requests.
    
    Retries on: 429 (rate limit), 502/503 (gateway errors), timeout, connection error.
    Backoff: base * 2^attempt (default: 2s, 4s, 8s...)
    
    Subclasses can override:
        sparql_endpoint, sparql_user_agent, sparql_timeout,
        sparql_max_retries, sparql_backoff_base
    
    Usage:
        class MyService(SPARQLRetryMixin):
            sparql_timeout = 30  # override default 10s
            
            def do_query(self):
                data = self._sparql_request_with_retry("SELECT ...", use_post=False)
    """
    
    # Class attributes — override in subclass if needed
    sparql_endpoint: str = WIKIDATA_SPARQL_ENDPOINT
    sparql_user_agent: str = WIKIDATA_USER_AGENT
    sparql_timeout: float = SPARQL_TIMEOUT_S
    sparql_max_retries: int = SPARQL_MAX_RETRIES
    sparql_backoff_base: float = SPARQL_BACKOFF_BASE_S
    
    def _sparql_request_with_retry(
        self,
        query: str,
        use_post: bool = True,
        max_retries: Optional[int] = None,
        backoff_base: Optional[float] = None,
    ) -> Optional[Dict]:
        """
        Execute a SPARQL request with retry+backoff for transient errors.
        
        Args:
            query: SPARQL query string.
            use_post: If True, use POST (avoids URL length limits for large
                      VALUES clauses). If False, use GET (for simple queries).
            max_retries: Override max retry attempts (default: self.sparql_max_retries).
            backoff_base: Override base delay in seconds (default: self.sparql_backoff_base).
        
        Returns:
            Parsed JSON response dict, or None if all retries exhausted.
        """
        max_retries = max_retries if max_retries is not None else self.sparql_max_retries
        backoff_base = backoff_base if backoff_base is not None else self.sparql_backoff_base
        
        for attempt in range(max_retries + 1):
            try:
                headers = {"User-Agent": self.sparql_user_agent}
                if use_post:
                    headers["Accept"] = "application/sparql-results+json"
                    response = requests.post(
                        self.sparql_endpoint,
                        data={"query": query, "format": "json"},
                        headers=headers,
                        timeout=self.sparql_timeout,
                    )
                else:
                    response = requests.get(
                        self.sparql_endpoint,
                        params={"query": query, "format": "json"},
                        headers=headers,
                        timeout=self.sparql_timeout,
                    )
                response.raise_for_status()
                return response.json()
            except requests.exceptions.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                # Retry only on transient server errors / rate limiting
                if status_code in (429, 502, 503) and attempt < max_retries:
                    delay = backoff_base * (2 ** attempt)
                    logger.warning(
                        f"SPARQL request got {status_code}, retrying in {delay}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"SPARQL request failed (HTTP {status_code}): {exc}")
                return None
            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    delay = backoff_base * (2 ** attempt)
                    logger.warning(
                        f"SPARQL request timed out, retrying in {delay}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"SPARQL request timed out after {max_retries} retries")
                return None
            except requests.exceptions.RequestException as exc:
                logger.error(f"SPARQL request failed: {exc}")
                return None
        return None