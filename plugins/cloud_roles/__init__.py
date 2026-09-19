"""Pinned cloud-only role handlers; configuration must be a trusted read-only mount."""
import json
from cloudworkbench.role_broker import BrokerError
from cloudworkbench.role_broker_transport import RoleClient


def make_handlers(client):
    def handler(action, args, *, session_id=None, tool_call_id=None, **ignored):
        try:
            if type(args) is not dict:
                raise BrokerError(400,'invalid_role_arguments')
            allowed = {'role','task','context_refs'} if action=='cloud_request_roles' else {'request_id','cursor'}
            required = {'role','task'} if action=='cloud_request_roles' else {'request_id'}
            if not required <= set(args) or not set(args) <= allowed:
                raise BrokerError(400,'invalid_role_arguments')
            result=client.call(action,args,session_id=session_id,tool_call_id=tool_call_id)
            return json.dumps({'result':result},separators=(',',':'))
        except BrokerError as error:
            return json.dumps({'error':error.code,'status':error.status_code})
    return {name:(lambda args,_name=name,**kwargs:handler(_name,args,**kwargs))
            for name in ('cloud_request_roles','cloud_get_role_results')}


def register(ctx):
    handlers=make_handlers(RoleClient.from_file())
    schemas={
        'cloud_request_roles':{'role':{'type':'string'},'task':{'type':'string'},'context_refs':{'type':'array','maxItems':32,'items':{'type':'object','properties':{'artifact_id':{'type':'string'},'sha256':{'type':'string'}},'required':['artifact_id','sha256'],'additionalProperties':False}}},
        'cloud_get_role_results':{'request_id':{'type':'string'},'cursor':{'type':'null'}},
    }
    for name,handler in handlers.items():
        ctx.register_tool(name=name,toolset='cloud_roles',schema={
            'name':name,'description':'Request a controller-authorized role or read its durable pending receipt. Scheduling is not enabled.',
            'parameters':{'type':'object','properties':schemas[name],
                          'required':['role','task'] if name=='cloud_request_roles' else ['request_id'],
                          'additionalProperties':False}},handler=handler)
