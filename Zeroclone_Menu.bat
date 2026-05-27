@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Wide layout so descriptions use the terminal width. Tweak ZC_COLS if text wraps oddly on your monitor.
set "ZC_COLS=160"
call :WIDE_CONSOLE

set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 set "PY=python"

:MAIN
call :RULE_LINE
echo  Zeroclone - email extraction / Million Verifier / SQLite pipeline menu
call :RULE_LINE
echo  Working directory: %CD%
echo  Python launcher:   %PY%
echo  Key folders here:  cycles\data  cycles\manifests  cycles\state  logs
echo  Default database:  linkedin_data.db under sibling CVL-ScraperLinkedIn_SendMails or CVL ^(see cycle_registry.py^)
echo.
echo  1  run_cycle.py
echo      Default "one job" orchestrator. Chains four stages without you remembering order: extract_cycle builds the next
echo      extract_*.csv from SQLite + manifests, validate_cycle sends those emails through Apify MV and writes validated_*.csv,
echo      update_db_cycle merges MV fields into zerobounce_validation and updates employee_email_state / company probe flags,
echo      then runs apply_validation_views.py in the CVL repo so dependent SQL views match the new validation snapshot.
echo      Equivalent menu path: Option 1 internally performs Option 2 ^(extract_cycle.py^), then Option 3 ^(validate_cycle.py^),
echo      then Option 4 ^(update_db_cycle.py^), plus a final views refresh step ^(apply_validation_views.py^) that is not a main option.
echo      Use when you want the whole loop in one shot ^(Task Scheduler friendly^). If Apify quota stops mid-flight, exit 3:
echo      rerun validate_cycle.py --resume, then update_db_cycle.py, or pick resume options inside this menu's run_cycle page.
echo.
echo  2  extract_cycle.py
echo      Stage 1 only: decides which guessed email formats to try per company using a probe-then-expand strategy to save
echo      Apify credits. Reads employees from linkedin_data.db, consults company_format_state.csv and employee_email_state.csv,
echo      and refuses a new extract while any row in extraction_manifest.csv is still pending_validation ^(finish validate first^).
echo      Output lands in cycles\data as extract_{format}_{timestamp}_cNNNN.csv plus a new extraction_manifest row for tracking.
echo      Tuning knobs: --limit caps total generated addresses per run, --probe-size controls how many probe emails per format.
echo.
echo  3  validate_cycle.py
echo      Stage 2 only: picks the next pending extraction manifest row ^(or a specific --extraction-cycle^), batches emails to
echo      the configured Apify Million Verifier actor, and streams progress into validation_manifest.csv for auditing partial runs.
echo      Writes cycles\data\validated_*.csv with MV JSON columns your DB importer expects. Supports --resume after API limits,
echo      and --list-pending to inspect queues without spending credits. Shares validation core code with validate_emails.py.
echo.
echo  4  update_db_cycle.py
echo      Stage 3 only: takes a completed validation_manifest entry whose notes are not yet db_updated, replays the validated CSV
echo      into linkedin_data.db, stamps source_batch values for traceability, refreshes per-employee format columns, and applies
echo      company-level probe outcomes back into company_format_state.csv. Creates an automatic SQLite backup unless --no-backup.
echo      Run only after validate_cycle reports success or you have intentionally resumed a partial file to completion.
echo.
echo  5  update_db_with_validation.py
echo      Escape hatch for CSVs produced outside the manifest workflow - e.g. legacy experiments or manual Apify downloads that
echo      already match the MV column layout. Imports straight into zerobounce_validation with configurable --source-batch labels.
echo      Default CSV is validated_unsent_500.csv beside this script; default DB path resolves like the cycle scripts. Does not
echo      advance extraction_manifest / validation_manifest rows, so treat it as a surgical merge rather than the main pipeline.
echo.
echo  6  validate_emails.py
echo      Standalone MV batch runner for arbitrary CSVs - specify --input/--output or rely on provider_config.json defaults.
echo      Ideal for ad-hoc lists unrelated to cycle manifests. Honors --resume to skip rows already written with raw JSON, and
echo      exposes --email-column plus Apify permission overrides when corporate policies require LIMITED_PERMISSIONS explicitly.
echo.
echo  7  Show full flag reference ^(no run^)
echo      Dumps a concise command cheat sheet covering every argparse flag from each script - safe when you only need syntax
echo      reminders or want to copy commands into another terminal without executing Python here.
echo.
echo  0  Exit
echo      Leave the menu and return to your shell with the current console width unchanged for the rest of the session.
call :RULE_LINE
set /p M=Choice: 
if "%M%"=="1" goto MENU_RUN_CYCLE
if "%M%"=="2" goto MENU_EXTRACT
if "%M%"=="3" goto MENU_VALIDATE_CYCLE
if "%M%"=="4" goto MENU_UPDATE_DB_CYCLE
if "%M%"=="5" goto MENU_UPDATE_DB_CSV
if "%M%"=="6" goto MENU_VALIDATE_EMAILS
if "%M%"=="7" goto SHOW_FLAGS
if "%M%"=="0" goto END
goto MAIN

:WIDE_CONSOLE
mode con: cols=%ZC_COLS% lines=9999 >nul 2>&1
powershell -NoProfile -NoLogo -Command "try { $c=%ZC_COLS%; $buf=$Host.UI.RawUI.BufferSize; $win=$Host.UI.RawUI.WindowSize; $Host.UI.RawUI.BufferSize = New-Object System.Management.Automation.Host.Size([Math]::Max($c,$buf.Width),[Math]::Max(9999,$buf.Height)); $max=[Math]::Min($c,$Host.UI.RawUI.MaxPhysicalWindowWidth); if ($win.Width -lt $max) { $Host.UI.RawUI.WindowSize = New-Object System.Management.Automation.Host.Size($max,[Math]::Min($win.Height,$Host.UI.RawUI.MaxWindowSize.Height)) } } catch { }" >nul 2>&1
exit /b 0

:RULE_LINE
powershell -NoProfile -NoLogo -Command "try { $b=[Console]::BufferWidth } catch { $b=0 }; $n=[Math]::Max(%ZC_COLS%,$b); if ($n -gt 240) { $n=240 }; Write-Host ('=' * $n)" 2>nul
if errorlevel 1 (
  echo ================================================================================================================================================================================
)
exit /b 0

:PAUSE_MAIN
echo.
pause
goto MAIN

:MENU_RUN_CYCLE
call :RULE_LINE
echo  run_cycle.py: orchestrated wrapper around extract / validate / db / views
call :RULE_LINE
echo  Flags you can mirror in option 6:
echo    --limit N                  forwarded to extract_cycle ^(default 500 candidates per invocation^)
echo    --steps a,b,c,d            comma list subset of: extract, validate, db, views; default runs all four in order
echo    --resume-validation        append --resume when spawning validate_cycle after a partial Apify run
echo    --extraction-cycle N       force validate_cycle to target a specific extraction manifest id
echo    --validation-cycle N       pass through to validate_cycle / update_db_cycle when resuming known cycle ids
echo.
echo  Preset actions:
echo    1  Default full chain at --limit 500: best everyday choice when manifests are clean and Apify quota is healthy.
echo    2  Same chain but prompts for a custom --limit before launching ^(use for smaller test batches or large catch-up jobs^).
echo    3  Stops after validate ^(--steps extract,validate^) so you can inspect CSVs before SQLite writes or CVL view refresh.
echo    4  Prompts for arbitrary trailing flags ^(example: --resume-validation --validation-cycle 2 --steps validate,db,views^).
echo    5  Runs python run_cycle.py --help for argparse text straight from the source file.
echo    6  Advanced: supply the entire argument list yourself after the script name ^(quotes respected by cmd if you add them^).
echo    B  Return to the main Zeroclone menu without executing Python.
call :RULE_LINE
set /p RC=Choice: 
if /i "%RC%"=="B" goto MAIN
if "%RC%"=="1" (
  %PY% run_cycle.py
  goto PAUSE_MAIN
)
if "%RC%"=="2" (
  set /p LIM=Limit default 500: 
  if "%LIM%"=="" set LIM=500
  %PY% run_cycle.py --limit %LIM%
  goto PAUSE_MAIN
)
if "%RC%"=="3" (
  %PY% run_cycle.py --steps extract,validate
  goto PAUSE_MAIN
)
if "%RC%"=="4" (
  echo Example: --resume-validation --validation-cycle 2
  set /p EXTRA=Extra args: 
  %PY% run_cycle.py %EXTRA%
  goto PAUSE_MAIN
)
if "%RC%"=="5" (
  %PY% run_cycle.py --help
  goto PAUSE_MAIN
)
if "%RC%"=="6" (
  set /p EXTRA=All args after run_cycle.py: 
  %PY% run_cycle.py %EXTRA%
  goto PAUSE_MAIN
)
goto MENU_RUN_CYCLE

:MENU_EXTRACT
call :RULE_LINE
echo  extract_cycle.py: manifest-aware extraction stage
call :RULE_LINE
echo  Common flags ^(override via option 5^):
echo    --limit N              hard cap on generated email rows for this run ^(default 500^)
echo    --probe-size N         override PROBE_SAMPLE_SIZE for how many employees validate per company per format probe
echo    --db PATH              alternate linkedin_data.db location; default resolved via cycle_registry.resolve_cvl_root
echo    --cycle-number N       force a specific manifest cycle id when you need deterministic filenames / auditing
echo    --tag STRING           embed a custom tag in extract filenames instead of the autogenerated timestamp slug
echo.
echo  Preset actions:
echo    1  Run with defaults: respects pending-validation guardrails and writes the next extract batch + manifest metadata.
echo    2  Prompts for --limit only ^(blank keeps 500^) while leaving other defaults untouched.
echo    3  Prompts for both --limit and --probe-size so you can shrink probes during experiments or widen them cautiously.
echo    4  Display argparse help text from extract_cycle.py without mutating manifests.
echo    5  Freeform arguments: useful when you need a one-off --db path or combined overrides not covered above.
echo    B  Back to the main menu.
call :RULE_LINE
set /p EC=Choice: 
if /i "%EC%"=="B" goto MAIN
if "%EC%"=="1" (
  %PY% extract_cycle.py
  goto PAUSE_MAIN
)
if "%EC%"=="2" (
  set /p LIM=Limit default 500: 
  if "%LIM%"=="" set LIM=500
  %PY% extract_cycle.py --limit %LIM%
  goto PAUSE_MAIN
)
if "%EC%"=="3" (
  set /p LIM=Limit default 500: 
  if "%LIM%"=="" set LIM=500
  set /p PSZ=Probe size: 
  %PY% extract_cycle.py --limit %LIM% --probe-size %PSZ%
  goto PAUSE_MAIN
)
if "%EC%"=="4" (
  %PY% extract_cycle.py --help
  goto PAUSE_MAIN
)
if "%EC%"=="5" (
  set /p EXTRA=All args after extract_cycle.py: 
  %PY% extract_cycle.py %EXTRA%
  goto PAUSE_MAIN
)
goto MENU_EXTRACT

:MENU_VALIDATE_CYCLE
call :RULE_LINE
echo  validate_cycle.py: Apify-powered validation tied to extraction manifests
call :RULE_LINE
echo  Flags worth memorizing:
echo    --config PATH            JSON with Apify token, actor id, batch sizes; defaults to provider_config.json beside scripts
echo    --extraction-cycle N      validate a specific extraction manifest cycle instead of the next pending_validation row
echo    --validation-cycle N     resume an existing validation manifest row ^(pairs with --resume when CSV partial^)
echo    --resume                  continue writing the partially completed validated CSV after actor throttling or quotas
echo    --batch-size N            override MV batch chunk size without editing provider_config.json for a single run
echo    --list-pending            read-only manifest dump to see what still needs validation credits before you spend them
echo.
echo  Preset actions:
echo    1  Auto-pick the next pending_validation extraction: typical daily driver once extract_cycle has queued work.
echo    2  Run --list-pending to print manifest diagnostics with zero Apify spend.
echo    3  Prompt for a specific extraction cycle id when you need to revalidate or catch up an older extract file deliberately.
echo    4  Prompted freeform args: combine --resume, --validation-cycle, or --extraction-cycle as your situation demands.
echo    5  argparse --help output for deeper field descriptions straight from validate_cycle.py.
echo    6  Advanced passthrough identical to option 4 but explicitly labeled for long multi-flag pastes.
echo    B  Back to the main menu.
call :RULE_LINE
set /p VC=Choice: 
if /i "%VC%"=="B" goto MAIN
if "%VC%"=="1" (
  %PY% validate_cycle.py
  goto PAUSE_MAIN
)
if "%VC%"=="2" (
  %PY% validate_cycle.py --list-pending
  goto PAUSE_MAIN
)
if "%VC%"=="3" (
  set /p N=Extraction cycle number: 
  %PY% validate_cycle.py --extraction-cycle %N%
  goto PAUSE_MAIN
)
if "%VC%"=="4" (
  echo Example: --extraction-cycle 3 --resume
  set /p EXTRA=Extra args: 
  %PY% validate_cycle.py %EXTRA%
  goto PAUSE_MAIN
)
if "%VC%"=="5" (
  %PY% validate_cycle.py --help
  goto PAUSE_MAIN
)
if "%VC%"=="6" (
  set /p EXTRA=All args after validate_cycle.py: 
  %PY% validate_cycle.py %EXTRA%
  goto PAUSE_MAIN
)
goto MENU_VALIDATE_CYCLE

:MENU_UPDATE_DB_CYCLE
call :RULE_LINE
echo  update_db_cycle.py: SQLite + employee state merge for completed validations
call :RULE_LINE
echo  Flags explained:
echo    --db PATH                     override linkedin_data.db target ^(defaults next to resolved CVL project root^)
echo    --validation-cycle N          choose which validation_manifest row to apply ^(blank auto-picks next completed row^)
echo    --source-batch-prefix TEXT    prefix for mv_cycle_batch ids written into zerobounce_validation.source_batch values
echo    --no-backup                   skip the automatic linkedin_data.db.timestamped backup before mutating tables
echo    --list-ready                  print validation_manifest rows showing which cycles still await DB application
echo.
echo  Preset actions:
echo    1  Auto-apply the next completed validation that still needs DB writes - safest default after a good validate_cycle pass.
echo    2  List-ready mode only - inspect backlog / statuses without touching SQLite yet ^(good sanity check after audits^).
echo    3  Prompted validation cycle id - use when multiple completed CSVs exist and you must apply them strictly in order.
echo    4  Same as 3 but adds --no-backup for scripted environments where snapshots already exist ^(still prompts for cycle id^).
echo    5  argparse --help for exhaustive field notes from update_db_cycle.py.
echo    6  Freeform arguments for unusual combinations ^(alternate --db path plus --source-batch-prefix tweaks, etc.^).
echo    B  Back to the main menu.
call :RULE_LINE
set /p UD=Choice: 
if /i "%UD%"=="B" goto MAIN
if "%UD%"=="1" (
  %PY% update_db_cycle.py
  goto PAUSE_MAIN
)
if "%UD%"=="2" (
  %PY% update_db_cycle.py --list-ready
  goto PAUSE_MAIN
)
if "%UD%"=="3" (
  set /p N=Validation cycle number: 
  %PY% update_db_cycle.py --validation-cycle %N%
  goto PAUSE_MAIN
)
if "%UD%"=="4" (
  set /p N=Validation cycle number: 
  %PY% update_db_cycle.py --validation-cycle %N% --no-backup
  goto PAUSE_MAIN
)
if "%UD%"=="5" (
  %PY% update_db_cycle.py --help
  goto PAUSE_MAIN
)
if "%UD%"=="6" (
  set /p EXTRA=All args after update_db_cycle.py: 
  %PY% update_db_cycle.py %EXTRA%
  goto PAUSE_MAIN
)
goto MENU_UPDATE_DB_CYCLE

:MENU_UPDATE_DB_CSV
call :RULE_LINE
echo  update_db_with_validation.py: direct CSV import into zerobounce_validation
call :RULE_LINE
echo  Arguments mirror a simplified ETL:
echo    --csv PATH              input MV-compatible CSV ^(defaults to validated_unsent_500.csv co-located with this menu^)
echo    --db PATH               destination linkedin_data.db - defaults through cycle_registry like the cycle-aware scripts
echo    --source-batch NAME     label stored on inserted rows so downstream analytics can filter imports vs live Apify batches
echo    --no-backup             skip the safety copy of SQLite before large merges ^(only when disk space is extremely tight^)
echo.
echo  Preset actions:
echo    1  Run with stock defaults - quickest way to load the canonical validated_unsent_500.csv sample into production SQLite.
echo    2  Prompt only for a CSV path - keeps default DB but swaps the input file ^(wrap paths containing spaces in quotes^).
echo    3  Prompt for CSV plus source-batch label - ideal when importing multiple third-party files in one maintenance window.
echo    4  argparse --help for column expectations and conflict behavior straight from update_db_with_validation.py.
echo    5  Fully custom trailing arguments for exotic combinations ^(alternate DB on D: drives, etc.^).
echo    B  Back to the main menu.
call :RULE_LINE
set /p UC=Choice: 
if /i "%UC%"=="B" goto MAIN
if "%UC%"=="1" (
  %PY% update_db_with_validation.py
  goto PAUSE_MAIN
)
if "%UC%"=="2" (
  set /p CSV=CSV path: 
  %PY% update_db_with_validation.py --csv "%CSV%"
  goto PAUSE_MAIN
)
if "%UC%"=="3" (
  set /p CSV=CSV path: 
  set /p SB=Source batch name: 
  %PY% update_db_with_validation.py --csv "%CSV%" --source-batch "%SB%"
  goto PAUSE_MAIN
)
if "%UC%"=="4" (
  %PY% update_db_with_validation.py --help
  goto PAUSE_MAIN
)
if "%UC%"=="5" (
  set /p EXTRA=All args after update_db_with_validation.py: 
  %PY% update_db_with_validation.py %EXTRA%
  goto PAUSE_MAIN
)
goto MENU_UPDATE_DB_CSV

:MENU_VALIDATE_EMAILS
call :RULE_LINE
echo  validate_emails.py: generic MV batch runner decoupled from cycle manifests
call :RULE_LINE
echo  Important behaviors:
echo    Loads provider_config.json first so corporate defaults ^(batch size, actor id, IO paths^) propagate unless CLI overrides win.
echo    --input / --output         explicit CSV paths - omit both to fall back to config defaults or built-in emails.csv names
echo    --batch-size N             tune Apify chunking per run without editing JSON secrets on shared machines
echo    --email-column NAME        disambiguate messy spreadsheets where MV must not guess the wrong column containing addresses
echo    --force-permission-level   switch between LIMITED_PERMISSIONS and FULL_PERMISSIONS when Apify prompts change mid-project
echo    --resume                   skip rows already validated in the output CSV that still contain validation_raw_json payloads
echo.
echo  Preset actions:
echo    1  Launch with config-driven defaults - perfect quick action when provider_config.json already encodes your standard paths.
echo    2  Prompt for explicit input/output CSVs - isolates ad-hoc files from the cycle pipeline artifacts under cycles\data.
echo    3  Prompted arbitrary flags - paste combinations like "--resume --batch-size 50" without navigating other menus first.
echo    4  argparse --help dump for validate_emails.py field-by-field documentation.
echo    5  Same as 3 but labeled for long multi-flag pastes copied from runbooks or ticketing systems.
echo    B  Back to the main menu.
call :RULE_LINE
set /p VE=Choice: 
if /i "%VE%"=="B" goto MAIN
if "%VE%"=="1" (
  %PY% validate_emails.py
  goto PAUSE_MAIN
)
if "%VE%"=="2" (
  set /p INP=Input CSV: 
  set /p OUTP=Output CSV: 
  %PY% validate_emails.py --input "%INP%" --output "%OUTP%"
  goto PAUSE_MAIN
)
if "%VE%"=="3" (
  echo Example: --resume  OR  --input x.csv --output y.csv --resume
  set /p EXTRA=Extra args: 
  %PY% validate_emails.py %EXTRA%
  goto PAUSE_MAIN
)
if "%VE%"=="4" (
  %PY% validate_emails.py --help
  goto PAUSE_MAIN
)
if "%VE%"=="5" (
  set /p EXTRA=All args after validate_emails.py: 
  %PY% validate_emails.py %EXTRA%
  goto PAUSE_MAIN
)
goto MENU_VALIDATE_EMAILS

:SHOW_FLAGS
call :RULE_LINE
echo  Full flag reference: copy/paste friendly cheat sheet ^(no Python execution in this section^)
call :RULE_LINE
echo  run_cycle.py
echo    python run_cycle.py [--limit N] [--steps extract,validate,db,views] [--resume-validation] [--extraction-cycle N] [--validation-cycle N]
echo      --steps accepts any comma subset; defaults to all four. Resume flags forward to validate_cycle when you continue partial MV runs.
echo.
echo  extract_cycle.py
echo    python extract_cycle.py [--limit N] [--probe-size N] [--db PATH] [--cycle-number N] [--tag TAG]
echo      Refuses work if extraction_manifest still has pending_validation rows - clear validate_cycle before expecting new extracts.
echo.
echo  validate_cycle.py
echo    python validate_cycle.py [--config PATH] [--extraction-cycle N] [--validation-cycle N] [--resume] [--batch-size N] [--list-pending]
echo      Uses validate_emails.py internals - provider_config.json must include a usable Apify token and Million Verifier actor id.
echo.
echo  update_db_cycle.py
echo    python update_db_cycle.py [--db PATH] [--validation-cycle N] [--source-batch-prefix PREFIX] [--no-backup] [--list-ready]
echo      Applies validation_manifest rows in completion order unless you pin a cycle id; backs up SQLite unless --no-backup is set.
echo.
echo  update_db_with_validation.py
echo    python update_db_with_validation.py [--csv PATH] [--db PATH] [--source-batch NAME] [--no-backup]
echo      Expects MV-shaped columns compatible with update_db_with_validation.parse_row_to_record - review script header for schema.
echo.
echo  validate_emails.py
echo    python validate_emails.py [--config PATH] [--input PATH] [--output PATH] [--batch-size N] [--email-column COL] [--force-permission-level FULL_PERMISSIONS^|LIMITED_PERMISSIONS] [--resume]
echo      Completely ignores cycles\manifests - use strictly for ad-hoc CSV validation workflows outside the extract/validate manifests.
echo.
echo  Path resolution reminder:
echo    cycle_registry.resolve_cvl_root searches sibling folders named CVL-ScraperLinkedIn_SendMails then CVL for linkedin_data.db
echo    or scripts\apply_validation_views.py - symlink or rename consistently so every stage hits the same SQLite + CVL checkout.
call :RULE_LINE
goto PAUSE_MAIN

:END
endlocal
exit /b 0
