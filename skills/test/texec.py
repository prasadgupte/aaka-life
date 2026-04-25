def describe() -> str:
    return (
        "/texec <message> — Executor echo test\n"
        "Queues an echo job; the executor on Mac picks it up and replies directly via Telegram API.\n\n"
        "Usage: /texec hello world\n"
        "Expected: Confirmation immediately, then executor reply within seconds.\n"
        "Use /tstatus if no reply arrives — check if executor is running."
    )
