def describe() -> str:
    return (
        "/tqueue — Queue smoke test\n"
        "Writes a test item to the queue and confirms it.\n\n"
        "Usage: /tqueue\n"
        "Expected: 'Queued command; will run when Executor comes online.'\n"
        "To verify: send /tstatus — item should move from queue to done."
    )
