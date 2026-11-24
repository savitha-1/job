import unittest
from discover import normalize, endpoint
from engine import rank, reciprocal_rank_fusion, source_priority

class Tests(unittest.TestCase):
    def test_greenhouse(self):
        j=normalize("greenhouse","demo",{"jobs":[{"id":1,"title":"Go Engineer","location":{"name":"India"},"absolute_url":"https://example.com/job/1","content":"<p>Python contact hiring@example.org</p>"}]})[0]
        self.assertEqual(j["location"],"India")
        self.assertIn("Python",j["description"])
        self.assertNotIn("hiring@example.org",j["description"])
        self.assertEqual(rank([j,j],["Go","Python"])[0]["skill_matches"],2)
        self.assertEqual(len(rank([j,j],["Go"])),1)
    def test_lever(self):
        self.assertEqual(normalize("lever","demo",[{"id":"a","text":"Developer","categories":{"location":"US only"},"applyUrl":"https://example.com/a","descriptionPlain":"Go"}])[0]["title"],"Developer")
    def test_exclusion(self):
        j=dict(source="lever",board="demo",id=1,title="Go",location="US only",description="")
        self.assertEqual(rank([j],["Go"]),[])

    def test_eligibility_needs_location_evidence_and_preserves_conflict(self):
        from engine import eligibility
        self.assertEqual(eligibility("Bengaluru, India", "Backend Engineer"), "eligible_signal")
        self.assertEqual(eligibility("Remote", "We have an India office. Backend Engineer."), "unknown")
        self.assertEqual(eligibility("Remote", "US only. Backend Engineer."), "excluded")
        self.assertEqual(eligibility("Remote", "Worldwide remote except India."), "excluded")
    def test_eligibility_does_not_promote_country_roles_from_boilerplate(self):
        from engine import eligibility
        self.assertEqual(eligibility("USA", "We are a worldwide company. Python role."), "unknown")
        self.assertEqual(eligibility("Spain", "Work from anywhere. We are hiring globally."), "unknown")
        self.assertEqual(eligibility("APAC", "This role is open to India-based candidates."), "eligible_signal")
        self.assertEqual(eligibility("Remote", "Remote worldwide. Applicants from any country."), "eligible_signal")
        from engine import eligibility_evidence
        result, evidence = eligibility_evidence("USA", "100% remote.", title="Senior Engineer (USA Only)")
        self.assertEqual(result, "excluded")
        self.assertTrue(any(item["source"] == "title" for item in evidence))
    def test_ashby(self):
        self.assertEqual(normalize("ashby","demo",{"jobs":[{"title":"Hidden","isListed":False}]}),[])
    def test_slug(self):
        with self.assertRaises(ValueError):
            endpoint("lever","../bad")
    def test_fusion(self):
        self.assertEqual(reciprocal_rank_fusion([["a","b"],["b"]])[0],"b")
    def test_exploration(self):
        self.assertGreater(source_priority(0,0,10),source_priority(3,5,10))

if __name__=="__main__":
    unittest.main()
