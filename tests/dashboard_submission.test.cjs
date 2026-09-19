const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),crypto=require('node:crypto').webcrypto;
function setup(fetch){
 const nodes=new Map();function node(){return {value:'',textContent:'',hidden:false,disabled:false,children:[],handlers:{},addEventListener(n,f){this.handlers[n]=f},replaceChildren(...n){this.children=n},append(...n){this.children.push(...n)},setAttribute(){},focus(){}}}
 const get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)};
 const context=vm.createContext({document:{getElementById:get,createElement:node,hidden:false},fetch,crypto,AbortController,setInterval(){},setTimeout,clearTimeout,console});
 let source=fs.readFileSync('src/cloudworkbench/static/dashboard.html','utf8').match(/<script nonce="__CSP_NONCE__">([\s\S]*?)<\/script>/)[1];
 source=source.replace("api('/auth/session').then(signedIn).catch(()=>signedOut());",'');vm.runInContext(source,context);
 vm.runInContext("config={scopes:['submit','observe'],input_max_bytes:100,projects:[{id:'p',agents:['fixture'],environment_versions:['v1']}]};csrf='test';",context);
 get('project').value='p';get('agent').value='fixture';get('environment').value='v1';get('goal').value='test goal';
 return {get,context,submit:()=>get('create-form').handlers.submit({preventDefault(){}})};
}
const reply=(status,data)=>({ok:status>=200&&status<300,status,json:async()=>data});
test('a created task stays confirmed when refresh fails',async()=>{
 let posts=0;const s=setup(async(path,opts)=>{if(opts.method==='POST'){posts++;return reply(201,{session_id:'created-id'})}return reply(500,{detail:'synthetic refresh fault'})});
 await s.submit();assert.equal(posts,1);assert.match(s.get('notice').textContent,/Task created.*created-id/);assert.doesNotMatch(s.get('upload-status').textContent,/Not confirmed/);
});
test('unknown final create outcome freezes edits and retries exact key/body',async()=>{
 const calls=[];const s=setup(async(path,opts)=>{if(opts.method==='POST'){calls.push({key:opts.headers['Idempotency-Key'],body:opts.body});throw Error('synthetic lost response')}return reply(200,{sessions:[]})});
 await s.submit();assert.equal(s.get('goal').disabled,true);assert.equal(s.get('submit-task').disabled,false);
 await s.submit();assert.deepEqual(calls[0],calls[1]);
});
test('HTML 401 clears private form state before JSON decoding',async()=>{
 const s=setup(async()=>({ok:false,status:401,json:async()=>{throw Error('HTML')}}));
 await assert.rejects(vm.runInContext("api('/v1/sessions')",s.context));assert.equal(s.get('goal').value,'');assert.equal(s.get('login').hidden,false);assert.equal(s.get('detail').hidden,true);
});
function attach(s){s.context.testBytes=new Uint8Array([0,255,128]);vm.runInContext("attachments=[{file:{name:'fixture.bin',size:3,type:'application/octet-stream',arrayBuffer:async()=>testBytes.buffer},reserveKey:'reserve-key',uploadKey:'upload-key',id:null,ready:false}]",s.context)}
test('lost PUT response retries same reservation and upload key',async()=>{
 let reserves=0,puts=0;const keys=[];const s=setup(async(path,opts)=>{
  if(path==='/v1/inputs'){reserves++;return reply(201,{id:'input-id',state:'pending'})}
  if(opts.method==='PUT'){puts++;keys.push(opts.headers['Idempotency-Key']);if(puts===1)throw Error('synthetic lost upload response');return reply(200,{id:'input-id',state:'ready',bytes:3,sha256:require('node:crypto').createHash('sha256').update(Buffer.from([0,255,128])).digest('hex')})}
  if(path==='/v1/sessions'&&opts.method==='POST')return reply(201,{session_id:'created'});
  return reply(500,{detail:'synthetic refresh fault'});
 });attach(s);await s.submit();await s.submit();assert.equal(reserves,1);assert.equal(puts,2);assert.deepEqual(keys,['upload-key','upload-key']);
});
test('wrong uploaded digest blocks task creation and explains recovery',async()=>{
 let creates=0;const s=setup(async(path,opts)=>{
  if(path==='/v1/inputs')return reply(201,{id:'i'});
  if(opts.method==='PUT')return reply(200,{id:'i',state:'ready',bytes:3,sha256:'incorrect'});
  creates++;return reply(201,{});
 });attach(s);await s.submit();assert.equal(creates,0);assert.match(s.get('notice').textContent,/Remove and reselect/);
});
test('stop submission aborts stalled upload and preserves retry state',async()=>{
 let uploading;const started=new Promise(r=>uploading=r);const s=setup(async(path,opts)=>{
  if(path==='/v1/inputs')return reply(201,{id:'i'});
  if(opts.method==='PUT'){uploading();return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Error('aborted'))))}
  throw Error('unexpected request');
 });attach(s);const pending=s.submit();await started;assert.equal(s.get('stop-upload').hidden,false);s.get('stop-upload').handlers.click();await pending;assert.equal(s.get('submit-task').disabled,false);assert.equal(vm.runInContext('attachments[0].id',s.context),'i');
});
test('logout clears UI even if logout endpoint returns 401',async()=>{
 const s=setup(async()=>reply(401,{detail:'expired'}));await s.get('logout').handlers.click();assert.equal(s.get('goal').value,'');assert.equal(s.get('detail').hidden,true);assert.equal(s.get('login').hidden,false);
});
test('unknown upload limits fail closed before reserving',()=>{
 const s=setup(async()=>{throw Error('must not fetch')});vm.runInContext('delete config.input_max_bytes',s.context);s.get('input-files').files=[{name:'f',size:0}];s.get('input-files').handlers.change();assert.match(s.get('notice').textContent,/limits unavailable/);assert.equal(vm.runInContext('attachments.length',s.context),0);
});
