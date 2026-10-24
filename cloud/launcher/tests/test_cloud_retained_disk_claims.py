"""#1228 review fixes on the retained disk: a claim of the derived name is attached or deleted only when it is this launcher's own (labels and
size), a failed create REPORTS the claim it keeps (and a Pending one with its storage class and message), and the cap counts Mi.
Mutants are built from the real source. Offline, fake cluster."""
import argparse
import contextlib
import io
import unittest

import test_cloud_retained_disk as td
from test_cloud_retained_disk import CLAIM, NAME, WORKER, Rig, cw, mutant


def own(name=CLAIM, gib=8, **meta):
    labels = {"app": cw.LABEL, "cw/worker": WORKER}
    labels.update(meta.pop("labels", {}))
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": dict({"name": name, "labels": labels, "creationTimestamp": cw.iso(td.T0)}, **meta),
            "spec": {"resources": {"requests": {"storage": "%dGi" % gib}}, "storageClassName": "fast"}}


def foreign(name=CLAIM):
    c = own(name)
    c["metadata"]["labels"] = {"app": "other-service"}
    return c


def attach_refused(mod, claim, **kw):
    r = Rig(mod)
    r.kube.claims[CLAIM] = claim
    try:
        r.launch(**kw)
    except mod.CloudError as e:
        assert e.code == mod.EXIT_REFUSED, e.code
        assert not r.kube.sets and not r.kube.secrets, "nothing may be made"
        return str(e)
    raise AssertionError("a claim that is not ours was attached")


def delete_refused(mod, claim):
    r = Rig(mod)
    r.kube.claims[CLAIM] = claim
    try:
        r.L.delete_disk(argparse.Namespace(worker=WORKER, reason=""))
    except mod.CloudError as e:
        assert e.code == mod.EXIT_REFUSED
        assert CLAIM in r.kube.claims, "the claim was deleted"
        return str(e)
    raise AssertionError("a claim that is not ours was deleted")


class Ownership(unittest.TestCase):
    def test_foreign_claim_refused_for_attach_and_for_delete(self):
        for c in (foreign(), {"metadata": {"name": CLAIM}, "spec": {}}):
            self.assertIn("not this launcher's claim", attach_refused(cw, c))
            self.assertIn("not this launcher's claim", delete_refused(cw, c))
        print("2 of 2 foreign claims refused for both")

    def test_other_workers_label_refused(self):
        c = own()
        c["metadata"]["labels"]["cw/worker"] = "someone-else"
        attach_refused(cw, c)
        delete_refused(cw, c)

    def test_smaller_claim_refused_larger_and_equal_attach(self):
        self.assertIn("less than", attach_refused(cw, own(gib=4), disk="8"))
        for gib in (8, 16):
            r = Rig()
            r.kube.claims[CLAIM] = own(gib=gib)
            r.launch()
            self.assertEqual(len(r.kube.claims), 1)

    def test_own_claim_still_deleted(self):
        r = Rig()
        r.kube.claims[CLAIM] = own()
        out = r.L.delete_disk(argparse.Namespace(worker=WORKER, reason=""))
        self.assertEqual(out["deleted_claims"], [CLAIM])

    def test_mutants_without_the_check_are_caught(self):
        with self.assertRaises(AssertionError):
            attach_refused(mutant("                if bad:\n                    raise CloudError(bad, EXIT_REFUSED)\n", ""), foreign())
        with self.assertRaises(AssertionError):
            delete_refused(mutant("                if bad:\n                    raise CloudError(bad, EXIT_REFUSED)\n                self.kube.delete_claim(c)", "                self.kube.delete_claim(c)"), foreign())


class FailedCreate(unittest.TestCase):
    def failed(self, r, **kw):
        try:
            r.launch(**kw)
        except Exception as e:   # a mutant build has its own CloudError class
            return str(e)
        raise AssertionError("the create should fail")

    def test_pod_create_failure_reports_the_kept_claim(self):
        r = Rig()
        r.L._nap = lambda seconds: None
        real = r.kube.apply

        def boom(obj):
            real(obj)
            if obj["kind"] == "Pod":
                raise cw.CloudError("pod create refused")
        r.kube.apply = boom
        msg = self.failed(r, role="worker", disk="4")
        self.assertIn("disk claim data-%s is kept" % NAME, msg)
        self.assertIn("delete it with: cloud-worker delete-disk %s" % WORKER, msg)
        self.assertIn("data-%s" % NAME, r.kube.claims)   # it really is kept

    def test_wait_timeout_reports_the_kept_claim(self):
        r = Rig()
        r.kube.hold = True
        r.L._nap = lambda seconds: None   # no real sleeping: the wait's own second bound ends it   # the controller never makes the pod: the wait times out
        msg = self.failed(r)
        self.assertIn("timed out", msg)
        self.assertIn("disk claim %s is kept" % CLAIM, msg)

    def test_pending_claim_names_class_and_message(self):
        r = Rig()
        r.kube.hold = True
        r.L._nap = lambda seconds: None   # no real sleeping: the wait's own second bound ends it
        c = own()
        c["status"] = {"phase": "Pending", "conditions": [{"type": "Pending", "message": 'storageclass.storage.k8s.io "fast" not found'}]}
        r.kube.claims[CLAIM] = c
        msg = self.failed(r)
        self.assertIn("is Pending (storage class fast: storageclass.storage.k8s.io", msg)
        self.assertIn("delete-disk", msg)

    def test_mutant_without_the_report_is_caught(self):
        r = Rig(mutant('                dnote = self._disk_note(worker, name, persistent) if disk else ""', '                dnote = ""'))
        r.kube.hold = True
        r.L._nap = lambda seconds: None   # no real sleeping: the wait's own second bound ends it
        with self.assertRaises(AssertionError):
            self.assertIn("is kept", self.failed(r))


class Sizes(unittest.TestCase):
    def test_gib_of_counts_mi_rounded_up(self):
        def c(s):
            return {"spec": {"resources": {"requests": {"storage": s}}}}
        self.assertEqual([cw.gib_of(c(x)) for x in ("8Gi", "512Mi", "1024Mi", "1025Mi", "1Ti", "3")], [8, 1, 1, 2, 1024, 0])


if __name__ == "__main__":
    unittest.main()
