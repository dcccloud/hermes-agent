"""Shared vision utilities for screenshot capture and LLM-based reading.

These are used by state_discovery, scrape_utils, and recipe_generator.
"""

import base64
from io import BytesIO
from typing import Any

from openai import OpenAI

from openclaw_agent.engine.common.config import get_config
from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("vision_utils")


def take_screenshot_b64(device: Any) -> str:
    """Capture a screenshot via u2 and return as base64 JPEG."""
    img = device.screenshot(format="pillow")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def vision_read(device: Any, prompt: str) -> str:
    """Take a screenshot and use the vision_read LLM to extract information."""
    config = get_config()
    img_b64 = take_screenshot_b64(device)

    client = OpenAI(
        api_key=config.vision_read.api_key,
        base_url=config.vision_read.base_url,
    )

    resp = client.chat.completions.create(
        model=config.vision_read.model_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}",
                        },
                    },
                ],
            }
        ],
        temperature=0.1,
        max_tokens=2000,
    )

    return (resp.choices[0].message.content or "").strip()


def vision_read_b64(img_b64: str, prompt: str) -> str:
    """Call vision LLM with a pre-captured base64 screenshot."""
    config = get_config()

    client = OpenAI(
        api_key=config.vision_read.api_key,
        base_url=config.vision_read.base_url,
    )

    resp = client.chat.completions.create(
        model=config.vision_read.model_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}",
                        },
                    },
                ],
            }
        ],
        temperature=0.1,
        max_tokens=2000,
    )

    return (resp.choices[0].message.content or "").strip()
