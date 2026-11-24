"""Deterministic application form planning. No submission or credential handling."""
import re
from pathlib import Path

RULES = {
    "first_name": r"\b(first|given)\s*name\b",
    "last_name": r"\b(last|family|sur)\s*name\b",
    "full_name": r"\b(full|legal)\s*name\b|^name$",
    "email": r"\b(e-?mail)\b",
    "phone": r"\b(phone|mobile|telephone)\b",
    "country": r"\bcountry\b|country/region",
    "city": r"\bcity\b|town",
    "state": r"\bstate\b|province|region",
    "postal_code": r"\b(postal|zip)\s*code\b",
    "address": r"\b(address|street)\b",
    "education_level": r"\b(degree|education level|qualification)\b",
    "school": r"\b(school|university|college|institution)\b",
    "field_of_study": r"\b(major|field of study|discipline)\b",
    "graduation_date": r"\b(graduation|graduated|completion)\b",
    "current_company": r"\b(current|most recent) employer\b|\bcompany name\b",
    "current_title": r"\b(current|most recent) (job )?title\b|\bjob title\b",
    "years_experience": r"\b(years? of experience|years? experience)\b",
    "start_date": r"\b(start|from) date\b",
    "end_date": r"\b(end|to) date\b",
    "cover_letter": r"\b(cover letter|motivation letter)\b",
    "linkedin": r"\blinkedin\b",
    "github": r"\bgithub\b",
    "portfolio": r"\b(portfolio|personal website)\b",
    "resume": r"\b(resume|résumé|cv|curriculum vitae)\b",
}
SENSITIVE = re.compile(r"authorization|authorisation|visa|sponsor|citizen|disability|gender|race|ethnic|criminal|consent|agree|certify|signature|salary|assessment|captcha", re.I)

def classify(field):
    label = " ".join(str(field.get(k) or "") for k in ("label","name","placeholder"))
    label = re.sub(r"[_-]", " ", label).strip().lower()
    if SENSITIVE.search(label):
        return None
    matches = [key for key, pattern in RULES.items() if re.search(pattern,label,re.I)]
    if field.get("type") == "file":
        return matches[0] if len(matches)==1 and matches[0] in ("resume", "cover_letter") else None
    return matches[0] if len(matches)==1 and matches[0]!="resume" else None

def plan(fields, profile):
    actions, review = [], []
    for field in fields:
        if field.get("disabled"):
            continue
        key=classify(field)
        if "approved_value" in field:
            key="approved_answer"
            value=field["approved_value"]
        else:
            value=profile.get(key) if key else None
        kind = str(field.get("type", "")).lower()
        supported = {"text", "email", "tel", "url", "file", "textarea", "date", "month", "number", "select-one", "select-multiple", "radio", "checkbox"}
        if value is None or value == "" or kind not in supported:
            if field.get("required") or SENSITIVE.search(str(field.get("label",""))):
                review.append({"id":field["id"],"reason":"unresolved field"})
            continue
        operation = "fill"
        if kind in ("select-one", "select-multiple"):
            choices = field.get("options", [])
            desired = value if isinstance(value, list) else [value]
            by_value = {str(o.get("value", "")).strip().casefold(): str(o.get("value", "")) for o in choices}
            by_label = {str(o.get("label", "")).strip().casefold(): str(o.get("value", "")) for o in choices}
            selected = []
            for choice in desired:
                match = by_value.get(str(choice).strip().casefold()) or by_label.get(str(choice).strip().casefold())
                if match is None:
                    selected = []
                    break
                selected.append(match)
            if not selected:
                review.append({"id":field["id"],"reason":"approved value does not match a form choice"})
                continue
            value = selected if kind == "select-multiple" else selected[0]
            operation = "select"
        elif kind == "radio":
            choices = field.get("options", [])
            matched = next((o for o in choices if str(value).strip().casefold() in (str(o.get("value", "")).strip().casefold(), str(o.get("label", "")).strip().casefold())), None)
            if not matched:
                review.append({"id":field["id"],"reason":"approved value does not match a radio choice"})
                continue
            value = matched.get("value")
            operation = "check"
        elif kind == "checkbox":
            if not isinstance(value, bool):
                review.append({"id":field["id"],"reason":"checkbox needs an approved boolean"})
                continue
            operation = "check" if value else "uncheck"
        if kind == "file":
            p=Path(value)
            extensions = (".pdf", ".docx") if key == "resume" else (".pdf", ".docx", ".txt")
            if not p.is_file() or p.suffix.lower() not in extensions:
                review.append({"id":field["id"],"reason":"invalid local upload file"})
                continue
            operation = "upload"
        actions.append({"id":field["id"],"operation":operation,"profile_key":key,"value":value})
    return {"actions":actions,"review":review,"ready":not review,"submission_authorized":False}
