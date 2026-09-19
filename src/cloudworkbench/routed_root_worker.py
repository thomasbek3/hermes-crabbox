"""Trusted queued-root consumer composing fixed stages and protected terminalization.

This is not an HTTP endpoint or a recovery/takeover loop. The host supplies a
qualified stage factory which owns privileged provisioning and all partial handles.
Already-started roots require explicit reconciliation, never another invocation.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat

from .delivery_schema import verify_schema
from .role_broker import ParentScope, RoleBroker
from .routed_delivery import load_root_delivery, prepare_root_delivery, finalize_root_delivery
from .routed_observation_store import validate_forbidden_values
from .routed_progression import register_root_input, stage_inputs, _input, VERSION
from .routed_publication import _directory
from .routed_stage_decision import load_finalized_stage, load_stage_release
from .routed_decision import load_finalized_task, load_task_release
from .routed_stop import close_stopped_workflow, load_stopped_workflow
from .scheduler import _child_control_tx
from .stage_contracts import VERSION as CONTRACT_VERSION
from .store import StoreError
from .workflow_revisions import WorkspaceRevision, RevisionBinding, verify_revision
from .workflow_routing import resolve_catalog


@dataclass(frozen=True)
class StageExecution:
    runtime: object = field(repr=False)
    prepared: object = field(repr=False)
    spec: object = field(repr=False)
    execution_profile: object = field(repr=False)
    services: tuple = field(repr=False)
    options: dict = field(repr=False)


@dataclass(frozen=True)
class RootRunReceipt:
    root_id: str
    generation: int
    disposition: str
    receipt: object | None


def _require(value, detail):
    if not value: raise StoreError(409, detail)


class RoutedRootWorker:
    def __init__(self, scheduler, *, stage_factory, cleanup_verifier, delivery_root, forbidden_values):
        if not callable(stage_factory) or not callable(cleanup_verifier):
            raise ValueError('Trusted routed factories required')
        self.scheduler=scheduler
        self.stage_factory=stage_factory
        self.cleanup_verifier=cleanup_verifier
        self.delivery_root=delivery_root
        self.forbidden_values=validate_forbidden_values(forbidden_values)
        self.executions={}

    def run_root(self, root_id, *, expected_generation, initial_revision, external_running=0):
        """Consume one fresh root; return queued if capacity is busy, or a saved result.

        Any exception after admission retains scheduler ownership and factory/runtime
        handles. This method does not reset states, retry launch, or free unknown work.
        Terminal replay authenticates the existing result without provisioning.
        """
        s=self.scheduler
        with _child_control_tx(s.store,write=False) as db:
            verify_schema(db)
            row=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
            _require(row is not None and type(expected_generation) is int and row['generation']==expected_generation,
                'Routed root identity unavailable')
            root=dict(row);attempt=s.store._attempt(db,root_id)
            if attempt['state']=='completed' and root['state']=='released':
                result=attempt['result'];terminal='stopped' if 'routed_stopped' in result else 'delivered'
            else:
                terminal=None
                _require(attempt['state']=='queued' and root['state']=='queued' and not attempt['cancel_requested']
                    and not root['cancel_requested'],'Started routed root requires explicit reconciliation')
                client=db.execute('SELECT * FROM clients WHERE id=?',(root['owner_id'],)).fetchone()
                session=db.execute('SELECT * FROM sessions WHERE id=?',(root['session_id'],)).fetchone()
                _require(client is not None and client['revoked_at'] is None and 'submit' in json.loads(client['scopes'])
                    and root['project_id'] in json.loads(client['projects']) and not session['archived'],
                    'Routed root submission authority unavailable')
                frozen=json.loads(root['frozen']);provenance=frozen['provenance'];plan=provenance['workflow']
                _require(hashlib.sha256(root['frozen'].encode()).hexdigest()==root['frozen_digest']
                    and provenance.get('stage_progression_version')==VERSION
                    and provenance.get('stage_contract_version')==CONTRACT_VERSION
                    and 'environment' in provenance,'Qualified routed root required')
                catalog=resolve_catalog(plan['workflow'],plan['workflow_policy_sha256'])
                _require([(v['id'],v['role']) for v in plan['steps']]==[(v.id,v.role) for v in catalog.steps],
                    'Frozen routed catalog changed')
                base_row=db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?',(root_id,)).fetchone()
                _require(base_row is not None and base_row['generation']==expected_generation,
                    'Frozen root delivery base unavailable')
                base=dict(base_row)
                if 'remediation' in provenance:
                    _require(type(initial_revision) is WorkspaceRevision
                        and _input(db,root)['sha256']==initial_revision.sha256,'Prepared remediation input differs')
        if terminal:
            loader=load_stopped_workflow if terminal=='stopped' else load_root_delivery
            return RootRunReceipt(root_id,expected_generation,terminal,
                loader(s,root_id,expected_generation=expected_generation,forbidden_values=self.forbidden_values))
        binding=RevisionBinding(root['owner_id'],root['project_id'],root['session_id'],root['turn_id'],
            root_id,expected_generation,root_id,expected_generation)
        _require(type(initial_revision) is WorkspaceRevision and initial_revision.binding==binding,
            'Exact root input revision required')
        verify_revision(s.store,initial_revision)
        storage=Path(self.delivery_root)
        fd=_directory(storage,private=True)
        try:
            _require(stat.S_IMODE(os.fstat(fd).st_mode) in (0o700,0o2700),
                'Private root delivery storage required')
        finally:
            os.close(fd)
        _require(storage!=initial_revision.path and storage not in initial_revision.path.parents
            and initial_revision.path not in storage.parents,'Root delivery storage overlaps input')
        task=attempt['request']['goal']
        _require(0<len(task.encode())<=32768,'Routed stage task exceeds broker bound')
        admitted=s.admit_root(root_id,accounts=frozen['accounts'],external_running=external_running)
        if admitted is None:return RootRunReceipt(root_id,expected_generation,'queued',None)
        s.transition(root_id,'running',expected_generation=expected_generation)
        scope=ParentScope(root['owner_id'],root['project_id'],root_id,root_id,expected_generation,
            'root-worker-'+root_id)
        register_root_input(s,scope,initial_revision)
        broker=RoleBroker(s.store.path,authorize_parent=s.authorize_parent,clock=s.clock)
        with _child_control_tx(s.store,write=False) as db:
            deadline=db.execute('SELECT deadline_at FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()[0]
        grant,token=broker.issue(scope,roles=tuple(dict.fromkeys(v.role for v in catalog.steps)),expires_at=deadline)
        try:
            return self._run_stages(root_id,expected_generation,initial_revision,task,frozen,catalog,base,scope,broker,grant,token)
        except BaseException as error:
            try:
                broker.revoke(grant)
            except Exception:
                error.add_note('Root broker revocation unconfirmed; ownership and runtime handles remain retained.')
            raise

    def _run_stages(self,root_id,expected_generation,initial_revision,task,frozen,catalog,base,scope,broker,grant,token):
        s=self.scheduler
        revision=initial_revision
        from .routed_stage_runner import run_prepared_stage, CompletedStage
        for step in catalog.steps:
            chosen=stage_inputs(s,scope,step.id)
            _require(chosen['input_revision_sha256']==revision.sha256,'Worker carried revision differs')
            pending=broker.prepare(token,native_session_id=scope.native_session_id,native_call_id=step.id,
                role=step.role,task=task,context_refs=tuple(chosen['context_refs']))
            s.admit_request(broker,scope,pending['id'],plan=frozen['role_plans'][step.role],
                step_id=step.id,input_revision_sha256=revision.sha256)
            child=s.claim_child(root_id)
            _require(child is not None,'Routed child claim unavailable')
            assignment=s.child_assignment(child['id'],expected_generation=child['generation'])
            execution=self.stage_factory(child,revision,assignment)
            _require(type(execution) is StageExecution,'Trusted stage execution required')
            self.executions[child['id']]=execution
            _require(type(execution.options) is dict and type(execution.services) is tuple,
                'Trusted stage execution required')
            completed=run_prepared_stage(s,execution.runtime,execution.prepared,execution.spec,
                execution_profile=execution.execution_profile,services=execution.services,input_revision=revision,
                forbidden_values=self.forbidden_values,**execution.options)
            _require(type(completed) is CompletedStage,'Completed stage receipt required')
            with _child_control_tx(s.store,write=False) as db:
                task_stage=step.role=='acceptance_verification'
                body,metadata=(load_finalized_task if task_stage else load_finalized_stage)(db,child['id'],
                    forbidden_values=self.forbidden_values)
                released=(load_task_release if task_stage else load_stage_release)(db,child['id'],
                    forbidden_values=self.forbidden_values)
                _require(released is not None and metadata['sha256']==completed.decision.sha256
                    and body['carried_revision_sha256']==completed.carried_revision.sha256
                    and body['root_id']==root_id and body['step_id']==step.id,'Worker finalized stage differs')
            revision=completed.carried_revision
            if body['gate']!='pass':
                broker.revoke(grant)
                stopped=close_stopped_workflow(s,root_id,expected_generation=expected_generation,
                    cleanup_verifier=self.cleanup_verifier,forbidden_values=self.forbidden_values)
                return RootRunReceipt(root_id,expected_generation,'stopped',stopped)
        broker.revoke(grant)
        prepared=prepare_root_delivery(s,root_id,expected_generation=expected_generation,
            expected_delivery_version=base['delivery_version'],expected_base_revision_sha256=base['revision_sha256'],
            selected_revision=revision,storage_root=self.delivery_root,forbidden_values=self.forbidden_values)
        delivered=finalize_root_delivery(s,prepared,cleanup_verifier=self.cleanup_verifier,
            forbidden_values=self.forbidden_values)
        return RootRunReceipt(root_id,expected_generation,'delivered',delivered)
