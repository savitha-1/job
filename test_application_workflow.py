import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from application_workflow import ApplicationWorkflow
from ledger import Ledger


class WorkflowTests(unittest.TestCase):
    def test_concurrent_ledger_deduplicates_employer_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "apps.sqlite")
            def add(i):
                return ledger.enqueue(f"https://jobs.example/role/{i}", "Acme", "batch-1")
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(add, range(8)))
            self.assertEqual(sum(outcomes), 1)

    def test_reusable_approval_and_multi_step_checkpoint_state(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = ApplicationWorkflow(Path(directory) / "local" / "apps.sqlite")
            workflow_id = workflow.create("https://jobs.example/apply/1", "Acme", "batch-1", automation_permitted=True)
            self.assertEqual(workflow.create("https://jobs.example/apply/1", "acme", "batch-1"), workflow_id)
            field = {"id": "q1", "label": "Years of professional experience", "type": "text", "required": True}
            plan, pending = workflow.plan_step(workflow_id, 0, [field], {})
            self.assertTrue(plan["review"])
            self.assertEqual(len(pending), 1)
            self.assertEqual(workflow.get(workflow_id)["state"], "human_required")
            workflow.approve_answer(pending[0]["question_key"], "3")
            plan, pending = workflow.plan_step(workflow_id, 0, [field], {})
            self.assertFalse(pending)
            self.assertEqual(plan["actions"][0]["value"], "3")
            self.assertEqual(workflow.get(workflow_id)["state"], "ready")
            _, pending_again = workflow.plan_step(workflow_id, 1, [dict(field, id="q2")], {})
            self.assertFalse(pending_again)

    def test_submit_crash_recovers_as_uncertain_and_cannot_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "apps.sqlite"
            workflow = ApplicationWorkflow(path)
            wid = workflow.create("https://jobs.example/apply/1", "Acme", "batch-1", automation_permitted=True)
            workflow.transition(wid, "review", "preparing")
            workflow.transition(wid, "preparing", "ready")
            workflow.authorize_submission(wid, "https://jobs.example/apply/1", "single application to Acme role 1")
            self.assertTrue(workflow.begin_submission(wid))
            recovered = ApplicationWorkflow(path)
            self.assertEqual(recovered.get(wid)["state"], "uncertain_submission")
            self.assertFalse(recovered.begin_submission(wid))
            self.assertTrue(recovered.confirm(wid, "gmail-message:fixture-1"))
            self.assertEqual(recovered.get(wid)["state"], "confirmed")

    def test_workflow_blocks_second_employer_opportunity_in_same_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = ApplicationWorkflow(Path(directory) / "apps.sqlite")
            workflow.create("https://jobs.example/apply/1", "Acme", "batch-1")
            with self.assertRaises(ValueError):
                workflow.create("https://jobs.example/apply/2", "acme", "batch-1")


if __name__ == "__main__":
    unittest.main()
