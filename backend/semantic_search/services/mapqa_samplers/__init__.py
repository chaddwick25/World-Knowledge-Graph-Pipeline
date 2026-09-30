"""Per-template MapQA samplers (monolith split, Phase 5).

Each template's sampler methods live in its own module as a mixin class;
``MapQAQuestionGenerator`` inherits them all.
"""

from semantic_search.services.mapqa_samplers.template_1 import Template1SamplerMixin
from semantic_search.services.mapqa_samplers.template_2 import Template2SamplerMixin
from semantic_search.services.mapqa_samplers.template_4 import Template4SamplerMixin
from semantic_search.services.mapqa_samplers.template_5 import Template5SamplerMixin
from semantic_search.services.mapqa_samplers.template_8 import Template8SamplerMixin

__all__ = [
    "Template1SamplerMixin",
    "Template2SamplerMixin",
    "Template4SamplerMixin",
    "Template5SamplerMixin",
    "Template8SamplerMixin",
]
