from typing import Dict


class QueryFormatter:
    """Utility for formatting OSM tag counts into natural language queries."""
    
    @staticmethod
    def format_structured(tag_counts: Dict[str, int]) -> str:
        """
        Format as structured key=value pairs.
        
        Example:
            {'cafe': 40, 'residential': 30} -> "Tags: cafe=40, residential=30"
        """
        if not tag_counts:
            return ""
        
        tag_strings = [f"{tag}={count}" for tag, count in tag_counts.items()]
        return f"Tags: {', '.join(tag_strings)}"
    
    @staticmethod
    def format_natural(tag_counts: Dict[str, int]) -> str:
        """
        Format as natural language description.
        
        Example:
            {'cafe': 40, 'residential': 30, 'park': 20}
            -> "Find areas with many cafes, some residential buildings, and some parks"
        """
        if not tag_counts:
            return ""
        
        total = sum(tag_counts.values())
        if total == 0:
            return ""
        
        parts = []
        for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
            proportion = count / total
            if proportion > 0.4:
                quantifier = "many"
            elif proportion > 0.2:
                quantifier = "some"
            else:
                quantifier = "a few"
            
            # Pluralize if needed
            tag_name = tag if count == 1 else f"{tag}s"
            parts.append(f"{quantifier} {tag_name}")
        
        if len(parts) == 1:
            return f"Find areas with {parts[0]}"
        elif len(parts) == 2:
            return f"Find areas with {parts[0]} and {parts[1]}"
        else:
            return f"Find areas with {', '.join(parts[:-1])}, and {parts[-1]}"
    
    @staticmethod
    def format_weighted_repetition(tag_counts: Dict[str, int]) -> str:
        """
        Format by repeating tags based on their weights.
        
        Example:
            {'cafe': 40, 'residential': 30}
            -> "cafe cafe cafe cafe residential residential residential"
        """
        if not tag_counts:
            return ""
        
        total = sum(tag_counts.values())
        if total == 0:
            return ""
        
        # Normalize to max 10 repetitions
        max_reps = 10
        normalized_counts = {
            tag: max(1, int((count / total) * max_reps))
            for tag, count in tag_counts.items()
        }
        
        words = []
        for tag, reps in normalized_counts.items():
            words.extend([tag] * reps)
        
        return " ".join(words)
    
    @classmethod
    def format(cls, tag_counts: Dict[str, int], format_type: str = 'structured') -> str:
        """
        Format tag counts using specified strategy.
        
        Args:
            tag_counts: Dictionary of tag to count
            format_type: One of 'structured', 'natural', 'weighted'
        
        Returns:
            Formatted query string
        """
        formatters = {
            'structured': cls.format_structured,
            'natural': cls.format_natural,
            'weighted': cls.format_weighted_repetition,
        }
        
        formatter = formatters.get(format_type, cls.format_structured)
        return formatter(tag_counts)
