import logging
import requests
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class SparqlCountryRelationService:
    """
    Fetches Country Relation IDs from the Wikidata SPARQL endpoint.
    """
    WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
    USER_AGENT = "EDAVectorSearchToolkit/1.0 (Contact: team@vgiscience.org)"

    def fetch_country_relations(self) -> Dict[str, Dict]:
        """
        Executes SPARQL to find all countries with their OSM relation IDs and ISO codes.
        Returns a mapping of ISO -> {relation_id, name, uri}.
        """
        query = """
SELECT ?country ?name ?iso ?relId WHERE {
  ?country wdt:P297 ?iso ;             # ISO 3166-1 alpha-2
           wdt:P402 ?relId ;           # OSM relation ID (P402)
           rdfs:label ?name .
  FILTER(LANG(?name) = "en")
}
        """
        
        logger.info(f"Fetching country relations from {self.WIKIDATA_SPARQL_ENDPOINT}...")
        try:
            response = requests.get(
                self.WIKIDATA_SPARQL_ENDPOINT,
                params={"query": query, "format": "json"},
                headers={"User-Agent": self.USER_AGENT},
                timeout=60
            )
            response.raise_for_status()
            
            results = response.json().get("results", {}).get("bindings", [])
            relations = {}
            
            for row in results:
                iso = row.get("iso", {}).get("value")
                rel_id = row.get("relId", {}).get("value")
                name = row.get("name", {}).get("value")
                uri = row.get("country", {}).get("value")
                
                if iso and rel_id:
                    relations[iso.upper()] = {
                        'relation_id': int(rel_id),
                        'name': name,
                        'uri': uri
                    }
                    
            logger.info(f"Successfully fetched {len(relations)} country relations.")
            return relations
            
        except Exception as e:
            logger.error(f"Failed to fetch Wikidata country relations: {e}")
            return {}

sparql_country_relation_service = SparqlCountryRelationService()
