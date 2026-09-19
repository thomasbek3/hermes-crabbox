"""Inert authentication of generic terminal-root cleanup; never delivery authority."""
from dataclasses import asdict
import hashlib
import json
import math
import re
import sqlite3

from .scheduler import _cleanup_target
from .store import StoreError


class RootCleanupHistoryError(ValueError):
    def __init__(self, code='root_cleanup_history_invalid'):
        self.code=code
        super().__init__(code)


def _require(value):
    if not value:raise RootCleanupHistoryError()


def _raw(value, *, ascii=False):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=ascii,allow_nan=False).encode()


def _hash(raw):return hashlib.sha256(raw).hexdigest()


def _decode(raw, maximum, *, ascii=False):
    _require(type(raw) is str and 0<len(raw.encode())<=maximum)
    def pairs(items):
        value={}
        for key,item in items:
            _require(key not in value);value[key]=item
        return value
    value=json.loads(raw,object_pairs_hook=pairs,
        parse_constant=lambda _:(_ for _ in ()).throw(RootCleanupHistoryError()))
    _require(_raw(value,ascii=ascii).decode()==raw)
    return value


def _identifier(value):
    return type(value) is str and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value) is not None


def _digest(value):return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def _positive(value):return type(value) is int and 0<value<=2**63-1


def _time(value):return type(value) in (int,float) and math.isfinite(value) and 0<=value<=10**11


def _receipt(value,target,*,inspector=None):
    keys={'target','outcome','observed_at','evidence_sha256'}
    if inspector is not None:keys.add('inspector_id')
    _require(type(value) is dict and set(value)==keys
        and _raw(value['target'])==_raw(target)
        and value['outcome'] in ('terminated','fenced')
        and _time(value['observed_at']) and _time(target['frozen_at'])
        and value['observed_at']>=target['frozen_at'] and _digest(value['evidence_sha256']))
    if inspector is not None:_require(value['inspector_id']==inspector)


_DISPATCH_KEYS=('request_id','attempt_id','generation','launch_nonce','profile_digest','payload_digest',
    'provider_name','gateway_name','internal_network_name','external_network_name','provider_id',
    'gateway_id','internal_network_id','external_network_id','state','version','operation','uncertain',
    'deadline_at','operation_receipt')


def _provider_proof(db,row,reservation,target):
    value=_decode(row['cleanup_receipt'],2*1024**2,ascii=True)
    _require(type(value) is dict and type(value.get('target')) is dict)
    scope=value['target']
    _require(set(scope)=={'scope','reservation','request_id','grant_id','attempt_id','generation',
        'frozen_at','cleanup_id','dispatches'} and scope['scope']=='owner'
        and _raw(scope['reservation'])==_raw(asdict(reservation))
        and scope['cleanup_id']==row['cleanup_id'] and _identifier(scope['cleanup_id'])
        and _time(scope['frozen_at']) and scope['frozen_at']==row['frozen_at'])
    request_fields=[scope[k] for k in ('request_id','grant_id','attempt_id','generation')]
    if any(v is not None for v in request_fields):
        _require(all(_identifier(v) for v in request_fields[:3]) and _positive(request_fields[3]))
        request=db.execute('SELECT * FROM provider_request_leases WHERE id=?',(scope['request_id'],)).fetchone()
        grant=db.execute('SELECT * FROM provider_execution_grants WHERE id=?',(scope['grant_id'],)).fetchone()
        _require(request is not None and grant is not None and request['state']=='released'
            and request['grant_id']==grant['id'] and request['reservation_id']==grant['reservation_id']==reservation.reservation_id
            and (grant['attempt_id'],grant['generation'])==(scope['attempt_id'],scope['generation'])
            and any(a.attempt_id==scope['attempt_id'] and a.generation==scope['generation'] for a in target.attempts))
    dispatches=scope['dispatches'];_require(type(dispatches) is list and len(dispatches)<=1024)
    seen=set()
    for binding in dispatches:
        _require(type(binding) is list and len(binding)==len(_DISPATCH_KEYS)
            and all(type(pair) is list and len(pair)==2 for pair in binding)
            and [pair[0] for pair in binding]==list(_DISPATCH_KEYS))
        frozen=dict(binding)
        _require(_identifier(frozen['request_id']) and frozen['request_id'] not in seen
            and _identifier(frozen['attempt_id']) and _positive(frozen['generation'])
            and _positive(frozen['version']) and frozen['state']=='cleaning'
            and frozen['operation']=='cleanup' and type(frozen['uncertain']) is int and frozen['uncertain']==1)
        seen.add(frozen['request_id'])
        current=db.execute('SELECT * FROM provider_dispatch WHERE request_id=?',(frozen['request_id'],)).fetchone()
        _require(current is not None and current['reservation_id']==reservation.reservation_id
            and current['state'] in ('cleaned','delivered') and current['operation'] is None and current['uncertain']==0
            and any(a.attempt_id==frozen['attempt_id'] and a.generation==frozen['generation'] for a in target.attempts)
            and _raw({k:current[k] for k in _DISPATCH_KEYS[:14]})==_raw({k:frozen[k] for k in _DISPATCH_KEYS[:14]})
            and current['deadline_at']==frozen['deadline_at'])
    _receipt(value,scope,inspector=target.inspector_id)
    return _hash(row['cleanup_receipt'].encode())


def _load(db,root_id,generation):
    _require(db.in_transaction and _identifier(root_id) and type(generation) is int and 0<generation<=2**31)
    root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
    saved=db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?',(root_id,)).fetchone()
    _require(root is not None and saved is not None and root['state']=='released'
        and root['child_attempt_id'] is None and root['generation']==generation)
    target_value=_decode(saved['target'],128*1024)
    _require(type(target_value) is dict and set(target_value)=={'root_id','generation','reservation_version',
        'controller_instance_id','cleanup_id','frozen_at','attempts','reservations','inspector_id'})
    target=_cleanup_target(saved['target'])
    _require(_raw(asdict(target))==_raw(target_value) and target.root_id==root_id
        and target.generation==saved['generation']==generation and _time(target.frozen_at)
        and target.controller_instance_id==saved['controller_instance_id']
        and target.cleanup_id==saved['cleanup_id'] and root['version']==target.reservation_version+1)
    rows=db.execute('SELECT * FROM attempts WHERE workflow_root_id=? ORDER BY id LIMIT 34',(root_id,)).fetchall()
    _require(1<=len(rows)<=33 and len(rows)==len(target.attempts))
    bindings=[{k:r[column] for k,column in (('attempt_id','id'),('generation','generation'),
        ('execution_kind','execution_kind'),('state','state'),('runtime_id','runtime_id'))} for r in rows]
    _require(_raw(bindings)==_raw([asdict(a) for a in target.attempts])
        and any(r['id']==root_id and r['generation']==generation and r['execution_kind']=='hermes_root'
            and (r['session_id'],r['turn_id'])==(root['session_id'],root['turn_id']) for r in rows)
        and all((r['session_id'],r['turn_id'])==(root['session_id'],root['turn_id']) for r in rows))
    runtime=_decode(saved['runtime_receipt'],256*1024)
    _receipt(runtime,target_value)
    release=_decode(saved['release_receipt'],128*1024)
    _require(type(release) is dict and set(release)=={'root_id','generation','cleanup_id',
        'runtime_receipt_sha256','provider_receipt_sha256','released_at'}
        and type(release['released_at']) is str and 0<len(release['released_at'])<=64
        and not any(ord(c)<32 for c in release['released_at']))
    frozen=_decode(root['frozen'],2*1024**2)
    _require(_hash(root['frozen'].encode())==root['frozen_digest'] and type(frozen) is dict
        and frozen.get('accounts')=={r.account_id:r.persistent_owner_id for r in target.reservations})
    accounts=db.execute('SELECT account_id,reservation_id FROM workflow_accounts WHERE root_id=? ORDER BY account_id LIMIT 9',(root_id,)).fetchall()
    _require([(r['account_id'],r['reservation_id']) for r in accounts]==
        [(r.account_id,r.reservation_id) for r in target.reservations])
    providers={}
    for reservation in target.reservations:
        row=db.execute('SELECT * FROM provider_reservations WHERE id=?',(reservation.reservation_id,)).fetchone()
        _require(row is not None and row['state']=='released'
            and (row['account_id'],row['epoch'],row['controller_instance_id'])==
                (reservation.account_id,reservation.epoch,reservation.controller_instance_id))
        providers[reservation.reservation_id]=_provider_proof(db,row,reservation,target)
    expected={'root_id':root_id,'generation':generation,'cleanup_id':target.cleanup_id,
        'runtime_receipt_sha256':_hash(saved['runtime_receipt'].encode()),
        'provider_receipt_sha256':providers,'released_at':release['released_at']}
    _require(_raw(release)==_raw(expected))
    events=db.execute("SELECT payload FROM events WHERE attempt_id=? AND session_id=? AND type='workflow.root_released' LIMIT 2",
        (root_id,root['session_id'])).fetchall()
    _require(len(events)==1 and _raw(_decode(events[0]['payload'],128*1024))==_raw(release))
    return release


def load_root_cleanup(db,root_id,expected_generation):
    """Authenticate the original generic cleanup in a caller-owned transaction.

    This grants no execution, publication or new cleanup authority. It reads no
    active account pointers and never calls a runtime, provider, or filesystem.
    Historical proof rows must remain retained; missing or corrupt evidence fails
    closed. Delivery-specific cleanup receipts use their own historical reader.
    """
    try:return _load(db,root_id,expected_generation)
    except RootCleanupHistoryError:raise
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,RecursionError,sqlite3.Error,StoreError):
        raise RootCleanupHistoryError() from None
