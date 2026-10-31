cd /work/repo
run(){ echo "== $*"; python3 bin/test-switchboard "$@" 2>&1 | grep -E "^(Ran|OK|FAILED|ERROR:|FAIL:)"; }
run TestMadeUpPids TestSupportRebinding TestEveryViewDerivesFromTheRegistry TestHygiene
run TestPR8Source TestPR10Source TestOneCharter TestRecordJsonReaders TestEachThingOnce TestAgentRegistryInventory TestTheBinSetClassesAreAllInTheTable
run TestAssembler TestArchitectureDocument TestGitShim TestDeliveryLag TestSentView TestLaunch TestHeavyWatchdog
echo "== files"
python3 -m unittest tests/switchboard/test_architecture.py 2>&1 | tail -3
python3 -m unittest tests/switchboard/test_closure.py 2>&1 | tail -3
tools/derive-closure --check >/dev/null 2>&1; echo derive rc=$?
python3 -m unittest cloud/launcher/tests/test_cloud_quick_scans_run.py 2>&1 | tail -3
echo DONE
