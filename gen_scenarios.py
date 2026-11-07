import pandas as pd

baseline = {
    "device_id": "LAPTOP01",
    "cpu_load_target_pct": 20,
    "memory_limit_mb": 4096,
    "rtt_target_ms": 10,
    "bandwidth_target_mbps": 100,
    "packet_loss_target_pct": 0
}
scenarios = []
scenarios.append(baseline.copy())


# Change CPU only
for cpu in [50, 80]:
    scenario = baseline.copy()
    scenario["cpu_load_target_pct"] = cpu
    scenarios.append(scenario)


# Change memory only
for memory in [2048, 1024]:
    scenario = baseline.copy()
    scenario["memory_limit_mb"] = memory
    scenarios.append(scenario)


# Change RTT only
for rtt in [50, 150]:
    scenario = baseline.copy()
    scenario["rtt_target_ms"] = rtt
    scenarios.append(scenario)


# Change bandwidth only
for bandwidth in [25, 5]:
    scenario = baseline.copy()
    scenario["bandwidth_target_mbps"] = bandwidth
    scenarios.append(scenario)


# Change packet loss only
for loss in [0.5, 2]:
    scenario = baseline.copy()
    scenario["packet_loss_target_pct"] = loss
    scenarios.append(scenario)


# Convert to a dataframe
df = pd.DataFrame(scenarios)


# Add scenario IDs
df.insert(
    0,
    "scenario_id",
    [f"S{i:03d}" for i in range(1, len(df) + 1)]
)


# Save to CSV
df.to_csv("data/scenarios.csv", index=False)


print(df)
print(f"\nGenerated {len(df)} scenarios.")