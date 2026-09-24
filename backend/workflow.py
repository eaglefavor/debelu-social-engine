from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from backend.database import content_variants, variant_assets


def content_fingerprint(topic: str, category: str, audience: str, variants: dict[str, dict[str, Any]]) -> str:
    relevant = {
        "topic": topic,
        "category": category,
        "audience": audience,
        "variants": {
            platform: {
                "format": variants.get(platform, {}).get("format", ""),
                "body": variants.get(platform, {}).get("body", ""),
                "caption": variants.get(platform, {}).get("caption", ""),
                "assetIds": sorted(variants.get(platform, {}).get("assetIds", [])),
            }
            for platform in ("instagram", "threads", "tiktok")
        },
    }
    canonical = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_fingerprint(connection: Connection, row) -> str:
    variant_rows = connection.execute(
        select(content_variants).where(content_variants.c.idea_id == row["id"])
    ).mappings().all()
    asset_rows = connection.execute(
        select(variant_assets.c.platform, variant_assets.c.asset_id)
        .where(variant_assets.c.idea_id == row["id"])
        .order_by(variant_assets.c.position)
    ).all()
    assets_by_platform: dict[str, list[str]] = {platform: [] for platform in ("instagram", "threads", "tiktok")}
    for platform, asset_id in asset_rows:
        assets_by_platform.setdefault(platform, []).append(asset_id)
    by_platform = {
        variant["platform"]: {
            "format": variant["format"],
            "body": variant["body"],
            "caption": variant["caption"],
            "assetIds": assets_by_platform.get(variant["platform"], []),
        }
        for variant in variant_rows
    }
    return content_fingerprint(row["topic"], row["category"], row["audience"], by_platform)
