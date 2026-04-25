def describe() -> str:
    return (
        "/tthread — Threading smoke test\n"
        "Sends a reply using reply_to_message_id to test threaded replies.\n\n"
        "Usage: /tthread\n"
        "Expected: Acknowledgement message + a threaded reply to your original message.\n"
        "If threading is unsupported, both messages arrive normally."
    )
