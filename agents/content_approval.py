#!/usr/bin/env python3
"""
Content Approval Agent — example multi-turn approval flow using the Aaka SDK.

Sends post drafts (text + optional image) to the user via Telegram, collects
Approve / Edit / Reject / Skip decisions, handles a second round-trip for edits,
and sends a final confirmation.

Run directly (not via the job scheduler — interactive flows need more than 300s):
    AAKA_AGENT_KEY=aaka-... python3 agents/content_approval.py

For a real agent, replace load_pending_posts() / publish() / reject() / apply_edits()
with your own logic (database, API calls, file writes, etc.).
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from gateway.agent_client import AakaClient, AakaClientError

AGENT_KEY = os.environ.get("AAKA_AGENT_KEY", "")
TIMEOUT_MINUTES = 60  # how long to wait for each reply


# ── Replace with your real data source ───────────────────────────────────────

def load_pending_posts() -> list[dict]:
    """Return a list of post drafts awaiting approval.

    Each dict may contain:
        title      str  — post title
        body       str  — post body text
        image_path str  — optional local path to a preview image
    """
    # Example stub — replace with your real implementation
    return [
        {
            "title": "5 things I learned shipping features solo",
            "body": "Last quarter I shipped 12 features without a PM...",
            "image_path": "",  # set to a file path to send an image
        },
    ]


def publish(post: dict) -> None:
    """Publish the post. Replace with your real publishing logic."""
    print(f"[publish] {post['title']}")


def reject(post: dict) -> None:
    """Archive/reject the post. Replace with your real logic."""
    print(f"[reject] {post['title']}")


def apply_edits(post: dict, feedback: str) -> None:
    """Apply user feedback to the post. Replace with your real logic."""
    print(f"[edit] {post['title']} | feedback: {feedback}")


# ── Main approval loop ────────────────────────────────────────────────────────

def run_approval(client: AakaClient, posts: list[dict]) -> None:
    approved = rejected = edited = skipped = 0

    for i, post in enumerate(posts, 1):
        print(f"\n--- Post {i}/{len(posts)}: {post['title']} ---")

        # Step 1: send draft (image + text, or text only)
        if post.get("image_path") and Path(post["image_path"]).exists():
            client.send_photo(
                post["image_path"],
                caption=f"*Draft {i}/{len(posts)}:* {post['title']}\n\n{post['body']}",
            )
        else:
            client.send(
                f"*Draft {i}/{len(posts)}:* {post['title']}\n\n{post['body']}"
            )

        # Step 2: collect decision
        try:
            choice = client.ask(
                "What would you like to do with this post?",
                options=["Approve", "✏️ Revise", "Reject", "Skip"],
                timeout_minutes=TIMEOUT_MINUTES,
            )
        except AakaClientError:
            client.send(f"Timed out waiting for your decision on _{post['title']}_. Skipping.")
            skipped += 1
            continue

        # Step 3: handle decision
        if choice == "Approve":
            publish(post)
            client.send(f"Published: _{post['title']}_", silent=True)
            approved += 1

        elif "Revise" in choice:
            try:
                feedback = client.ask(
                    "What changes would you like? (free text)",
                    timeout_minutes=TIMEOUT_MINUTES,
                )
            except AakaClientError:
                client.send(f"Timed out waiting for edit feedback on _{post['title']}_. Skipping.")
                skipped += 1
                continue
            apply_edits(post, feedback)
            client.send(f"Updated _{post['title']}_ with your feedback.", silent=True)
            edited += 1

        elif choice == "Reject":
            reject(post)
            client.send(f"Rejected: _{post['title']}_", silent=True)
            rejected += 1

        else:  # Skip or anything unexpected
            skipped += 1

    # Step 4: final summary (fire-and-forget, no reply needed)
    summary = (
        f"Review complete. {len(posts)} posts processed:\n"
        f"✅ Approved: {approved}\n"
        f"✏️ Edited: {edited}\n"
        f"❌ Rejected: {rejected}\n"
        f"⏭ Skipped: {skipped}"
    )
    client.send(summary)
    print(f"\n{summary}")


def main():
    if not AGENT_KEY:
        print("Error: AAKA_AGENT_KEY not set.")
        print("Register with: python3 admin/register_agent.py content-approval 'Content Approval'")
        sys.exit(1)

    client = AakaClient(api_key=AGENT_KEY)
    posts = load_pending_posts()

    if not posts:
        print("No posts pending approval.")
        return

    print(f"Starting approval flow for {len(posts)} post(s)...")
    run_approval(client, posts)


if __name__ == "__main__":
    main()
