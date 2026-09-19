> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Control-plane scheduler checkpoint

Owned checkpoint completed locally;404tests passed in14.19sec,3 existingdependency/pytestmetadatawarnings. evidence/scheduler-workflow-final-tests.xml/log and final-binding.json containexactsource/testhashes; frozenownedfiles in evidence/scheduler-workflow-final-source. No liveDB/schema/service/runtime/provider operations. No active commands remain.

Fable firstcodecall budgetexhausted/noverdict; preserved. Onecorrectiveretry producedvalidREVISE; scheduler-code-disposition.md independentlyfixes restarttransitionbypass, cleanupownerfence, exactproviderguardSQL, migrationconflictdiagnostics. OldfrozenStoreactualDBmigration/oldbinaryrejectiontests added. Fable reviewed earlier snapshot; neworderedstepaddition was parentdirected and locallyverified, not labeledFablePASS.

APIs stable docs/ROLE-SCHEDULER-CHECKPOINT.md. Explicit Store.migrate_scheduler() (defaultschema1unchanged), RoleScheduler enqueue/admitroot/nativebrokerrequest->serialchild; rootreserves2slots. LegacyStore.transition refusesrouted; use scheduler.transition withactualowner predicate. claim/activelegacy excludesrouted. child_assignment getsnative task/profile + workflowstep/inputrevision. Cleanuprequires currentowner+exactCASreceipt; noforceclear.

Orderedworkflow: frozen.provenance.workflow receipt fromparentadapter; immutable stepsIDs/ordinals; admit_request(...,step_id,input_revision_sha256,plan) requires allpriorpassgates and locksassignedrevisionbeforelaunch. record_step_gate(child_id,expected_generation,decision,revision_sha256,reviewed_artifact_id,reviewed_artifact_sha256,evidence_sha256) requires exactchild/generationartifact, metadatareviewedrevisionmatchesprelaunchassignment, completed/verifiedforpass. Rejectneveradvances; repeatrolesnotcollapsed. Rootcompleted denieduntilallstepspass+allchildren terminal+cleanupslotreleased. Completedrootstillholdsaccount/globalcapacity.

Parentowns workflow_routing/pstack_routing/workflow_submission; artifactsdelegateownsAPI/bundledefaultrootselectorfix. Preservedtheirchanges; integratedtests included. Remainingreleasegates: typedruntime/workspace/native launch; verifyactualrevisionbytes+Pstackrefhashes; real qualification/evaluatorproof; rootcleanup/recovery/release (no liveactivationuntildone); routedgenerationrecovery/coldfollowup; phasedeadlines andverifieddeliverypromotion. Parentwillcontinueverticalexecution.

## Subsequent root cleanup and native Docker checkpoints

Root cleanup/release implemented and frozen in evidence/root-cleanup-final-source; binding SHA e8eb621c02dcda18b836a98110cc6447b48d2d10f7be817214cf8d613eb3de2c. 210 tests passed. Two bounded Fable attempts produced no verdict (budget/turn limits); reviews/root-cleanup-review-status.md preserves this unreviewed boundary. Parent directed no third attempt and independent native Docker progress. No root source edits after freeze.

Native Docker branch owned only provider_docker.py, new test_provider_docker_native.py and checkpoint/evidence. Exact NativeProfile profile/digest selects readonly native-auth.json <=64KiB; historical claude-token <=4096 unchanged. Controller credential checks metadata only. Same provider_main command. Exact mount set/unique destinations and pre-start profile validation. docs/PROVIDER-DOCKER-NATIVE-CHECKPOINT.md lists remaining review and live activation gates. Parent account-limit notice forbids starting Fable until limit clears; no Fable call for native unit, no claimed review verdict. No live changes.
