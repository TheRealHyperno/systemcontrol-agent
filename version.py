"""
Single source of truth for which SystemC release this is.

Change SYSTEMC_VERSION (and SYSTEMC_STAGE if needed) here, once.  Every startup
banner (bot, control panel, setup wizard, Stoat bot, installers) and the agent's
system prompt read from this file.  No dependencies, so the installers can run it
before anything is installed:

    python3 version.py
"""

SYSTEMC_VERSION = "Aurelia"   # codename and/or version
SYSTEMC_STAGE = "beta"       # "beta", "release", ... ("" to leave it out)


def display() -> str:
    """e.g. 'Drovex (beta)' -- used in the system prompt."""
    return f"{SYSTEMC_VERSION} ({SYSTEMC_STAGE})" if SYSTEMC_STAGE else SYSTEMC_VERSION


def banner() -> str:
    """e.g. 'You are running a beta version of SystemC codenamed Drovex.'"""
    kind = f"a {SYSTEMC_STAGE} version" if SYSTEMC_STAGE else "version"
    return f"You are running {kind} of SystemC codenamed {SYSTEMC_VERSION}."


if __name__ == "__main__":
    print(banner())
