"""Evidence-first ranking primitives. Python 3.10+, no dependencies."""
import math
import re
from collections import defaultdict
from urllib.parse import urlsplit

def eligibility_evidence(location, description, title=""):
    # Positive eligibility must describe the candidate's allowed work location,
    # not merely mention a country in an unrelated part of the posting.
    location_text = str(location or "").lower()
    title_text = str(title or "").lower()
    description_text = str(description or "").lower()
    text = (location_text + " " + title_text + " " + description_text)
    regions = r"(?:us|u\.s\.|usa|united states|uk|united kingdom|canada|eu|europe|eea|north america|americas|australia|new zealand|singapore)"
    excluded = re.search(r"\b" + regions + r"\s+only\b|must (?:be|reside|live) (?:based )?in (?:the )?" + regions + r"\b|only applicants? (?:located|based|residing) in\s+" + regions, text)
    india_excluded = re.search(r"\b(?:excluding|except|not available in|not open to)\s+(?:candidates? in\s+)?india\b", text)
    india_location = re.search(r"\b(?:india|indian)\b", location_text)
    india_remote = re.search(r"\b(?:remote|work|hiring|applicants?|candidates?)\b.{0,50}\b(?:from|in|within|based in)\s+india\b|\b(?:india-based|based in india|located in india|applicants? (?:from|in) india)\b", text)
    india = india_location or india_remote
    global_remote = re.search(r"\b(?:worldwide remote|remote worldwide|globally remote|remote[- ]from[- ]anywhere|work remotely from anywhere|work from anywhere|open to candidates worldwide|open worldwide|anywhere in the world)\b", text)
    # A country/region shown as the role location conflicts with boilerplate
    # elsewhere in the description. Keep it unknown unless the field itself
    # clearly says the role is remote/worldwide or names India.
    specific_non_india = bool(re.search(r"\b(?:usa?|united states|canada|united kingdom|uk|ireland|germany|france|spain|netherlands|switzerland|sweden|norway|finland|denmark|poland|portugal|australia|new zealand|singapore|czechia|latam|emea|apac|europe|north america)\b", location_text))
    evidence = []
    for source, subject in (("location", location_text), ("title", title_text), ("description", description_text)):
        india_pattern = r"\b(?:india|indian)\b" if source == "location" else r"\b(?:remote|work|hiring|applicants?|candidates?)\b.{0,50}\b(?:from|in|within|based in)\s+india\b|\b(?:india-based|based in india|located in india|applicants? (?:from|in) india)\b"
        for label, pattern in (("india_exclusion", r"\b(?:excluding|except|not available in) india\b"), ("regional_exclusion", r"\b(?:us|usa|uk|canada|eu|europe)\s+only\b|must (?:be|reside|live) (?:based )?in (?:the )?(?:us|usa|uk|canada|eu|europe)\b|only applicants? (?:located|based|residing) in"), ("india_allowed", india_pattern), ("worldwide_remote", r"\b(?:worldwide|anywhere in the world|globally remote|remote[- ]from[- ]anywhere|work from anywhere)\b")):
            match = re.search(pattern, subject)
            if match:
                evidence.append({"signal": label, "source": source, "text": match.group(0)})
    if india_excluded:
        return "excluded", evidence
    if excluded:
        return ("unknown" if india else "excluded"), evidence
    if india:
        return "eligible_signal", evidence
    if global_remote and not specific_non_india and re.search(r"\b(remote|worldwide|anywhere)\b", location_text):
        return "eligible_signal", evidence
    return "unknown", evidence


def eligibility(location, description):
    return eligibility_evidence(location, description)[0]

def reciprocal_rank_fusion(rankings, k=60):
    scores = defaultdict(float)
    for ranking in rankings:
        for i, key in enumerate(dict.fromkeys(ranking), 1):
            scores[key] += 1 / (k+i)
    return sorted(scores, key=lambda key: (-scores[key], key))

def source_priority(successes, trials, total):
    if not 0 <= successes <= trials <= total:
        raise ValueError("invalid counts")
    return float("inf") if trials == 0 else successes/trials + math.sqrt(2*math.log(max(total,1))/trials)

def canonical_key(job):
    return (job["source"], job["board"], str(job["id"]))

def rank(jobs, skills):
    unique = {canonical_key(j): j for j in jobs}
    candidates = {k:j for k,j in unique.items() if eligibility(j.get("location",""),j.get("description","")) != "excluded"}
    def fit(j):
        text = (j.get("title","")+" "+j.get("description","")).lower()
        return sum(bool(re.search(r"(?<!\w)"+re.escape(s.lower())+r"(?!\w)",text)) for s in skills)
    relevant = sorted(candidates, key=lambda k: (-fit(candidates[k]),k))
    geographic = sorted(candidates, key=lambda k: (eligibility(candidates[k].get("location",""),candidates[k].get("description","")) != "eligible_signal",k))
    return [dict(candidates[k], skill_matches=fit(candidates[k]), eligibility=eligibility(candidates[k].get("location",""),candidates[k].get("description","")), eligibility_evidence=eligibility_evidence(candidates[k].get("location",""),candidates[k].get("description",""))[1]) for k in reciprocal_rank_fusion([relevant,geographic])]
