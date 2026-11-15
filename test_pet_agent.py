from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from cultivation.__main__ import example, run_turn
from cultivation.phase4.contracts import Fact
from style.pet_style import catalog


class NavigationSmoke(IsolatedAsyncioTestCase):
    async def test_reviewed_navigation(self):
        req = example("想把跑步计划的后续安排放慢一些")
        req.facts = [Fact(evidenceRef="goal-run", objectId="goal-run", title="跑步计划",
                          kind="goal", revision=1, data={"status": "active"})]
        req.consentSnapshot.planningRead = True
        req.consentSnapshot.allowedFields = ["facts.goal"]
        calls = []

        class Model:
            async def reply(self, *, role, system_prompt, context, output_schema):
                calls.append(role)
                if role == "C0":
                    assert "refinePlan" in system_prompt
                    return dict(replyText="可以去调整这份计划。", responsePriority="ordinary",
                                intents=["planning"], editGoalRef="goal-run", editAction="refinePlan",
                                evidenceRefs=["goal-run"])
                assert role == "C1"
                return dict(accepted=True, issues=[], evidenceRefs=["goal-run"])

        with patch.dict("os.environ", {"PET_MODEL_URL": ""}):
            result = await run_turn(req, model=Model())
        self.assertEqual(calls, ["C0", "C1"])
        self.assertEqual((result["editAction"], result["editGoalId"]), ("refinePlan", "goal-run"))
        self.assertFalse(result["formalWrites"] or result["handoffProposals"])
        self.assertIn("俏皮嘴硬", {card["label"] for card in catalog()["personalities"]})
