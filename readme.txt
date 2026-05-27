Purpose
-------
Email pipeline: staged format extraction, Apify/Million Verifier validation,
manifest-tracked cycles, partial-run resume, and DB updates.

Cycle layout
------------
  cycles/data/              extract_*.csv, validated_*.csv
  cycles/manifests/       extraction_manifest.csv, validation_manifest.csv
  cycles/state/           employee_email_state.csv (per-employee format results)
  logs/                   extract_cycle_*.log, validate_cycle_*.log,
                          validate_emails_*.log, update_db_cycle_*.log

Format cascade (auto — same as CVL send_linkedin_campaigns_params.py)
---------------------------------------------------------------------
  1. firstname.lastname
  2. firstname              (when #1 INVALID)
  3. firstinitial.lastname  (when #1–2 INVALID)
  4. firstname.lastinitial  (when #1–3 INVALID)

extract_cycle.py picks the earliest stage that still has eligible employees.

Typical workflow
----------------
1. Copy provider_config.template.json → provider_config.json (Apify token).
2. pip install -r requirements.txt

Windows (double-click or cmd):
  run_cycle.bat
  run_cycle.bat 500
  run_cycle_resume.bat          (after partial validation)

One full cycle (500 emails, format auto):
  python run_cycle.py

Or step by step:
  python extract_cycle.py --limit 500
  python validate_cycle.py
  python update_db_cycle.py

If Apify quota stops validation mid-run:
  python validate_cycle.py --resume
  python update_db_cycle.py

Re-run run_cycle.bat until extract reports no eligible employees (all four stages done).

Inspect manifests:
  python validate_cycle.py --list-pending
  python update_db_cycle.py --list-ready

Legacy scripts (still work):
  extract_unsent_emails_500.py, validate_emails.py, update_db_with_validation.py
