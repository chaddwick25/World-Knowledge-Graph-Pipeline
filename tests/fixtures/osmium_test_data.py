"""
Test fixtures and mock data for osmium command testing
"""

import json
from datetime import datetime
from typing import Dict, List, Any


class OsmiumTestFixtures:
    """Centralized test fixtures for osmium command testing"""
    
    @staticmethod
    def get_sample_fileinfo_output() -> Dict[str, Any]:
        """Sample osmium fileinfo JSON output"""
        return {
            "file": {
                "name": "/tmp/test.pbf",
                "size": 1048576,
                "format": "PBF",
                "compression": "none"
            },
            "header": {
                "generator": "osmium/1.18.0",
                "timestamp": "2023-12-01T10:30:00Z",
                "box": {
                    "left": -122.5194,
                    "bottom": 37.7749,
                    "right": -122.3194,
                    "top": 37.8749
                },
                "has_multiple_object_versions": False,
                "osmosis_replication_timestamp": "2023-12-01T10:30:00Z",
                "osmosis_replication_sequence_number": 12345,
                "osmosis_replication_base_url": "https://planet.openstreetmap.org/replication/minute/"
            },
            "data": {
                "count": {
                    "nodes": 15000,
                    "ways": 3500,
                    "relations": 250,
                    "changesets": 0
                },
                "timestamp": {
                    "first": "2023-01-01T00:00:00Z",
                    "last": "2023-12-01T10:30:00Z"
                },
                "bbox": {
                    "left": -122.5194,
                    "bottom": 37.7749,
                    "right": -122.3194,
                    "top": 37.8749
                }
            }
        }
    
    @staticmethod
    def get_sample_tags_count_output() -> str:
        """Sample osmium tags-count output"""
        return """8500 highway=residential
6200 building=yes
4800 name=*
3900 highway=primary
3200 amenity=restaurant
2800 building=house
2400 highway=secondary
2100 natural=tree
1900 amenity=school
1600 leisure=park
1400 highway=tertiary
1200 building=commercial
1000 amenity=cafe
900 highway=footway
800 building=apartments
750 amenity=bank
700 highway=cycleway
650 natural=water
600 leisure=playground
550 amenity=hospital
500 highway=trunk
450 building=retail
400 amenity=pharmacy
350 highway=service
300 natural=forest
280 leisure=sports_centre
250 amenity=fuel
220 highway=path
200 building=industrial
180 amenity=post_office
160 natural=grassland
140 leisure=swimming_pool
120 highway=track
100 building=garage
90 amenity=library
80 natural=beach
70 leisure=golf_course
60 highway=motorway
50 building=church
40 amenity=place_of_worship
30 natural=cliff
25 leisure=marina
20 highway=motorway_link
15 building=stadium
10 amenity=fire_station
8 natural=peak
6 leisure=dog_park
4 highway=raceway
2 building=bunker
1 amenity=prison"""
    
    @staticmethod
    def get_sample_tags_count_by_type() -> Dict[str, str]:
        """Sample osmium tags-count output by object type"""
        return {
            "node": """5000 amenity=restaurant
3500 natural=tree
2800 amenity=school
2200 amenity=cafe
1800 amenity=bank
1500 amenity=hospital
1200 amenity=pharmacy
1000 amenity=fuel
800 amenity=post_office
600 amenity=library
400 amenity=place_of_worship
300 amenity=fire_station
200 amenity=prison""",
            
            "way": """8500 highway=residential
6200 building=yes
3900 highway=primary
2800 building=house
2400 highway=secondary
1400 highway=tertiary
1200 building=commercial
900 highway=footway
800 building=apartments
700 highway=cycleway
500 highway=trunk
450 building=retail
350 highway=service
220 highway=path
200 building=industrial
120 highway=track
100 building=garage
60 highway=motorway
50 building=church
20 highway=motorway_link
15 building=stadium
4 highway=raceway
2 building=bunker""",
            
            "relation": """2100 natural=water
1600 leisure=park
650 natural=forest
280 leisure=sports_centre
250 natural=grassland
160 leisure=swimming_pool
140 natural=beach
80 leisure=golf_course
70 natural=cliff
30 leisure=marina
25 natural=peak
8 leisure=dog_park"""
        }
    
    @staticmethod
    def get_sample_check_refs_output() -> Dict[str, str]:
        """Sample osmium check-refs output scenarios"""
        return {
            "clean": {
                "stdout": "Checking references...\nAll references are valid.\nNo missing references found.",
                "stderr": ""
            },
            "with_errors": {
                "stdout": """Checking references...
Node 12345678 referenced by way 87654321 not found
Node 23456789 referenced by way 98765432 not found
Node 34567890 referenced by relation 11223344 not found
Way 45678901 referenced by relation 55667788 not found
Missing references found: 4""",
                "stderr": "ERROR: Missing references detected in dataset"
            },
            "severe_errors": {
                "stdout": """Checking references...
Node 11111111 referenced by way 22222222 not found
Node 33333333 referenced by way 44444444 not found
Node 55555555 referenced by way 66666666 not found
Node 77777777 referenced by relation 88888888 not found
Node 99999999 referenced by relation 10101010 not found
Way 12121212 referenced by relation 13131313 not found
Way 14141414 referenced by relation 15151515 not found
Way 16161616 referenced by relation 17171717 not found
Relation 18181818 referenced by relation 19191919 not found
Missing references found: 9""",
                "stderr": "ERROR: Severe referential integrity issues detected"
            }
        }
    
    @staticmethod
    def get_sample_show_output() -> Dict[str, str]:
        """Sample osmium show command output"""
        return {
            "count_format": """Nodes: 15000
Ways: 3500
Relations: 250
Total: 18750""",
            
            "debug_format": """File format: PBF
Compression: none
File size: 1048576 bytes
Header timestamp: 2023-12-01T10:30:00Z
Bounding box: -122.5194,37.7749,-122.3194,37.8749
Object counts:
  Nodes: 15000
  Ways: 3500
  Relations: 250""",
            
            "error": "ERROR: Cannot read file format"
        }
    
    @staticmethod
    def get_sample_temporal_data() -> Dict[str, Any]:
        """Sample temporal data for historical PBF files"""
        return {
            "timestamps": [
                "2023-01-01T00:00:00Z",
                "2023-03-01T00:00:00Z",
                "2023-06-01T00:00:00Z",
                "2023-09-01T00:00:00Z",
                "2023-12-01T00:00:00Z"
            ],
            "evolution_metrics": {
                "2023-01-01T00:00:00Z": {
                    "nodes": 12000,
                    "ways": 2800,
                    "relations": 180,
                    "unique_tags": 120,
                    "tag_entropy": 3.2
                },
                "2023-03-01T00:00:00Z": {
                    "nodes": 13200,
                    "ways": 3100,
                    "relations": 200,
                    "unique_tags": 135,
                    "tag_entropy": 3.4
                },
                "2023-06-01T00:00:00Z": {
                    "nodes": 14100,
                    "ways": 3300,
                    "relations": 220,
                    "unique_tags": 145,
                    "tag_entropy": 3.5
                },
                "2023-09-01T00:00:00Z": {
                    "nodes": 14800,
                    "ways": 3450,
                    "relations": 240,
                    "unique_tags": 150,
                    "tag_entropy": 3.6
                },
                "2023-12-01T00:00:00Z": {
                    "nodes": 15000,
                    "ways": 3500,
                    "relations": 250,
                    "unique_tags": 155,
                    "tag_entropy": 3.7
                }
            }
        }
    
    @staticmethod
    def get_sample_geographic_data() -> Dict[str, Any]:
        """Sample geographic data for testing extracts"""
        return {
            "san_francisco_bbox": {
                "west": -122.5194,
                "south": 37.7049,
                "east": -122.3594,
                "north": 37.8349
            },
            "downtown_sf_bbox": {
                "west": -122.4294,
                "south": 37.7749,
                "east": -122.3894,
                "north": 37.8049
            },
            "bay_area_bbox": {
                "west": -122.8,
                "south": 37.2,
                "east": -121.5,
                "north": 38.0
            },
            "polygon_files": {
                "sf_boundary": "/tmp/sf_boundary.poly",
                "downtown_boundary": "/tmp/downtown_boundary.poly",
                "custom_area": "/tmp/custom_area.poly"
            }
        }
    
    @staticmethod
    def get_sample_filter_configurations() -> Dict[str, Dict[str, Any]]:
        """Sample filter configurations for testing"""
        return {
            "highway_only": {
                "include_tags": ["highway=*"],
                "exclude_tags": []
            },
            "buildings_only": {
                "include_tags": ["building=*"],
                "exclude_tags": ["building=no"]
            },
            "amenities_no_parking": {
                "include_tags": ["amenity=*"],
                "exclude_tags": ["amenity=parking"]
            },
            "transport_infrastructure": {
                "include_tags": ["highway=*", "railway=*", "public_transport=*"],
                "exclude_tags": ["highway=footway", "highway=path"]
            },
            "complex_urban": {
                "include_tags": ["building=*", "highway=*", "amenity=*"],
                "exclude_tags": ["building=no", "amenity=parking", "highway=service"],
                "min_tag_count": 2
            }
        }
    
    @staticmethod
    def get_sample_quality_scenarios() -> Dict[str, Dict[str, Any]]:
        """Sample quality assessment scenarios"""
        return {
            "high_quality": {
                "referential_integrity_score": 1.0,
                "missing_references": 0,
                "data_completeness_score": 0.95,
                "coordinate_precision": 7,
                "tag_completeness": 0.9
            },
            "medium_quality": {
                "referential_integrity_score": 0.85,
                "missing_references": 15,
                "data_completeness_score": 0.8,
                "coordinate_precision": 5,
                "tag_completeness": 0.75
            },
            "low_quality": {
                "referential_integrity_score": 0.6,
                "missing_references": 150,
                "data_completeness_score": 0.6,
                "coordinate_precision": 3,
                "tag_completeness": 0.5
            },
            "corrupted": {
                "referential_integrity_score": 0.3,
                "missing_references": 500,
                "data_completeness_score": 0.4,
                "coordinate_precision": 2,
                "tag_completeness": 0.3
            }
        }
    
    @staticmethod
    def get_sample_processing_sessions() -> List[Dict[str, Any]]:
        """Sample processing session configurations"""
        return [
            {
                "session_name": "Highway Analysis Session",
                "session_type": "METRICS_ANALYSIS",
                "configuration": {
                    "focus": "highway_infrastructure",
                    "quality_checks": True,
                    "geographic_analysis": True
                },
                "expected_results": {
                    "metrics_generated": 1,
                    "processing_duration": 15.5,
                    "quality_score": 0.9
                }
            },
            {
                "session_name": "Urban Dataset Generation",
                "session_type": "DATASET_GENERATION",
                "configuration": {
                    "tag_filters": {
                        "include_tags": ["building=*", "highway=*", "amenity=*"],
                        "exclude_tags": ["amenity=parking"]
                    },
                    "geographic_extracts": [
                        {"bbox": {"west": -122.5, "south": 37.7, "east": -122.3, "north": 37.8}}
                    ]
                },
                "expected_results": {
                    "generated_datasets": 2,
                    "total_output_files": 3,
                    "size_reduction_ratio": 0.4
                }
            },
            {
                "session_name": "Temporal Corpus Creation",
                "session_type": "TEMPORAL_CORPUS",
                "configuration": {
                    "timestamps": [
                        "2023-01-01T00:00:00Z",
                        "2023-06-01T00:00:00Z",
                        "2023-12-01T00:00:00Z"
                    ],
                    "tag_filters": {
                        "include_tags": ["highway=*"]
                    }
                },
                "expected_results": {
                    "corpus_files": 3,
                    "temporal_evolution": True,
                    "total_size_mb": 25.6
                }
            }
        ]
    
    @staticmethod
    def get_mock_command_responses() -> Dict[str, Dict[str, Any]]:
        """Mock responses for different osmium commands"""
        return {
            "fileinfo": {
                "success": {
                    "returncode": 0,
                    "stdout": json.dumps(OsmiumTestFixtures.get_sample_fileinfo_output()),
                    "stderr": ""
                },
                "file_not_found": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ERROR: Cannot open file '/nonexistent/file.pbf'"
                },
                "invalid_format": {
                    "returncode": 2,
                    "stdout": "",
                    "stderr": "ERROR: Invalid PBF file format"
                }
            },
            "tags-count": {
                "success": {
                    "returncode": 0,
                    "stdout": OsmiumTestFixtures.get_sample_tags_count_output(),
                    "stderr": ""
                },
                "empty_file": {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": ""
                },
                "processing_error": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ERROR: Cannot process tags in file"
                }
            },
            "check-refs": {
                "clean": {
                    "returncode": 0,
                    "stdout": OsmiumTestFixtures.get_sample_check_refs_output()["clean"]["stdout"],
                    "stderr": OsmiumTestFixtures.get_sample_check_refs_output()["clean"]["stderr"]
                },
                "with_errors": {
                    "returncode": 1,
                    "stdout": OsmiumTestFixtures.get_sample_check_refs_output()["with_errors"]["stdout"],
                    "stderr": OsmiumTestFixtures.get_sample_check_refs_output()["with_errors"]["stderr"]
                }
            },
            "show": {
                "success": {
                    "returncode": 0,
                    "stdout": OsmiumTestFixtures.get_sample_show_output()["count_format"],
                    "stderr": ""
                },
                "error": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": OsmiumTestFixtures.get_sample_show_output()["error"]
                }
            },
            "tags-filter": {
                "success": {
                    "returncode": 0,
                    "stdout": "Filtered 15000 objects to 7500 objects",
                    "stderr": ""
                },
                "no_matches": {
                    "returncode": 0,
                    "stdout": "Filtered 15000 objects to 0 objects",
                    "stderr": ""
                },
                "filter_error": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ERROR: Invalid filter expression"
                }
            },
            "extract": {
                "success": {
                    "returncode": 0,
                    "stdout": "Extracted 8500 objects from 15000 objects",
                    "stderr": ""
                },
                "invalid_bbox": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ERROR: Invalid bounding box coordinates"
                },
                "polygon_not_found": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ERROR: Polygon file not found"
                }
            },
            "time-filter": {
                "success": {
                    "returncode": 0,
                    "stdout": "Filtered to timestamp 2023-06-01T00:00:00Z",
                    "stderr": ""
                },
                "no_history": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ERROR: File does not contain historical data"
                },
                "invalid_timestamp": {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ERROR: Invalid timestamp format"
                }
            }
        }


class OsmiumTestHelpers:
    """Helper functions for osmium testing"""
    
    @staticmethod
    def create_mock_pbf_file(file_path: str, size_bytes: int = 1048576) -> None:
        """Create a mock PBF file for testing"""
        with open(file_path, 'wb') as f:
            f.write(b'mock_pbf_content' * (size_bytes // 16))
    
    @staticmethod
    def calculate_expected_entropy(tag_distribution: List[Dict[str, Any]]) -> float:
        """Calculate expected Shannon entropy for tag distribution"""
        import math
        
        if not tag_distribution:
            return 0.0
        
        total_count = sum(tag['count'] for tag in tag_distribution)
        entropy = 0.0
        
        for tag in tag_distribution:
            probability = tag['count'] / total_count
            if probability > 0:
                entropy -= probability * math.log2(probability)
        
        return entropy
    
    @staticmethod
    def generate_reduction_ratios(original_counts: Dict[str, int], 
                                filtered_counts: Dict[str, int]) -> Dict[str, float]:
        """Generate expected reduction ratios"""
        ratios = {}
        for obj_type in ['nodes', 'ways', 'relations']:
            orig_count = original_counts.get(obj_type, 0)
            filt_count = filtered_counts.get(obj_type, 0)
            
            if orig_count > 0:
                ratios[f'{obj_type}_ratio'] = filt_count / orig_count
            else:
                ratios[f'{obj_type}_ratio'] = 0.0
        
        return ratios
    
    @staticmethod
    def validate_geographic_bounds(bbox: Dict[str, float]) -> bool:
        """Validate geographic bounding box"""
        required_keys = ['west', 'south', 'east', 'north']
        
        if not all(key in bbox for key in required_keys):
            return False
        
        if bbox['west'] >= bbox['east']:
            return False
        
        if bbox['south'] >= bbox['north']:
            return False
        
        # Check reasonable coordinate ranges
        if not (-180 <= bbox['west'] <= 180 and -180 <= bbox['east'] <= 180):
            return False
        
        if not (-90 <= bbox['south'] <= 90 and -90 <= bbox['north'] <= 90):
            return False
        
        return True
    
    @staticmethod
    def generate_test_timestamps(start_date: str, end_date: str, 
                               interval_months: int = 3) -> List[str]:
        """Generate test timestamps for temporal testing"""
        from datetime import datetime, timedelta
        from dateutil.relativedelta import relativedelta
        
        start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        end = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        
        timestamps = []
        current = start
        
        while current <= end:
            timestamps.append(current.strftime('%Y-%m-%dT%H:%M:%SZ'))
            current += relativedelta(months=interval_months)
        
        return timestamps


# Export commonly used fixtures as module-level constants
SAMPLE_FILEINFO = OsmiumTestFixtures.get_sample_fileinfo_output()
SAMPLE_TAGS_COUNT = OsmiumTestFixtures.get_sample_tags_count_output()
SAMPLE_FILTER_CONFIGS = OsmiumTestFixtures.get_sample_filter_configurations()
SAMPLE_GEOGRAPHIC_DATA = OsmiumTestFixtures.get_sample_geographic_data()
MOCK_COMMAND_RESPONSES = OsmiumTestFixtures.get_mock_command_responses()
