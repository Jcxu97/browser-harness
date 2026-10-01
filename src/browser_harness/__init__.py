"""Browser Harness core package."""
import os

# The daemon prefixes the title of its tab with a horse emoji. In this fork the
# daemon starts on a tab of the user's own window, so keep the marker off unless
# someone asks for it. Set here so that the daemon process gets it too.
os.environ.setdefault("BH_TAB_MARKER", "0")
