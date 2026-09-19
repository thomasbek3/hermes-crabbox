from pathlib import Path
import subprocess,sys
CHECK=Path(__file__).resolve().parents[1]/'scripts/demo-check-booking.py'

def test_exit_zero_without_behavior_cannot_forge_verification(tmp_path):
    (tmp_path/'booking.py').write_text('raise SystemExit(0)\n')
    run=subprocess.run([sys.executable,str(CHECK),'--workspace',str(tmp_path)],capture_output=True)
    assert run.returncode!=0

def test_true_for_every_date_fails(tmp_path):
    (tmp_path/'booking.py').write_text('def valid_date(value): return True\n')
    run=subprocess.run([sys.executable,str(CHECK),'--workspace',str(tmp_path)],capture_output=True)
    assert run.returncode!=0

def test_correct_behavior_passes(tmp_path):
    (tmp_path/'booking.py').write_text('def valid_date(value):\n from datetime import date\n try: date.fromisoformat(value); return True\n except ValueError: return False\n')
    run=subprocess.run([sys.executable,str(CHECK),'--workspace',str(tmp_path)],capture_output=True)
    assert run.returncode==0
