import json
import ssl
import urllib.request
import logging
from typing import Dict, Any, Optional
from django.conf import settings


class BaseCkanService:
    """Base class for CKAN API interactions following CKAN API best practices"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.base_url = getattr(settings, 'TORONTO_CKAN_CONFIG', {}).get(
            'base_url',
            'https://ckan0.cf.opendata.inter.prod-toronto.ca'
        )
        self.api_version = getattr(settings, 'TORONTO_CKAN_CONFIG', {}).get(
            'api_version',
            '3'
        )
        self.ssl_verify = getattr(settings, 'TORONTO_CKAN_CONFIG', {}).get(
            'ssl_verify',
            False
        )
        self.timeout = getattr(settings, 'TORONTO_CKAN_CONFIG', {}).get(
            'timeout',
            30
        )
        self.user_agent = getattr(settings, 'TORONTO_CKAN_CONFIG', {}).get(
            'user_agent',
            'EDA-Vector-Search-Toolkit/1.0'
        )

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context for CKAN API requests"""
        ctx = ssl.create_default_context()
        if not self.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _make_request(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Make a CKAN API request following best practices from docs.ckan.org
        
        Args:
            action: CKAN action name (e.g., 'package_show')
            params: Optional parameters for the action
            
        Returns:
            Response data dictionary
        """
        if params is None:
            params = {}
        
        # Build URL with action
        url = f"{self.base_url}/api/{self.api_version}/action/{action}"
        
        # Add query parameters for GET-able actions
        if params and action in ['package_show', 'package_list', 'resource_show']:
            query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
            url = f"{url}?{query_string}"
        
        # Create request with proper User-Agent header (CKAN requirement)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        
        try:
            response = urllib.request.urlopen(
                req,
                context=self._get_ssl_context(),
                timeout=self.timeout
            )
            data = json.loads(response.read().decode("utf-8"))
            
            # Validate response per CKAN docs
            if not self._validate_response(data):
                error_msg = data.get('error', {}).get('message', 'Unknown error')
                self.logger.error(f"CKAN API error for {action}: {error_msg}")
                return {}
            
            return self._extract_result(data)
            
        except urllib.error.HTTPError as e:
            self.logger.error(f"HTTP error fetching {action}: {e.code} - {e.reason}")
            return {}
        except urllib.error.URLError as e:
            self.logger.error(f"URL error fetching {action}: {e.reason}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON decode error for {action}: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"Unexpected error fetching {action}: {e}")
            return {}

    def _validate_response(self, response: Dict[str, Any]) -> bool:
        """
        Validate CKAN API response per documentation
        
        CKAN always returns 200 OK, so we must check the 'success' key
        """
        return response.get('success', False) is True

    def _extract_result(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Extract result from CKAN response envelope"""
        return response.get('result', {})
