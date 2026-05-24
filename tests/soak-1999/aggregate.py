"""Quick aggregator for parallel-worker .jsonl outputs."""
import json, sys, glob, collections
files = sys.argv[1:] if len(sys.argv) > 1 else glob.glob("tests/soak-1999/results/p4_w*.jsonl")
total = 0
passed = 0
fails = collections.Counter()
err_samples = collections.defaultdict(list)
for f in files:
    for line in open(f, encoding="utf-8"):
        r = json.loads(line)
        total += 1
        if r["ok"]:
            passed += 1
        else:
            fails[r["name"]] += 1
            if len(err_samples[r["name"]]) < 1:
                err_samples[r["name"]].append((r.get("stderr_tail") or "")[-150:])
print(f"=== TOTAL n={total} pass={passed} fail={total-passed} rate={passed/max(1,total)*100:.1f}% ===")
print()
print("TOP FAILS:")
for name, n in fails.most_common(15):
    print(f"  {name}: fail={n}")
    if err_samples[name]:
        print(f"    -> {err_samples[name][0]}")
