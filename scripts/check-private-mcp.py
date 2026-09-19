"""Read-only live MCP smoke. Credential is read privately; never emitted."""
import asyncio, hashlib, json, os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integrations/omarchy-cloud/scripts'))
from omarchy_cloud import Client
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    origin='https://omarchy.tail0d5eb6.ts.net'
    secret=Client(origin,Path.home()/'.config/omarchy-cloud/token').token
    async with streamablehttp_client(origin+'/mcp',headers={'Authorization':'Bearer '+secret}) as (read,write,_):
        async with ClientSession(read,write) as session:
            init=await session.initialize()
            names=[t.name for t in (await session.list_tools()).tools]
            assert len(names)==10
            guide=await session.call_tool('get_delegation_guide',{})
            assert not guide.isError and 'CONTRIBUTING.md' in guide.structuredContent['skill']
            listing=await session.call_tool('list_tasks',{'limit':2})
            assert not listing.isError
            checks=[]
            for row in listing.structuredContent['sessions'][:1]:
                for name in ('get_task','get_events','get_results'):
                    r=await session.call_tool(name,{'session_id':row['id']})
                    assert not r.isError,(name,'failed')
                    checks.append(name)
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False) as client:
        package=await client.get(origin+'/mcp/skill.zip',headers={'Authorization':'Bearer '+secret});package.raise_for_status()
        denied=await client.post(origin+'/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        assert denied.status_code==401
        bad=await client.post(origin+'/mcp',headers={'Authorization':'Bearer '+'invalid-'*8},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        assert bad.status_code==401
        api=await client.get(origin+'/v1/health');assert api.status_code==200
    print(json.dumps({'server':init.serverInfo.name,'tools':names,'existing_task_read_checks':checks,
        'skill_download_bytes':len(package.content),'skill_sha256':hashlib.sha256(package.content).hexdigest(),
        'missing_auth_status':denied.status_code,'invalid_auth_status':bad.status_code,'existing_api_status':api.status_code,
        'mutations':0,'model_calls':0},indent=2))

if __name__=='__main__':asyncio.run(main())
