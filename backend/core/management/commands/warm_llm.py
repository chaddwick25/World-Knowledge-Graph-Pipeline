"""
warm_llm — Load the platform LLM into VRAM at startup.

qwen3:14b unloads after OLLAMA_KEEP_ALIVE (10m in docker-compose), and a
cold reload takes 5-15s on the 4070 Ti Super. Running this in the backend
entrypoint means the first user query does not pay that penalty. Fail-soft:
skips silently when the LLM is disabled or unreachable.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Warm the platform LLM (loads the model into VRAM)."

    def handle(self, *args, **options):
        from core.services.llm_service import LLMService

        llm = LLMService.get_instance()
        if not llm.enabled:
            self.stdout.write("LLM disabled — skipping warm-up")
            return
        if not llm.is_available():
            self.stdout.write("LLM unavailable — skipping warm-up")
            return
        answer = llm.chat(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            temperature=0.0,
            max_tokens=4,
        )
        if answer:
            self.stdout.write(f"LLM warm-up OK ({llm.model})")
        else:
            self.stdout.write(f"LLM warm-up returned empty ({llm.model})")
