import subprocess
import time
import csv
import os
import re
import resource
from pathlib import Path

OPENSSL = "/opt/openssl-3.5/bin/openssl"
GNU_TIME = "/usr/bin/time"

PROFILES = [
    "X25519", #classical
    "MLKEM768", #pqc
    "X25519MLKEM768" #hybrid
]

REPETITIONS = 3
env = os.environ.copy()
env["LD_LIBRARY_PATH"] = "/opt/openssl-3.5/lib64:" + env.get(
    "LD_LIBRARY_PATH", ""
)

project_dir = Path(__file__).parent
data_dir = project_dir / "data"
data_dir.mkdir(exist_ok=True)
output_file = data_dir / "raw_network_pilot_S001.csv"

rows = []
run_number = 1

for profile in PROFILES:
    for repetition in range(1, REPETITIONS + 1):
        openssl_command = [
            OPENSSL,
            "s_client",
            "-connect", "10.10.0.2:4433",
            "-tls1_3",
            "-groups", profile,
            "-CAfile", str(Path.home() / "fyp-tls-test/server.crt"),
            "-verify_ip", "10.10.0.2",
            "-verify_return_error"
        ]
        command = [
            GNU_TIME,
            "-f", "__MAX_MEMORY_KB__:%M"
        ] + openssl_command
        #Total elapsed time
        start = time.perf_counter()
        #Max cpu time
        cpu_before = resource.getrusage(
            resource.RUSAGE_CHILDREN
        )
        #Actually run the OpenSSL TLS client
        result = subprocess.run(
            command,
            input="\n",
            text=True,
            capture_output=True,
            env=env,
            timeout=10
        )

        #Record CPU usage after OpenSSL finishes
        cpu_after = resource.getrusage(
            resource.RUSAGE_CHILDREN
        )
        #Stop measuring total elapsed time
        end = time.perf_counter()
        elapsed_ms = (end - start) * 1000
        cpu_time_ms = (
            (
                cpu_after.ru_utime
                + cpu_after.ru_stime
            )
            -
            (
                cpu_before.ru_utime
                + cpu_before.ru_stime
            )
        ) * 1000

        full_output = result.stdout + result.stderr
        success = (
            result.returncode == 0
            and "TLSv1.3" in full_output
        )


        byte_match = re.search(
            r"SSL handshake has read (\d+) bytes and written (\d+) bytes",
            full_output
        )

        if byte_match:
            bytes_read = int(byte_match.group(1))
            bytes_written = int(byte_match.group(2))
        else:
            bytes_read = None
            bytes_written = None


        memory_match = re.search(
            r"__MAX_MEMORY_KB__:(\d+)",
            result.stderr
        )

        if memory_match:
            peak_memory_kb = int(memory_match.group(1))
            peak_memory_mb = peak_memory_kb / 1024
        else:
            peak_memory_kb = None
            peak_memory_mb = None


        rows.append({
            #Example: R001
            "run_id": f"R{run_number:03d}",
            #Crypto profile being tested
            "profile": profile,
            #Which repetition this is
            "repetition": repetition,
            #Total time taken
            "elapsed_ms": round(elapsed_ms, 3),
            #CPU time used
            "cpu_time_ms": round(cpu_time_ms, 3),
            # Highest RAM usage
            "peak_memory_mb": (
                round(peak_memory_mb, 3)
                if peak_memory_mb is not None
                else None
            ),
            "bytes_read": bytes_read,
            "bytes_written": bytes_written,
            "success": success
        })


        print(
            profile,
            "| run", repetition,
            "| elapsed:", round(elapsed_ms, 3), "ms",
            "| CPU:", round(cpu_time_ms, 3), "ms",
            "| memory:",
            round(peak_memory_mb, 3)
            if peak_memory_mb is not None
            else None,
            "MB",
            "| read:", bytes_read,
            "| written:", bytes_written,
            "| success:", success
        )


        run_number += 1


with open(output_file, "w", newline="") as file:

    writer = csv.DictWriter(
        file,
        fieldnames=[
            "run_id",
            "profile",
            "repetition",
            "elapsed_ms",
            "cpu_time_ms",
            "peak_memory_mb",
            "bytes_read",
            "bytes_written",
            "success"
        ]
    )

    writer.writeheader()
    writer.writerows(rows)


print()
print("Finished!")
print("Results saved to:", output_file)


# elapsed_ms      = total time
# cpu_time_ms     = CPU work used
# peak_memory_mb  = highest RAM used
# bytes_read      = handshake data received
# bytes_written   = handshake data sent
# success         = did TLS work?