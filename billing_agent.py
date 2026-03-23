"""Entry point for the SaaS Billing Agent container.

This is the module specified in the Dockerfile CMD.
It imports from the agent package and starts the Runtime app.
"""
import sys
import os

# Ensure project root is on path so 'agent' package resolves
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.agent import app  # noqa: E402

if __name__ == "__main__":
    app.run()
