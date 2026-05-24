"""
Soak runner for BH safe-mode. Two execution lanes:

  --mode=fresh     each task spawns its own `browser-harness` process. Stresses
                   bootstrap + atexit + STATE_FILE concurrency. Slower (3-5s
                   per process startup), but the only way to test atexit.

  --mode=batch     N tasks per BH process (saves N-1 startups). Stresses cap,
                   _record_access mtime, STATE_FILE growth, prune_agent_tabs.
                   Single agent_tab is reused for all N tasks.

Output: results.jsonl (one line per task), summary.json (aggregate).
"""
from __future__ import annotations

import argparse, json, os, random, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).parent
RESULTS_DIR = HERE / "results"
RESULTS_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(HERE))
from tasks import TASKS  # noqa: E402


def run_fresh(task: dict, idx: int, timeout: int = 30) -> dict:
    """Spawn a fresh `browser-harness` process and feed task['inline'] as stdin."""
    code = task["inline"]
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["browser-harness"],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        elapsed = time.time() - t0
        ok = (proc.returncode == 0) and (task["expect"] in (proc.stdout or ""))
        return {
            "idx": idx,
            "name": task["name"],
            "mode": "fresh",
            "ok": ok,
            "rc": proc.returncode,
            "elapsed_s": round(elapsed, 2),
            "stdout_tail": (proc.stdout or "")[-300:],
            "stderr_tail": (proc.stderr or "")[-300:],
            "expected": task["expect"],
        }
    except subprocess.TimeoutExpired:
        return {
            "idx": idx, "name": task["name"], "mode": "fresh",
            "ok": False, "rc": -1, "elapsed_s": round(time.time() - t0, 2),
            "stdout_tail": "", "stderr_tail": "TIMEOUT",
            "expected": task["expect"],
        }
    except Exception as e:
        return {
            "idx": idx, "name": task["name"], "mode": "fresh",
            "ok": False, "rc": -2, "elapsed_s": round(time.time() - t0, 2),
            "stdout_tail": "", "stderr_tail": f"EXCEPTION: {e!r}"[-300:],
            "expected": task["expect"],
        }


def build_batch_inline(picks: list[dict]) -> str:
    """Concatenate N task bodies into one stdin payload, separated by markers.

    Each task emits its marker to BOTH stdout (for normal pass/fail parsing)
    AND stderr (for post-mortem when the process dies mid-batch). When run_batch
    sees TASK_NEVER_REACHED in stdout, the matching stderr marker tells us
    whether the task started at all — if stderr has marker[i] but stdout doesn't,
    task i started and BH/daemon crashed inside it; if neither has marker[i],
    a previous task killed the process before i began.
    """
    parts = ["import sys as _sys"]
    for i, t in enumerate(picks):
        marker = f"###BATCH_TASK_{i:04d}|{t['name']}|EXPECT={t['expect']}###"
        parts.append(f"""
print({marker!r}, flush=True)
_sys.stderr.write({marker!r} + '\\n'); _sys.stderr.flush()
try:
{indent(t['inline'], 4)}
except Exception as _e:
    print(f'###BATCH_FAIL|{t["name"]}|' + repr(_e), flush=True)
""")
    return "\n".join(parts)


def indent(s: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line.strip() else line for line in s.splitlines())


def run_batch(picks: list[dict], batch_idx: int, timeout: int = 180) -> list[dict]:
    code = build_batch_inline(picks)
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["browser-harness"],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        out = proc.stdout or ""
        elapsed = time.time() - t0
        results = []
        for i, t in enumerate(picks):
            marker = f"###BATCH_TASK_{i:04d}|{t['name']}|EXPECT={t['expect']}###"
            mpos = out.find(marker)
            if mpos == -1:
                results.append({
                    "idx": batch_idx * 1000 + i, "name": t["name"], "mode": "batch",
                    "ok": False, "rc": proc.returncode,
                    "elapsed_s": round(elapsed / max(1, len(picks)), 3),
                    "stdout_tail": "", "stderr_tail": "TASK_NEVER_REACHED",
                    "expected": t["expect"],
                })
                continue
            # span until next marker or fail
            next_marker = (
                f"###BATCH_TASK_{i+1:04d}|" if i + 1 < len(picks) else None
            )
            end = out.find(next_marker, mpos) if next_marker else len(out)
            if end == -1:
                end = len(out)
            span = out[mpos:end]
            failed = "###BATCH_FAIL|" + t["name"] in span
            ok = (not failed) and (t["expect"] in span) and (proc.returncode == 0)
            results.append({
                "idx": batch_idx * 1000 + i, "name": t["name"], "mode": "batch",
                "ok": ok, "rc": proc.returncode,
                "elapsed_s": round(elapsed / max(1, len(picks)), 3),
                "stdout_tail": span[-300:],
                "stderr_tail": (proc.stderr or "")[-200:] if not ok else "",
                "expected": t["expect"],
            })
        return results
    except subprocess.TimeoutExpired:
        return [{
            "idx": batch_idx * 1000 + i, "name": t["name"], "mode": "batch",
            "ok": False, "rc": -1, "elapsed_s": -1, "stdout_tail": "",
            "stderr_tail": "BATCH_TIMEOUT", "expected": t["expect"],
        } for i, t in enumerate(picks)]
    except Exception as e:
        return [{
            "idx": batch_idx * 1000 + i, "name": t["name"], "mode": "batch",
            "ok": False, "rc": -2, "elapsed_s": -1, "stdout_tail": "",
            "stderr_tail": f"BATCH_EXCEPTION: {e!r}"[-200:], "expected": t["expect"],
        } for i, t in enumerate(picks)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True, help="total task count")
    ap.add_argument("--mode", choices=["fresh", "batch"], default="batch")
    ap.add_argument("--batch-size", type=int, default=20, help="tasks per BH process when --mode=batch")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, required=True, help="results.jsonl path")
    ap.add_argument("--start-idx", type=int, default=0)
    args = ap.parse_args()

    random.seed(args.seed)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t_start = time.time()
    completed = 0
    pass_count = 0

    with open(out_path, "w", encoding="utf-8") as fout:
        if args.mode == "fresh":
            for i in range(args.n):
                t = random.choice(TASKS)
                rec = run_fresh(t, args.start_idx + i)
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()
                completed += 1
                if rec["ok"]:
                    pass_count += 1
                if completed % 25 == 0:
                    rate = completed / (time.time() - t_start)
                    print(f"  progress {completed}/{args.n} pass={pass_count} rate={rate:.2f}/s", file=sys.stderr)
        else:
            done = 0
            batch_idx = 0
            while done < args.n:
                size = min(args.batch_size, args.n - done)
                picks = [random.choice(TASKS) for _ in range(size)]
                results = run_batch(picks, batch_idx)
                for r in results:
                    r["idx"] = args.start_idx + done + (r["idx"] % 1000)
                    fout.write(json.dumps(r, ensure_ascii=False) + "\n")
                    if r["ok"]:
                        pass_count += 1
                fout.flush()
                done += size
                batch_idx += 1
                rate = done / (time.time() - t_start)
                print(f"  progress {done}/{args.n} pass={pass_count} rate={rate:.2f}/s", file=sys.stderr)

    elapsed = time.time() - t_start
    print(f"\nDONE n={args.n} pass={pass_count} fail={args.n - pass_count} elapsed={elapsed:.1f}s rate={args.n/elapsed:.2f}/s")


if __name__ == "__main__":
    main()
