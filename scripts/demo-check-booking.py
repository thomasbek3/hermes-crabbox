"""Protected black-box check: a child exiting zero without an answer cannot pass."""
import argparse,json,os,subprocess,sys,tempfile
parser=argparse.ArgumentParser();parser.add_argument('--workspace',default='/workspace');args=parser.parse_args()
for value,expected in [('2026-09-17',True),('garbage',False),('2026-02-30',False)]:
    code='import sys,json;sys.path.insert(0,sys.argv[1]);from booking import valid_date;print(json.dumps(valid_date(sys.argv[2])))'
    with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
        result=subprocess.run([sys.executable,'-I','-c',code,args.workspace,value],stdout=out,stderr=err,timeout=5)
        out.seek(0);body=out.read(4097)
    if result.returncode or len(body)>4096:
        raise SystemExit('Booking check failed: execution error')
    try:answer=json.loads(body)
    except (ValueError,UnicodeError):raise SystemExit('Booking check failed: missing or malformed answer')
    if answer is not expected:raise SystemExit('Booking check failed: incorrect answer')
print('booking checks passed: valid, malformed and impossible dates')
