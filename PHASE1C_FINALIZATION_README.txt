CAACCS Phase 1C development dataset finalization v1

Verified source repository: fastcel/CAACCS, branch phase-1c
Known dataset source checkpoint: 4ba1151ec9d13c740a70a2af323ccdb85d38be2a

1. Put these files in the CAACCS FYP repository root:
   - phase1c_finalize_development_v1.py
   - PHASE1C_DATASET_SPEC_v1.md

2. From that directory, run:
   python3 phase1c_validate_v2.py --all
   python3 phase1c_finalize_development_v1.py

3. Expected after the second command:
   Status: PASS (development dataset only)
   Scenarios: 30 of 30 development
   Attempted connections: 1800
   Successful connections: 1800
   Failed connections: 0
   Derived scenario-by-profile rows: 90

4. Outputs are created in data/phase1c_dataset_v1/: 
   - development_profile_summary.csv
   - development_source_manifest.csv
   - development_audit_report.txt

5. Review outputs, then commit only the two v1 source files and the v1 directory:
   git add phase1c_finalize_development_v1.py PHASE1C_DATASET_SPEC_v1.md data/phase1c_dataset_v1/
   git commit -m "Freeze Phase 1C development dataset specification and audit"
   git push

IMPORTANT: The six held-out scenarios are not collected by this tool, and must remain
unopened until the separately frozen evaluation protocol and model-selection procedure
are established. No held-out TLS experiments are run by these files.
