"""://agent_arena — entrypoint.

Preserves the organizers' contract (`python agent.py`, configured by the same
APPWORLD_* / MODEL env vars), but delegates to our owned harness in ./harness/.
The original starter is preserved as agent_starter_original.py.
"""
from harness.run import main

if __name__ == "__main__":
    main()
