"""Pure Discord task-envelope construction for the Discord adapter."""
from __future__ import annotations

from typing import Mapping


def select_rulebook_key(access_tier: str, is_collaborator: bool) -> str:
    """Select the collaborator rulebook without changing the wire tier."""
    return "team-collaborator" if is_collaborator else access_tier


def build_task_content(
    *,
    task_id: str,
    timestamp: str,
    media_headers: str,
    channel_id,
    channel_name: str,
    guild_name: str,
    source_message_id,
    parent_headers: str,
    user_id,
    access_tier: str,
    is_collaborator: bool,
    priority: str,
    user_task_text: str,
    tier_instructions: Mapping[str, str],
    skill_hints: str,
    secret_notice: str,
) -> str:
    """Return the adapter's established task-mid bytes without side effects."""
    collaborator_line = "collaborator: true\n" if is_collaborator else ""
    rulebook_key = select_rulebook_key(access_tier, is_collaborator)
    rulebook = tier_instructions.get(rulebook_key, tier_instructions["other"])
    return (
        f"id: {task_id}\n"
        f"timestamp: {timestamp}\n"
        "source: discord\n"
        "interaction_type: message\n"
        f"{media_headers}"
        f"channel_id: {channel_id}\n"
        f"channel_name: {channel_name}\n"
        f"guild_name: {guild_name}\n"
        f"source_message_id: {source_message_id}\n"
        f"{parent_headers}"
        f"user_id: {user_id}\n"
        f"access_tier: {access_tier}\n"
        f"{collaborator_line}"
        f"priority: {priority}\n"
        f"task: {user_task_text}\n"
        f"{rulebook}"
        f"{skill_hints}"
        f"{secret_notice}"
    )
