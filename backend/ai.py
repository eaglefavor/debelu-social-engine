from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from backend.config import settings


class AIProviderError(RuntimeError):
    pass


def generate_week(brand_profile: dict[str, str], plan_start: str, plan_dates: list[str]) -> list[dict[str, Any]]:
    """Generate one weekly plan through an OpenAI-compatible Chat Completions API."""
    if not settings.ai_api_key:
        raise AIProviderError("AI generation is not configured. Add AI_API_KEY to the server .env file and restart the app.")

    system_prompt = (
        "You are the content editor for Debelu Ventures, a cybersecurity, cloud and software engineering brand. "
        "Create accurate, useful, platform-native educational content. Avoid fearmongering, invented statistics, "
        "unverifiable claims, overpromising, generic motivational copy and fabricated customer stories. "
        "If a technical nuance is uncertain, use cautious wording rather than inventing detail. "
        "Content is a draft and will be reviewed by a human before use. Return valid JSON only."
    )
    user_prompt = {
        "task": "Create a seven-day content plan. Each idea must be adapted independently for Instagram, Threads and TikTok.",
        "brand_profile": brand_profile,
        "plan_start_monday": plan_start,
        "plan_dates_monday_to_sunday": plan_dates,
        "content_pillars": [
            "CYBERSECURITY", "CLOUD", "APPLICATION SECURITY", "DEVSECOPS",
            "SOFTWARE ENGINEERING", "AI SECURITY", "DATA SECURITY", "BUILD IN PUBLIC", "DEBELU VENTURES",
        ],
        "requirements": {
            "number_of_ideas": 7,
            "audience": "Name a realistic target audience for each idea.",
            "instagram": "A 5- or 6-slide carousel with clearly separated slide copy, plus a concise caption and CTA.",
            "threads": "A self-contained, conversational text post with useful line breaks and an optional discussion CTA.",
            "tiktok": "A 30-45 second vertical-video script with a hook, explanation and close, plus a caption.",
            "variety": "Use different pillars and angles across the week. Avoid repeating the same topic.",
            "technical_accuracy": "Do not claim HTTPS prevents application flaws; distinguish authentication from authorization; avoid unsupported statistics.",
            "output_schema": {
                "ideas": [
                    {
                        "topic": "string, maximum 120 characters",
                        "category": "one content pillar",
                        "audience": "string, maximum 160 characters",
                        "variants": {
                            "instagram": {"format": "string", "body": "string", "caption": "string"},
                            "threads": {"format": "string", "body": "string", "caption": "string"},
                            "tiktok": {"format": "string", "body": "string", "caption": "string"},
                        },
                    }
                ]
            },
        },
    }
    payload = {
        "model": settings.ai_model,
        "temperature": 0.55,
        "max_tokens": 9000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        f"{settings.ai_base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.ai_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not return provider response bodies: they may contain account or request details.
        raise AIProviderError(f"The configured AI provider returned HTTP {exc.code}. Check the model and API configuration.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIProviderError("Could not reach the configured AI provider. Check server network access and AI_BASE_URL.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AIProviderError("The configured AI provider returned an unreadable response.") from exc

    try:
        content = response_data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        result = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AIProviderError("The AI response was not valid JSON in the expected format. Try again or change the configured model.") from exc

    ideas = result.get("ideas") if isinstance(result, dict) else None
    if not isinstance(ideas, list) or len(ideas) != 7:
        raise AIProviderError("The AI response did not contain seven ideas. Try generating the week again.")
    return ideas
