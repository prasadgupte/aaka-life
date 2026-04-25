# Aaka — Claude Code setup prompt

Paste this into Claude Code (in the aaka-repo directory) to begin a guided installation:

---

```
Read INSTALL.md fully before taking any action.

Then collect the required credentials from me — use the "Before you start" table in INSTALL.md as your checklist. Ask me for one item at a time if that's easier, or ask for all of them at once and I'll provide what I have.

Once you have the credentials, write them to /tmp/aaka-setup.json and work through the phases in order:
- For [Claude] phases: run the commands yourself
- For [Human] phases: give me exact step-by-step instructions, then wait for me to confirm before moving on
- Run each phase's Verify block before advancing to the next phase
- If a verify step fails, diagnose and fix before continuing

Start by reading INSTALL.md now.
```
