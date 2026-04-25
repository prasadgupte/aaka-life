def describe() -> str:
    return (
        "/tstatus — Live queue + outbox snapshot\n"
        "Shows items waiting for executor (queue) and messages waiting to send (outbox).\n\n"
        "Usage: /tstatus\n"
        "Expected: Count of pending queue items and pending outbox items with intent labels."
    )
