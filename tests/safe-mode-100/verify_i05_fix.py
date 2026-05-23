"""I05 fix verification — agent navigates to a URL containing the marker
substring (?bh-agent-tab=spoof). The atexit hook MUST NOT close that tab
because it has a different nonce than what was stored at spawn.

Run from a real BH process (with bootstrap injected). Stdout is captured by
the runner.
"""
import json, os, sys, time
from pathlib import Path

# These globals are pre-injected by run.py + bootstrap.safe_globals().
# noqa: F821 — agent_tab/goto/eval_js are bound by bootstrap, not imports.

print(f"[verify_i05] agent_tab tid = {agent_tab[:8]}")  # noqa: F821
spoof_url = "https://example.com/?bh-agent-tab=spoof"
goto(spoof_url)  # noqa: F821
href = eval_js("location.href")  # noqa: F821
print(f"[verify_i05] href after goto = {href}")
assert "spoof" in href, f"navigation failed, href={href}"

# Read the state file to confirm a nonce is stored for this tab
state_file = Path.home() / ".browser-harness" / "second-window-state.json"
state = json.loads(state_file.read_text())
my_pid = os.getpid()
mine = [r for r in state.get("agent_tabs", []) if r.get("claimed_by_pid") == my_pid]
my_rec = next((r for r in mine if r["tid"] == agent_tab), None)  # noqa: F821
print(f"[verify_i05] my state record = {json.dumps(my_rec, indent=2)}")
assert my_rec is not None, "no state record for my tab"
assert my_rec.get("nonce"), f"FAIL: no nonce stored — atexit will fall back to leave-alone, OK but not the strong path"
assert my_rec["nonce"] not in href, f"FAIL: nonce {my_rec['nonce']} happens to appear in spoofed URL — won't be a clean test"
print(f"[verify_i05] nonce {my_rec['nonce']!r} NOT in spoofed URL — atexit will correctly skip closing this tab")
print("[verify_i05] OK — letting BH exit; expect tab to STAY OPEN")
